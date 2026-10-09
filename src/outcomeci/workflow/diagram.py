"""A workflow's static diagram: what starts it, its steps, and how they connect.

`workflow_diagram(compiled)` projects a compiled v1 workflow into a small,
versioned structure a UI can draw without knowing the workflow format: the
trigger, one node per step in order, and the data dependencies between steps.
It reads only the compiled workflow, so it needs no network or Vault access,
and it is deterministic. It never includes credentials, Vault references,
secret names, grant arguments, or instructions beyond a one-line summary.

Within `outcomeci.workflow-diagram/v1`, fields are only added; a consumer
renders an unknown step `kind` as a generic step.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = "outcomeci.workflow-diagram/v1"
SUMMARY_LIMIT = 160

TRIGGER_LABELS = {
    "webhook": "Webhook",
    "cron": "Schedule",
    "email": "Email",
    "manual": "Manual",
}


def workflow_diagram(compiled: Mapping[str, Any]) -> dict[str, Any]:
    """The diagram of a compiled workflow (see the module docstring)."""
    steps: Mapping[str, Any] = compiled["instructions"]["steps"]
    connectors: Mapping[str, Any] = compiled.get("connectors") or {}
    source = compiled.get("source") or {}
    order = list(steps)
    nodes = [_node(name, index, steps[name], connectors) for index, name in enumerate(order)]
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_revision": compiled["workflow_revision"],
        "name": str(source.get("name") or _metadata_name(compiled) or ""),
        "trigger": _trigger(compiled),
        "nodes": nodes,
        "edges": _edges(order, steps, nodes),
    }


def _metadata_name(compiled: Mapping[str, Any]) -> str | None:
    workflow = compiled.get("workflow") or {}
    return (workflow.get("metadata") or {}).get("name")


def _trigger(compiled: Mapping[str, Any]) -> dict[str, Any]:
    triggers: Mapping[str, Any] = compiled.get("triggers") or {}
    config = next(iter(triggers.values()), {}) if triggers else {}
    raw = str(config.get("type") or "manual")
    kind = raw.removesuffix(".received")
    trigger: dict[str, Any] = {
        "id": "trigger",
        "type": kind,
        "provider": None,
        "label": TRIGGER_LABELS.get(kind, kind.replace("_", " ").title()),
    }
    if kind == "cron":
        trigger["schedule"] = {
            "expression": str(config.get("expression", "")),
            "timezone": str(config.get("timezone", "")),
        }
    receiver = config.get("receiver")
    if kind == "webhook" and isinstance(receiver, Mapping):
        provider = str(receiver.get("uses") or "") or None
        trigger["provider"] = provider
        trigger["events"] = sorted({str(event) for event in receiver.get("events") or []})
        if provider:
            trigger["label"] = f"{provider.replace('_', ' ').title()} events"
    return trigger


def _node(
    name: str, index: int, step: Mapping[str, Any], connectors: Mapping[str, Any]
) -> dict[str, Any]:
    block: Mapping[str, Any] = step.get("v1") or {}
    reasoning = block.get("reasoning") or {}
    compiled_kind = str(block.get("kind") or "agent")
    # A step whose reasoning selects a model profile is a model step; any
    # other agent step runs an agent. Converse, await and later kinds keep
    # their own kind.
    kind = "model" if compiled_kind == "agent" and reasoning.get("model") else compiled_kind
    node: dict[str, Any] = {
        "id": name,
        "order": index,
        "kind": kind,
        "label": name,
        "summary": _summary(step.get("content")),
        "connectors": _connectors(block.get("grants") or [], connectors),
        "reviewed": bool(block.get("policy")),
    }
    if kind == "agent":
        runner = (step.get("policy") or {}).get("runner")
        node["runner"] = str(runner) if runner else None
    if reasoning.get("model"):
        node["model"] = _model(reasoning)
    for_each = block.get("for_each")
    if isinstance(for_each, Mapping):
        node["for_each"] = {"over": str(for_each.get("ref")), "as": str(for_each.get("as"))}
    gate = _gate(block, connectors)
    if gate:
        node["gate"] = gate
    when = block.get("when")
    if isinstance(when, Mapping):
        condition: dict[str, Any] = {"ref": str(when.get("ref")), "op": str(when.get("op"))}
        if "value" in when:
            condition["value"] = when["value"]
        node["condition"] = condition
    return node


def _model(reasoning: Mapping[str, Any]) -> dict[str, Any]:
    model = str(reasoning["model"])
    described: dict[str, Any] = {"provider": model.split("/", 1)[0], "model": model}
    fallback = reasoning.get("fallback")
    if isinstance(fallback, Mapping) and fallback.get("model"):
        other = str(fallback["model"])
        described["fallback"] = {"provider": other.split("/", 1)[0], "model": other}
    return described


def _connectors(
    grants: list[Mapping[str, Any]], connectors: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One entry per declared API the step may call, with its operations.

    Icons key on `provider`, the connector the alias uses (alias `x_company`
    uses `x`). Grant arguments, which can name channels or repositories, are
    never included."""
    operations: dict[str, set[str]] = {}
    for grant in grants:
        api, _, operation = str(grant.get("capability", "")).partition(".")
        if api and operation:
            operations.setdefault(api, set()).add(operation)
    return [
        {
            "api": api,
            "provider": str((connectors.get(api) or {}).get("provider") or api),
            "operations": sorted(names),
        }
        for api, names in sorted(operations.items())
    ]


