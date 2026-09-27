from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from outcomeci.config import ConfigError, _human_interactions, compile_workflow
from outcomeci.repository import initialize


def _hook(delivery: dict) -> dict:
    return {
        "id": "confirm",
        "participant": "requester",
        "purpose": "Confirm scope",
        "interaction": "approval",
        "delivery": delivery,
    }


@pytest.mark.parametrize("delivery", [{"type": "slack"}, {"type": "slack", "mode": "message"}])
def test_slack_delivery_requires_reaction_or_reply_mode(delivery: dict) -> None:
    with pytest.raises(ConfigError, match="mode is required and must be reaction or reply"):
        _human_interactions({"before": [_hook(delivery)]}, "spec.agents.phases.intake.humans")


def test_custom_delivery_requires_a_connection() -> None:
    with pytest.raises(ConfigError, match="delivery.connection is required"):
        _human_interactions(
            {"before": [_hook({"type": "custom"})]}, "spec.agents.phases.intake.humans"
        )


def test_slack_is_not_a_connection_provider(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
    value = yaml.safe_load(workflow.read_text())
    value["spec"]["connections"] = [{"ref": "slack_local", "provider": "slack"}]
    workflow.write_text(yaml.safe_dump(value, sort_keys=False))
    with pytest.raises(ConfigError, match=r"spec.connections\[0\].provider is unsupported"):
        compile_workflow(workflow)
