"""Typed, tool-free decision calls and strict provider-neutral output contracts."""

from __future__ import annotations

import json
import math
from typing import Any

import jsonschema

from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import IDENTIFIER, ConfigError

PROBABILITY = {"type": "number", "minimum": 0, "maximum": 1}


def object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def questions_schema(questions: Any) -> dict:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 128:
        raise ConfigError("decision must contain 1 to 128 named questions")
    outputs = {}
    for name, question in questions.items():
        if (
            not isinstance(name, str)
            or not IDENTIFIER.fullmatch(name)
            or not isinstance(question, dict)
        ):
            raise ConfigError("decision questions must be named mappings")
        kind = question.get("type")
        if not isinstance(kind, str) or kind not in {"predicate", "choice", "score"}:
            raise ConfigError(f"decision.{name}.type must be predicate, choice or score")
        extra = {"choice": {"choices"}, "score": {"levels"}}.get(kind, set())
        if (
            set(question) != {"type", "instructions"} | extra
            or not isinstance(question.get("instructions"), str)
            or not question["instructions"].strip()
        ):
            raise ConfigError(f"decision.{name} requires instructions and the fields for {kind}")
        properties = {"type": {"const": kind}, "name": {"const": name}}
        if kind == "predicate":
            properties["probability"] = PROBABILITY
        else:
            entries = question["choices" if kind == "choice" else "levels"]
            maximum = 255 if kind == "choice" else 10
            if not isinstance(entries, list) or not 2 <= len(entries) <= maximum:
                raise ConfigError(f"decision.{name} requires 2 to {maximum} options")
            key = "value" if kind == "choice" else "label"
            values = []
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or key not in entry
                    or set(entry) - {key, "description"}
                ):
                    raise ConfigError(f"decision.{name} has an invalid option")
                value = entry[key]
                if (
                    not isinstance(value, (str, bool) if kind == "choice" else str)
                    or isinstance(value, str)
                    and not value.strip()
                ):
                    raise ConfigError(f"decision.{name} has an invalid {key}")
                if "description" in entry and not isinstance(entry["description"], str):
                    raise ConfigError(f"decision.{name} descriptions must be text")
                values.append(value)
            canonical = [
                str(value).lower() if isinstance(value, bool) else value for value in values
            ]
            if len(set(canonical)) != len(values):
                raise ConfigError(f"decision.{name} options must be unique")
            probability = {
                "value": {"enum": values}
                if kind == "choice"
                else {"type": "integer", "minimum": 0, "maximum": len(values) - 1},
                "probability": PROBABILITY,
            }
            if kind == "score":
                probability["label"] = {"enum": values}
            properties.update(
                {
                    "confidence": PROBABILITY,
                    "probabilities": {
                        "type": "array",
                        "minItems": len(values),
                        "maxItems": len(values),
                        "items": object_schema(probability),
                    },
                }
            )
            properties["choice" if kind == "choice" else "score"] = (
                {"enum": values}
                if kind == "choice"
                else {"type": "number", "minimum": 0, "maximum": len(values) - 1}
            )
        outputs[name] = object_schema(properties)
    return object_schema(outputs)


def validate_answers(questions: dict, response: dict) -> dict:
    answers = response.get("answers")
    if not isinstance(answers, list) or any(not isinstance(answer, dict) for answer in answers):
        raise ExecutionError("decision response must contain named answers")
    outputs = {
        answer.get("name"): answer for answer in answers if isinstance(answer.get("name"), str)
    }
    if len(outputs) != len(answers) or list(outputs) != list(questions):
        raise ExecutionError("decision response has missing or duplicate answer names")
    try:
        jsonschema.validate(outputs, questions_schema(questions))
        json.dumps(response, allow_nan=False)
    except (jsonschema.ValidationError, ValueError, TypeError) as exc:
        raise ExecutionError("decision response does not match its declared questions") from exc
    for name, answer in outputs.items():
        probabilities = answer.get("probabilities")
        if probabilities is not None:
            values = [json.dumps(item["value"]) for item in probabilities]
            if len(set(values)) != len(values) or not math.isclose(
                sum(item["probability"] for item in probabilities), 1, abs_tol=0.001
            ):
                raise ExecutionError("decision response has invalid probabilities")
            if answer["type"] == "score" and [
                (item["value"], item["label"]) for item in probabilities
            ] != [(index, level["label"]) for index, level in enumerate(questions[name]["levels"])]:
                raise ExecutionError("decision score labels do not match declared levels")
    return outputs


def local_client(compiled: dict, resolver=None):
    def call(*, step: str, input: dict) -> dict:
        from outcomeci.reasoning.models import _key

        try:
            import litellm
        except ImportError as exc:
            raise ExecutionError("local decision steps require outcomeci-cli[models]") from exc
        block = compiled["instructions"]["steps"][step]["v1"]
        profile = block["reasoning"]
        try:
            from outcomeci.reasoning.decision_transport import install_decision_response_guard

            install_decision_response_guard(profile["model"].split("/")[0], asynchronous=False)
            response = litellm.decisions(
                model=profile["model"],
                api_key=_key(profile, resolver),
                input=json.dumps(input, allow_nan=False),
                questions=[
                    {"name": name, **question} for name, question in block["decision"].items()
                ],
                timeout=60,
                num_retries=0,
                api_base={
                    "openai": "https://api.openai.com/v1",
                    "typesafe": "https://api.typesafe.ai",
                }[profile["model"].split("/")[0]],
                caching=False,
                **{"no-log": True},
            )
            return response.model_dump(mode="json")
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError(
                f"step {step}: decision provider call failed ({type(exc).__name__})"
            ) from exc

    return call