def _gate(block: Mapping[str, Any], connectors: Mapping[str, Any]) -> dict[str, Any] | None:
    for kind, signal in (("converse", "thread"), ("await", "reaction")):
        spec = block.get(kind)
        if not isinstance(spec, Mapping):
            continue
        api = str(spec.get("api") or "")
        gate: dict[str, Any] = {
            "type": kind,
            "provider": str((connectors.get(api) or {}).get("provider") or api) or None,
            "signal": signal,
        }
        if spec.get("subject"):
            gate["subject"] = str(spec["subject"])
        if spec.get("by"):
            gate["by"] = str(spec["by"])
        if kind == "await" and spec.get("emoji"):
            gate["emoji"] = str(spec["emoji"])
        if isinstance(spec.get("timeout_seconds"), int):
            gate["timeout_seconds"] = spec["timeout_seconds"]
        if isinstance(spec.get("max_turns"), int):
            gate["max_turns"] = spec["max_turns"]
        return {key: value for key, value in gate.items() if value is not None}
    return None


def _summary(content: Any) -> str | None:
    """The first line of a step's instructions, as one short sentence.

    A Markdown heading is used as written; otherwise the first sentence of the
    first paragraph."""
    if not isinstance(content, str):
        return None
    for line in content.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            return _clip(text.lstrip("#").strip())
        paragraph = " ".join(
            part.strip() for part in content[content.index(line) :].split("\n\n", 1)[0].splitlines()
        )
        sentence = re.split(r"(?<=[.!?])\s+", paragraph, maxsplit=1)[0]
        return _clip(sentence)
    return None


def _clip(text: str) -> str | None:
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 1].rstrip() + "…"


def _edges(
    order: list[str], steps: Mapping[str, Any], nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Data dependencies, from each step a step reads (and the trigger).

    A step that reads the trigger, or no earlier step, has an edge from the
    trigger. The edge from the step its condition reads is a `condition`
    edge, labeled with the condition."""
    position = {name: index for index, name in enumerate(order)}
    edges: list[dict[str, Any]] = []
    for node in nodes:
        name = node["id"]
        block: Mapping[str, Any] = steps[name].get("v1") or {}
        reads = [
            step
            for step in block.get("reads") or []
            if step in position and position[step] < position[name]
        ]
        reads_trigger = any(
            str(item.get("ref", "")).split(".", 1)[0] == "trigger"
            for item in block.get("inputs") or []
            if isinstance(item, Mapping)
        )
        condition = node.get("condition")
        condition_from = None
        if condition:
            head = condition["ref"].split(".", 1)[0]
            condition_from = head if head in position else "trigger"
        sources = (["trigger"] if reads_trigger or not reads else []) + sorted(
            reads, key=position.__getitem__
        )
        if condition_from == "trigger" and "trigger" not in sources:
            sources.insert(0, "trigger")
        for source in dict.fromkeys(sources):
            is_condition = source == condition_from
            edges.append(
                {
                    "from": source,
                    "to": name,
                    "kind": "condition" if is_condition else "data",
                    "label": _condition_label(condition) if is_condition else None,
                }
            )
    return edges


def _condition_label(condition: Mapping[str, Any]) -> str:
    if condition["op"] == "truthy":
        return condition["ref"]
    value = condition.get("value")
    shown = str(value).lower() if isinstance(value, bool) else str(value)
    return f"{condition['ref']} {condition['op']} {shown}"
