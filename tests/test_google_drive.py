"""Drive contracts run through the real compiler, grant policy and credential broker."""

from pathlib import Path

import httpx
import pytest
import yaml
from outcomeci_connectors.providers.google.drive import PROVIDER

from outcomeci.broker import executor as integrations
from outcomeci.broker.executor import IntegrationError, IntegrationExecutor
from outcomeci.broker.policy import PolicyExecutor
from outcomeci.runtime.process import ExecutionError
from outcomeci.vault.leases import LeaseResolver
from outcomeci.workflow import language
from outcomeci.workflow.compiler import compile_workflow


def setup(tmp_path, monkeypatch, *, parents=("folder1",), expires="2099-01-01T00:00:00+00:00"):
    monkeypatch.setattr(language, "providers", lambda: {PROVIDER.name: PROVIDER})
    monkeypatch.setattr(integrations, "_safe_destination", lambda *args: None)
    source = tmp_path / "drive.outcome.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v1",
                "trigger": "manual",
                "secrets": {"google": "vault:google/team-drive"},
                "apis": {"drive": {"uses": "google.drive", "auth": "secrets.google"}},
                "steps": [
                    {
                        "inspect": {
                            "reason": "Inspect documents",
                            "can": [
                                {"drive.files.download": {"folder_id": "folder1"}},
                                {"drive.files.export": {"folder_id": "folder1"}},
                                {"drive.files.list": {"folder_id": "folder1"}},
                                {"drive.files.update": {"file_id": "file1"}},
                            ],
                        }
                    }
                ],
            }
        )
    )
    compiled = compile_workflow(source)
    seen = []

    def server(request):
        seen.append(request)
        assert request.url.host == "www.googleapis.com"
        assert request.headers["authorization"] == "Bearer leased-google-access"
        if request.url.params.get("alt") == "media" or request.url.path.endswith("/export"):
            return httpx.Response(200, content=b"document bytes")
        return httpx.Response(
            200,
            json={
                "id": "file1",
                "name": "document",
                "mimeType": "text/plain",
                "parents": list(parents),
            },
        )

    resolver = LeaseResolver(
        {
            "google/team-drive": {
                "credential_type": "auth_header",
                "service": "google",
                "configuration": {"header_name": "Authorization", "scheme": "Bearer"},
                "secrets": {"value": "leased-google-access"},
            }
        },
        expires,
    )
    executor = IntegrationExecutor(
        compiled,
        resolver=resolver,
        transport=httpx.MockTransport(server),
        reviewed=True,
        downloads=tmp_path / "downloads",
    )
    return executor, seen


@pytest.mark.parametrize(
    "operation,extra", [("files.download", {}), ("files.export", {"mime_type": "application/pdf"})]
)
def test_managed_bearer_download_and_export(tmp_path, monkeypatch, operation, extra):
    executor, seen = setup(tmp_path, monkeypatch)
    result = executor.execute(
        "drive." + operation,
        {"file_id": "file1", **extra},
        step="inspect",
        response_grants=[[{"name": "folder_id", "paths": ["body.parents"], "granted": "folder1"}]],
    )
    file = result["output"]["file"]
    assert Path(file["path"]).read_bytes() == b"document bytes"
    assert file["content_type"] == extra.get("mime_type", "text/plain")
    assert len(seen) == 2
    assert "leased-google-access" not in str(result)
    assert all(r.url.host != "oauth2.googleapis.com" for r in seen)


def test_folder_grant_refuses_other_drive_content(tmp_path, monkeypatch):
    executor, seen = setup(tmp_path, monkeypatch, parents=("other",))
    with pytest.raises(IntegrationError, match="folder_id must be folder1"):
        executor.execute(
            "drive.files.download",
            {"file_id": "file1"},
            step="inspect",
            response_grants=[
                [{"name": "folder_id", "paths": ["body.parents"], "granted": "folder1"}]
            ],
        )
    assert len(seen) == 1


def test_expired_managed_lease_never_sends_access_token(tmp_path, monkeypatch):
    executor, seen = setup(tmp_path, monkeypatch, expires="2000-01-01T00:00:00+00:00")
    with pytest.raises(ExecutionError, match="lease expired"):
        executor.execute("drive.files.download", {"file_id": "file1"}, step="inspect")
    assert seen == []


def test_update_grant_prevents_mutating_another_file(tmp_path, monkeypatch):
    executor, seen = setup(tmp_path, monkeypatch)
    policy = PolicyExecutor(
        executor,
        tmp_path,
        {},
        grants=[{"capability": "drive.files.update", "args": {"file_id": "file1"}, "as": None}],
    )
    with pytest.raises(ExecutionError):
        policy._apply_grants(
            "drive.files.update", {"file_id": "other", "metadata": {"name": "Renamed"}}
        )
    assert seen == []
