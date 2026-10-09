from __future__ import annotations

import pytest

from outcomeci.broker import server as capability
from outcomeci.runtime.process import ExecutionError


def _broker():
    broker = capability.Broker.__new__(capability.Broker)
    broker.run_id, broker.token, broker.step = "run-1", "secret", "investigate"
    return broker


def test_broker_rejects_a_wrong_token_or_run() -> None:
    broker = _broker()
    with pytest.raises(ExecutionError, match="invalid outcome capability"):
        broker.dispatch({"token": "guess", "kind": "integration", "run_id": "run-1"})
    with pytest.raises(ExecutionError, match="not authorized"):
        broker.dispatch({"token": "secret", "kind": "integration", "run_id": "other"})


def test_broker_serves_only_integration_calls() -> None:
    with pytest.raises(ExecutionError, match="not allowed"):
        _broker().dispatch({"token": "secret", "run_id": "run-1", "operation": "poll"})


def test_broker_requires_an_object_input() -> None:
    with pytest.raises(ExecutionError, match="must be an object"):
        _broker().dispatch(
            {"token": "secret", "kind": "integration", "run_id": "run-1", "inputs": "text"}
        )
