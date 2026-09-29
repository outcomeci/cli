"""Versioned trigger payload contracts, validated before any run starts."""

from __future__ import annotations

import json
import re
from datetime import datetime
from importlib.resources import files
from typing import Any

import jsonschema

FORMAT_CHECKER = jsonschema.FormatChecker()


@FORMAT_CHECKER.checks("date-time")
def _date_time(value: Any) -> bool:
    """Enforce offset-qualified timestamps without an optional format dependency."""
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value
    ):
        return False
    try:
        return datetime.fromisoformat(value.upper()).utcoffset() is not None
    except ValueError:
        return False


CONTRACT_FILES = {
    "email.received": "email-received-v1.schema.json",
    "webhook.received": "webhook-received-v1.schema.json",
    "cron": "cron-received-v1.schema.json",
}


class ContractError(ValueError):
    """A safe contract error that does not echo untrusted payload values."""


def contract_schema(name: str) -> dict[str, Any]:
    filename = CONTRACT_FILES.get(name)
    if filename is None:
        raise ContractError(f"unsupported contract type {name}")
    resource = files("outcomeci").joinpath("schemas", filename)
    return json.loads(resource.read_text(encoding="utf-8"))


def validate_contract(name: str, value: Any) -> None:
    schema = contract_schema(name)
    validator = jsonschema.Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    error = next(validator.iter_errors(value), None)
    if error is not None:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ContractError(f"{name} contract failed at {path} ({error.validator})")


def validate_trigger_payload(trigger_type: str, value: Any) -> None:
    if trigger_type == "manual":
        if not isinstance(value, dict):
            raise ContractError("manual trigger payload must be an object")
        return
    validate_contract(trigger_type, value)
