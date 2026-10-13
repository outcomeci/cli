import pytest
import yaml

from outcomeci.workflow.compiler import ConfigError, compile_workflow


def compile_document(tmp_path, **fields):
    document = {
        "apiVersion": "outcomeci.workflow/v2",
        "trigger": "manual",
        "steps": [{"work": {"reason": "Do the work."}}],
        **fields,
    }
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(document))
    return compile_workflow(path)


def test_training_compiles_and_affects_revision(tmp_path):
    first = compile_document(tmp_path, training={"labeler": {"model": "openai/test"}})
    assert first["api_version"] == "outcomeci.workflow/v2"
    assert first["training"] == {"labeler": {"model": "openai/test"}}
    second = compile_document(tmp_path, training={"labeler": {"model": "anthropic/test"}})
    assert first["workflow_revision"] != second["workflow_revision"]


def test_training_key_resolves_like_reasoning(tmp_path):
    result = compile_document(
        tmp_path,
        secrets={"key": "vault:models/key"},
        training={"labeler": {"model": "openai/test", "key": "secrets.key"}},
    )
    assert result["training"]["labeler"] == {
        "model": "openai/test",
        "key": "key",
        "credential": "vault:models/key",
    }


@pytest.mark.parametrize(
    "training",
    [
        None,
        {},
        [],
        {"other": {}},
        {"labeler": None},
        {"labeler": {"runner": "codex"}},
        {"labeler": {"model": "test"}},
        {"labeler": {"model": "openai/test", "key": "secrets.missing"}},
    ],
)
def test_invalid_training(tmp_path, training):
    with pytest.raises(ConfigError, match="training"):
        compile_document(tmp_path, training=training)


def test_training_is_opt_in(tmp_path):
    assert "training" not in compile_document(tmp_path)
    with pytest.raises(ConfigError, match="training"):
        compile_document(
            tmp_path,
            apiVersion="outcomeci.workflow/v1",
            training={"labeler": {"model": "openai/test"}},
        )
