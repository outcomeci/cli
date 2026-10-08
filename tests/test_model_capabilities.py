from __future__ import annotations

import json
from importlib import metadata
from types import SimpleNamespace

import httpx
import pytest
import yaml

from outcomeci import cli, models
from outcomeci import model_capabilities as caps
from outcomeci.config import ConfigError, compile_workflow
from outcomeci.process import ExecutionError


@pytest.fixture(autouse=True)
def reset_catalog():
    reader = caps._catalog
    reader.cache_clear()
    yield
    reader.cache_clear()


def catalog(monkeypatch, rows):
    monkeypatch.setattr(caps, "_catalog", lambda: (rows, "1.104.2", "known"))


def row(**values):
    return {
        "litellm_provider": "openai",
        "mode": "chat",
        "supports_function_calling": True,
        "supports_vision": True,
        "max_input_tokens": 1000,
        "max_output_tokens": 200,
        **values,
    }


def workflow(tmp_path, *, fallback=None, returns=True):
    profile = {"model": "openai/test"}
    if fallback:
        profile["fallback"] = {"model": fallback}
    document = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "reasoning": {"model": profile},
        "steps": [
            {
                "answer": {
                    "reason": "Answer",
                    "using": "model",
                    **({"returns": {"answer": "string"}} if returns else {}),
                }
            }
        ],
    }
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


@pytest.mark.parametrize(
    "value,expected", [(True, True), (False, False), (None, None), (0, None), ("false", None)]
)
def test_capability_metadata_preserves_three_states(monkeypatch, value, expected):
    catalog(monkeypatch, {"openai/test": row(supports_function_calling=value)})
    assert caps.get_capabilities("openai/test").tools is expected
    if expected is False:
        with pytest.raises(caps.CapabilityError, match="unsupported_tools"):
            caps.validate_requirements("openai/test", tools=True)
    else:
        report = caps.validate_requirements("openai/test", tools=True)
        assert report["status"] == ("verified" if expected else "unverified")


def test_absent_capability_is_not_inferred_false(monkeypatch):
    data = row()
    del data["supports_function_calling"]
    catalog(monkeypatch, {"openai/test": data})
    assert "tools_unverified" in caps.validate_requirements("openai/test", tools=True)["warnings"]


def test_provider_lookup_never_borrows_vendor_or_case_colliding_metadata(monkeypatch):
    catalog(
        monkeypatch,
        {"Test": row(), "openai/test": row(), "vendor/test": row(litellm_provider="vendor")},
    )
    assert caps.get_capabilities("openai/Test").tools is True
    assert caps.get_capabilities("openai/TEST").metadata_status == "unknown"
    assert caps.get_capabilities("openrouter/openai/test").metadata_status == "unknown"
    assert caps.get_capabilities("groq/test").metadata_status == "unknown"


def test_compiler_rejects_primary_and_fallback_incompatibility_with_location(tmp_path, monkeypatch):
    catalog(
        monkeypatch, {"openai/test": row(), "openai/no-tools": row(supports_function_calling=False)}
    )
    with pytest.raises(ConfigError, match="step answer profile model fallback.*unsupported_tools"):
        compile_workflow(workflow(tmp_path, fallback="openai/no-tools"))
    catalog(monkeypatch, {"openai/test": row(mode="embedding")})
    with pytest.raises(
        ConfigError, match="step answer profile model primary.*incompatible_model_mode"
    ):
        compile_workflow(workflow(tmp_path, returns=False))


def test_reports_change_without_changing_workflow_revision(tmp_path, monkeypatch):
    path = workflow(tmp_path)
    catalog(monkeypatch, {"openai/test": row()})
    known = compile_workflow(path)
    catalog(monkeypatch, {})
    unknown = compile_workflow(path)
    assert known["workflow_revision"] == unknown["workflow_revision"]
    assert known["model_capabilities"][0]["status"] == "verified"
    assert unknown["model_capabilities"][0]["status"] == "unverified"
    assert unknown["capability_warnings"]


