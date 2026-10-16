"""Compiler contract for bounded, opt-in historical step outputs."""

from __future__ import annotations

import math
import re
from typing import Any

from outcomeci.workflow.compiler import ConfigError

CAPABILITY = "queryable-step-outputs/v1"
SYSTEM_FIELDS = {"completed_at", "workflow_revision", "run_id", "step_id", "output_id"}


def mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ConfigError(f"{label} must be a mapping")
    return value


def validate(document: dict) -> dict:
    config = mapping(document.get("outputs", {}), "outputs")
    if set(config) - {"sources", "queries"}:
        raise ConfigError("outputs supports sources and queries only")
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ConfigError("steps must be a nonempty list")
    steps = {}
    for entry in raw_steps:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ConfigError("steps must contain single named mappings")
        for name, body in entry.items():
            if not isinstance(name, str) or name in steps:
                raise ConfigError("steps must have unique string names")
            steps[name] = mapping(body, f"steps.{name}")
    indexes = {}
    for name, body in steps.items():
        if "index" not in body:
            continue
        index = mapping(body["index"], f"{name}.index")
        fields = index.get("fields")
        if set(index) != {"fields"} or not isinstance(fields, list) or not fields:
            raise ConfigError(f"{name}.index requires nonempty fields")
        from outcomeci.workflow.language import shape_schema

        schema = shape_schema(body.get("returns"), f"{name}.returns")
        props = schema.get("properties", {})
        if any(
            not isinstance(f, str)
            or f not in props
            or props[f].get("type") not in {"string", "integer", "number", "boolean"}
            for f in fields
        ):
            raise ConfigError(f"{name}.index fields must name scalar returns")
        if len(fields) > 100 or len(set(fields)) != len(fields) or set(fields) & SYSTEM_FIELDS:
            raise ConfigError("index fields must be unique, nonreserved, and at most 100")
        indexes[name] = {"fields": fields, "field_types": {f: props[f]["type"] for f in fields}}
    sources = mapping(config.get("sources", {}), "outputs.sources")
    if len(sources) > 20:
        raise ConfigError("outputs supports at most 20 sources")
    for name, raw in sources.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            raise ConfigError("output source names must be identifiers")
        source = mapping(raw, f"outputs.sources.{name}")
        if (
            set(source) - {"workflow", "steps", "fields", "run_status", "max_results"}
            or source.get("workflow") != "self"
        ):
            raise ConfigError("output sources require workflow: self and supported constraints")
        selected = source.get("steps")
        fields = source.get("fields")
        if (
            not isinstance(selected, list)
            or not selected
            or len(selected) > 100
            or any(not isinstance(s, str) or s not in indexes for s in selected)
        ):
            raise ConfigError("output source steps must name indexed steps")
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > 100
            or any(not isinstance(f, str) for f in fields)
        ):
            raise ConfigError("output source fields must be a nonempty list")
        if len(set(selected)) != len(selected) or len(set(fields)) != len(fields):
            raise ConfigError("source steps and fields must be unique")
        for step in selected:
            returns = steps[step].get("returns", {})
            if any(f not in {k.removesuffix("?") for k in returns} for f in fields):
                raise ConfigError("output source fields must name declared returns")
        if source.get("run_status", "succeeded") not in ("succeeded", "step_succeeded"):
            raise ConfigError("output run_status must be succeeded or step_succeeded")
        limit = source.get("max_results", 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConfigError("output max_results must be 1..100")
    shared = mapping(config.get("queries", {}), "outputs.queries")
    reserved = set(steps) | {"trigger", "history", "calls"}

    def queries(items, predecessors):
        for name, raw in mapping(items, "query").items():
            if not isinstance(name, str) or not name.isidentifier() or name in reserved:
                raise ConfigError("query names must be identifiers and cannot shadow steps")
            query = mapping(raw, f"query.{name}")
            if set(query) - {
                "source",
                "where",
                "order_by",
                "limit",
                "required",
                "workflow_revision",
            }:
                raise ConfigError("unsupported output query field")
            source_name = query.get("source")
            if not isinstance(source_name, str):
                raise ConfigError("query source must be a string")
            source = sources.get(source_name)
            if source is None:
                raise ConfigError("query must name a declared output source")
            limit = query.get("limit", source.get("max_results", 20))
            if type(limit) is not int or not 1 <= limit <= source.get("max_results", 20):
                raise ConfigError("query limit exceeds source bounds")
            if "required" in query and type(query["required"]) is not bool:
                raise ConfigError("query.required must be boolean")
            if "workflow_revision" in query and not isinstance(query["workflow_revision"], str):
                raise ConfigError("query workflow_revision must be a string")
            indexed = set.intersection(*(set(indexes[s]["fields"]) for s in source["steps"]))
            allowed = indexed.intersection(source["fields"]) | SYSTEM_FIELDS
            where = mapping(query.get("where", {}), "query.where")
            if len(where) > 20:
                raise ConfigError("query supports at most 20 predicate fields")
            for field, predicates in where.items():
                if field not in allowed:
                    raise ConfigError(f"query field {field} is not permitted and indexed")
                predicates = mapping(predicates, "predicate")
                if not predicates:
                    raise ConfigError("query predicates cannot be empty")
                for op, value in predicates.items():
                    if op not in {"eq", "lt", "lte", "gt", "gte", "is_null"}:
                        raise ConfigError("unsupported output query predicate")
                    if isinstance(value, dict):
                        if (
                            set(value) != {"ref"}
                            or not isinstance(value["ref"], str)
                            or not re.fullmatch(
                                r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+",
                                value["ref"],
                            )
                            or value["ref"].split(".")[0] not in predecessors | {"trigger"}
                        ):
                            raise ConfigError(
                                "query references must name trigger or completed predecessors"
                            )
                    elif (
                        type(value) not in (str, int, float, bool)
                        or (type(value) is float and not math.isfinite(value))
                        or (type(value) is int and not -(2**63) <= value < 2**63)
                        or (op == "is_null" and type(value) is not bool)
                    ):
                        raise ConfigError(
                            "query predicates require scalar values or explicit references"
                        )
                    if not isinstance(value, dict) and op != "is_null":
                        from outcomeci.artifacts.output_store import _compatible

                        types = (
                            {indexes[s]["field_types"][field] for s in source["steps"]}
                            if field not in SYSTEM_FIELDS
                            else {"string"}
                        )
                        if any(
                            not _compatible(t, value) or (t == "boolean" and op != "eq")
                            for t in types
                        ):
                            raise ConfigError(f"query predicate type does not match {field}")
                    if isinstance(value, dict) and value["ref"].split(".")[0] != "trigger":
                        head, _, attribute = value["ref"].partition(".")
                        declared = {k.removesuffix("?") for k in steps[head].get("returns", {})}
                        if attribute not in declared:
                            raise ConfigError(
                                "query reference must name a declared predecessor return"
                            )
            order = query.get("order_by", [])
            if (
                not isinstance(order, list)
                or len(order) > 10
                or any(
                    not isinstance(o, dict)
                    or set(o) != {"field", "direction"}
                    or not isinstance(o.get("field"), str)
                    or o["field"] not in allowed
                    or o["direction"] not in ("asc", "desc")
                    for o in order
                )
            ):
                raise ConfigError("query order_by must name permitted indexed fields and asc/desc")

    queries(shared, set())
    previous = set()
    for name, step in steps.items():
        local = mapping(step.get("query", {}), f"{name}.query")
        if set(local) & set(shared):
            raise ConfigError("step queries cannot shadow workflow queries")
        queries(local, previous)
        previous.add(name)
    if len(shared) + sum(len(s.get("query", {})) for s in steps.values()) > 20:
        raise ConfigError("workflow exceeds 20 output queries")
    return {"sources": sources, "queries": shared, "indexes": indexes, "capability": CAPABILITY}