def test_compiler_without_sdk_reports_unverified(tmp_path, monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(caps.metadata, "distribution", missing)
    compiled = compile_workflow(workflow(tmp_path))
    assert compiled["model_capabilities"][0]["capabilities"]["metadata_status"] == "sdk_unavailable"
    assert "sdk_unavailable" in compiled["model_capabilities"][0]["warnings"]


def test_cli_validate_exposes_reports(tmp_path, monkeypatch, capsys):
    catalog(monkeypatch, {})
    path = workflow(tmp_path)
    assert cli.main(["validate", "--dir", str(tmp_path), "--config", path.name]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["model_capabilities"][0]["status"] == "unverified"
    assert result["capability_warnings"]


def test_runtime_rejects_vision_and_bounds_estimated_context(monkeypatch):
    catalog(monkeypatch, {"openai/test": row(supports_vision=False)})
    images = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://private.invalid/image"}}
            ],
        }
    ]
    with pytest.raises(caps.CapabilityError, match="unsupported_vision"):
        caps.preflight("openai/test", messages=images, tools=[])
    monkeypatch.setattr(caps, "_estimate", lambda *a: 950)
    report = caps.preflight(
        "openai/test", messages=[{"role": "user", "content": "private"}], tools=[]
    )
    assert report["max_tokens"] == 50
    assert report["input_count_status"] == "estimated"
    assert report["status"] == "unverified"
    assert "input_tokens_estimated" in report["warnings"]
    monkeypatch.setattr(caps, "_estimate", lambda *a: 1000)
    with pytest.raises(caps.CapabilityError, match="estimated_input_exceeds_window") as error:
        caps.preflight(
            "openai/test", messages=[{"role": "user", "content": "secret-prompt"}], tools=[]
        )
    assert "secret-prompt" not in str(error.value)


def test_missing_tokenizer_leaves_context_unverified(monkeypatch):
    catalog(monkeypatch, {"openai/test": row()})
    monkeypatch.setattr(caps, "_estimate", lambda *a: None)
    result = caps.preflight("openai/test", messages=[], tools=[])
    assert result["estimated_input_tokens"] is None
    assert result["input_count_status"] == "unverified"
    assert "tokenizer_unavailable" in result["warnings"]


def test_actual_offline_estimate_counts_tools_without_image_or_tokenizer_requests(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    pytest.importorskip("litellm")

    def no_network(*args, **kwargs):
        pytest.fail("token preflight attempted network")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    import requests

    monkeypatch.setattr(requests.Session, "request", no_network)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "https://private.invalid/image"}},
            ],
        }
    ]
    plain = caps._estimate("openai/gpt-4.1-mini", messages, [])
    tool = {
        "type": "function",
        "function": {
            "name": "answer",
            "description": "Return the answer",
            "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}},
        },
    }
    counted = caps._estimate("openai/gpt-4.1-mini", messages, [tool])
    assert plain is not None and counted > plain


def test_bad_fallback_preflight_happens_before_any_key_or_completion(monkeypatch):
    import sys

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(completion=lambda **k: pytest.fail("provider called")),
    )
    catalog(monkeypatch, {"openai/test": row(), "openai/bad": row(supports_function_calling=False)})
    monkeypatch.setattr(caps, "_estimate", lambda *a: 10)
    compiled = {
        "reasoning": {
            "model": {
                "model": "openai/test",
                "credential": "vault:key",
                "fallback": {"model": "openai/bad", "credential": "vault:other"},
            }
        }
    }
    with pytest.raises(ExecutionError, match="fallback.*unsupported_tools"):
        models.local_client(compiled, lambda ref: pytest.fail("key resolved"))(
            step="answer", profile="model", messages=[], tools=[{"type": "function"}]
        )


def test_known_incompatibility_is_rejected_even_before_sdk_import(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "litellm", None)
    catalog(monkeypatch, {"openai/test": row(supports_vision=False)})
    compiled = {"reasoning": {"model": {"model": "openai/test", "credential": "vault:key"}}}
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        }
    ]
    with pytest.raises(ExecutionError, match="unsupported_vision"):
        models.local_client(compiled, lambda ref: pytest.fail("key resolved"))(
            step="answer", profile="model", messages=messages, tools=[]
        )


def test_runtime_retains_primary_and_unused_fallback_reports_in_transcript(monkeypatch):
    import sys

    catalog(monkeypatch, {"openai/test": row(), "openai/backup": row()})
    monkeypatch.setattr(caps, "_estimate", lambda *a: 50)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="done", tool_calls=[]), finish_reason="stop"
            )
        ],
        usage=None,
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=lambda **k: response))
    compiled = {
        "reasoning": {
            "model": {
                "model": "openai/test",
                "credential": "vault:key",
                "fallback": {"model": "openai/backup", "credential": "vault:backup"},
            }
        }
    }
    keys, turns = [], []
    client = models.local_client(compiled, lambda ref: keys.append(ref) or "key")
    models.run(
        client,
        step="answer",
        profile="model",
        system="Answer",
        user="Hi",
        capabilities=[],
        call=lambda *a: {},
        turns=turns,
    )
    assert keys == ["vault:key"]
    reports = turns[-1]["model_capabilities"]
    assert [report["role"] for report in reports] == ["primary", "fallback"]
    assert all(report["input_count_status"] == "estimated" for report in reports)
    assert turns[-1]["capability_warnings"]
