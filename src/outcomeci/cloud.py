"""OutcomeCI Cloud device authentication and workflow synchronization."""

from __future__ import annotations

import base64
import json
import os
import stat
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import yaml

from outcomeci.runtime.process import ExecutionError
from outcomeci.security import private_path
from outcomeci.workflow.compiler import compile_workflow


class CloudRequestError(ExecutionError):
    """A cloud call that failed, with the HTTP status (None when unreachable)."""

    def __init__(self, message: str, status: int | None):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


def _raise_for_status(
    status: int, value: Any, expected: int | set[int], fallback: str, *, require_dict: bool = False
) -> None:
    ok = status in expected if isinstance(expected, set) else status == expected
    if require_dict:
        ok = ok and isinstance(value, dict)
    if not ok:
        detail = value.get("detail") if isinstance(value, dict) else None
        raise CloudRequestError(str(detail or fallback), status)


def credentials_path() -> Path:
    root = Path(os.environ.get("OUTCOMECI_CONFIG_HOME", Path.home() / ".config" / "outcomeci"))
    return root / "credentials.json"


def _request(
    api_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.request(
            method,
            f"{api_url.rstrip('/')}/v1{path}",
            json=body,
            headers=headers,
            timeout=30,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise CloudRequestError(f"could not reach OutcomeCI Cloud: {exc}", None) from exc
    try:
        return response.status_code, json.loads(response.content) if response.content else {}
    except json.JSONDecodeError:
        return response.status_code, {}


def _write_credentials(value: dict[str, Any]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def load_credentials() -> dict[str, Any]:
    path = credentials_path()
    if not path.is_file():
        raise ExecutionError("OutcomeCI Cloud is not authenticated; run `oci auth login`")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(
            "OutcomeCI credentials are unreadable; run `oci auth login` again"
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("access_token"), str):
        raise ExecutionError("OutcomeCI credentials are invalid; run `oci auth login` again")
    return value


def login(api_url: str, *, open_browser: bool = True) -> dict[str, Any]:
    status, attempt = _request(api_url, "/auth/device", method="POST", body={})
    if status != 201:
        raise ExecutionError(str(attempt.get("detail") or "could not start OutcomeCI login"))
    print(f"Open {attempt['verification_uri_complete']}")
    print(f"Confirm code: {attempt['user_code']}")
    if open_browser:
        webbrowser.open(str(attempt["verification_uri_complete"]))
    deadline = time.monotonic() + int(attempt["expires_in"])
    interval = max(1, int(attempt.get("interval", 3)))
    while time.monotonic() < deadline:
        status, tokens = _request(
            api_url,
            "/auth/device/token",
            method="POST",
            body={"device_code": attempt["device_code"]},
        )
        if status == 200:
            credentials = {**tokens, "api_url": api_url.rstrip("/")}
            _write_credentials(credentials)
            return {"authenticated": True, "api_url": credentials["api_url"]}
        if status != 428:
            raise ExecutionError(str(tokens.get("detail") or "OutcomeCI login failed"))
        time.sleep(interval)
    raise ExecutionError("OutcomeCI login expired")


def login_with_key(api_url: str, key: str) -> dict[str, Any]:
    """Persist a member-bound workspace key without sending it anywhere first."""
    key = key.strip()
    if not key.startswith("oci_") or len(key) < 20:
        raise ExecutionError("invalid OutcomeCI workspace key")
    credentials = {
        "api_url": api_url.rstrip("/"),
        "access_token": key,
        "credential_type": "workspace_key",
    }
    _write_credentials(credentials)
    return {
        "authenticated": True,
        "api_url": credentials["api_url"],
        "credential_type": "workspace_key",
    }


def logout() -> dict[str, Any]:
    path = credentials_path()
    if path.is_file():
        try:
            credentials = load_credentials()
            if credentials.get("credential_type") != "workspace_key":
                _request(
                    credentials["api_url"],
                    "/auth/revoke",
                    method="POST",
                    body={"refresh_token": credentials.get("refresh_token")},
                )
        except ExecutionError:
            pass
    if path.exists():
        path.unlink()
    return {"authenticated": False}


def auth_status() -> dict[str, Any]:
    try:
        value = load_credentials()
    except ExecutionError:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "api_url": value.get("api_url"),
        "credential_type": value.get("credential_type", "user"),
    }


def _refresh(credentials: dict[str, Any]) -> dict[str, Any]:
    status, tokens = _request(
        credentials["api_url"],
        "/auth/refresh",
        method="POST",
        body={"refresh_token": credentials["refresh_token"], "client_id": "outcomeci"},
    )
    if status != 200:
        raise ExecutionError("OutcomeCI login expired; run `oci auth login` again")
    value = {**tokens, "api_url": credentials["api_url"]}
    _write_credentials(value)
    return value


def _authorized_request(
    path: str, *, method: str = "GET", body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any] | list[Any]]:
    credentials = load_credentials()
    status, value = _request(
        credentials["api_url"], path, method=method, body=body, token=credentials["access_token"]
    )
    if status == 401:
        if credentials.get("credential_type") == "workspace_key":
            raise ExecutionError(
                "OutcomeCI workspace key is invalid or revoked; run `oci auth login` again"
            )
        credentials = _refresh(credentials)
        status, value = _request(
            credentials["api_url"],
            path,
            method=method,
            body=body,
            token=credentials["access_token"],
        )
    return status, value


def get_workflow(workspace_id: str, workflow_id: str) -> dict[str, Any]:
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflow-revisions/{workflow_id}/latest"
    )
    _raise_for_status(status, value, 200, "could not read workflow", require_dict=True)
    return value


def sync_workflow(
    path: Path,
    workspace_id: str,
    name: str | None,
    mode: str,
) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise ExecutionError(f"workflow file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix not in {".yml", ".yaml", ".json"}:
        raise ExecutionError("workflow must be YAML or JSON")
    compile_workflow(path)
    content = path.read_text(encoding="utf-8")
    document = json.loads(content) if suffix == ".json" else yaml.safe_load(content)
    workflow_name = (
        name
        or str(document.get("name") or (document.get("metadata") or {}).get("name") or "").strip()
    )
    if not workflow_name:
        raise ExecutionError(
            "workflow name is required; set name (v1) or metadata.name, or pass --name"
        )
    files: dict[str, str] = {}
    support_root = path.parent / ".outcomeci"
    if support_root.is_dir():
        total = 0
        for support in sorted(
            item
            for item in support_root.rglob("*")
            if item.is_file()
            and "outcomes" not in item.relative_to(support_root).parts
            and not private_path(item.relative_to(support_root))
        ):
            support_content = support.read_bytes()
            total += len(support_content)
            if len(support_content) > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
                raise ExecutionError(
                    "workflow support files exceed the 20 MiB synchronization limit"
                )
            files[str(Path(".outcomeci") / support.relative_to(support_root))] = base64.b64encode(
                support_content
            ).decode()
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflow-revisions",
        method="POST",
        body={
            "name": workflow_name,
            "mode": mode,
            "content": content,
            "content_type": "json" if suffix == ".json" else "yaml",
            "source_filename": path.name,
            "files": files,
        },
    )
    _raise_for_status(status, value, 201, "workflow synchronization failed", require_dict=True)
    return value


def issue_debug_lease(
    workspace_id: str,
    workflow_id: str,
    *,
    ttl_seconds: int = 600,
    agent_provider: str | None = None,
) -> dict[str, Any]:
    """Fetch a short-lived cloud vault lease for locally debugging one workflow.

    With agent_provider, the lease also carries the workspace's agent credential
    for that provider, held exclusively until complete_debug_agent_lease().
    """
    body: dict[str, Any] = {"ttl_seconds": ttl_seconds}
    if agent_provider is not None:
        body["agent_provider"] = agent_provider
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflows/{workflow_id}/debug-lease",
        method="POST",
        body=body,
    )
    _raise_for_status(status, value, 200, "could not issue a debug lease", require_dict=True)
    return value


def renew_debug_agent_lease(
    workspace_id: str, workflow_id: str, job_id: str, token: str, ttl_seconds: int
) -> None:
    """Extend a debug run's agent lease so it stays exclusive while the run lasts."""
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflows/{workflow_id}/debug-lease/agent/{job_id}/renew",
        method="POST",
        body={"token": token, "ttl_seconds": ttl_seconds},
    )
    _raise_for_status(status, value, 200, "could not renew the debug agent lease")


def complete_debug_agent_lease(
    workspace_id: str,
    workflow_id: str,
    job_id: str,
    token: str,
    status_value: str,
    *,
    agent_credential: Any = None,
    expected_credential_version: int | None = None,
) -> None:
    """Release a debug run's agent lease, writing back a rotated credential."""
    body: dict[str, Any] = {"token": token, "status": status_value}
    if agent_credential is not None:
        body["agent_credential"] = agent_credential
        body["expected_credential_version"] = expected_credential_version
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflows/{workflow_id}/debug-lease/agent/{job_id}/complete",
        method="POST",
        body=body,
    )
    _raise_for_status(status, value, 200, "could not release the debug agent lease")


def rotate_debug_vault_credential(
    workspace_id: str,
    workflow_id: str,
    lease_id: str,
    path: str,
    expected_version: int,
    secrets: dict[str, str],
) -> int:
    """Save secret fields a provider rotated during a run with this Vault lease.

    Returns the credential's new version; a stale `expected_version` is refused
    so a concurrent rotation is never overwritten.
    """
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflows/{workflow_id}/debug-lease/vault/{lease_id}/rotate",
        method="POST",
        body={"path": path, "expected_version": expected_version, "secrets": secrets},
    )
    _raise_for_status(status, value, 200, f"could not save the rotated secret for {path}")
    return int(value["version"])


def vault_request(workspace_id: str, operation: str, **values: Any) -> dict[str, Any] | list[Any]:
    """Perform provider-neutral credential management through OutcomeCI Vault."""
    base = f"/workspaces/{workspace_id}/vault"
    method, path, body = "GET", base, None
    expected = {200}
    if operation == "put":
        method, path, expected = "POST", f"{base}/secrets", {201}
        body = {
            "path": values["path"],
            "display_name": values["display_name"],
            "value": values["value"],
            "workflow_ids": values.get("workflow_ids", []),
        }
    elif operation == "put_credential":
        method, path, expected = "POST", f"{base}/credentials", {201}
        credential = values["credential"]
        body = {
            "path": values["path"],
            "display_name": values["display_name"],
            "service": values["provider"],
            "credential_type": credential["credential_type"],
            "configuration": credential["configuration"],
            "secrets": credential["secrets"],
            "workflow_ids": values.get("workflow_ids", []),
        }
    elif operation == "rotate":
        method, path, body = (
            "POST",
            f"{base}/secrets/{values['entry_id']}/rotate",
            {"value": values["value"]},
        )
    elif operation == "grant":
        method, path, body = (
            "PUT",
            f"{base}/entries/{values['entry_id']}/grants",
            {"workflow_ids": values.get("workflow_ids", [])},
        )
    elif operation == "revoke":
        method, path, expected = "DELETE", f"{base}/entries/{values['entry_id']}", {204}
    status, result = _authorized_request(path, method=method, body=body)
    _raise_for_status(status, result, expected, f"Vault request failed ({status})")
    return result


def storage_directory(
    workspace_id: str, path: str = "", cursor: str | None = None
) -> dict[str, Any]:
    """One page of a workspace storage folder: its files and subfolders."""
    query = str(
        httpx.QueryParams({"path": path, "limit": "200", **({"cursor": cursor} if cursor else {})})
    )
    status, value = _authorized_request(f"/workspaces/{workspace_id}/storage?{query}")
    _raise_for_status(status, value, 200, "could not list workspace storage", require_dict=True)
    return value


def storage_view_link(workspace_id: str, object_id: str, version_id: str | None) -> dict[str, Any]:
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/storage/view-link",
        method="POST",
        body={"object_id": object_id, "version_id": version_id},
    )
    _raise_for_status(status, value, 200, "could not open a stored file", require_dict=True)
    return value


def download_object(workspace_id: str, entry: dict[str, Any]) -> bytes:
    """A stored file's bytes, through a short-lived view link."""
    link = storage_view_link(workspace_id, entry["object_id"], entry.get("version_id"))
    try:
        response = httpx.get(link["url"], timeout=60, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise CloudRequestError(f"could not download a stored file: {exc}", None) from exc
    if response.status_code != 200:
        raise CloudRequestError(
            f"could not download a stored file (HTTP {response.status_code})", response.status_code
        )
    return response.content


def list_workflows(workspace_id: str) -> list[dict[str, Any]]:
    status, value = _authorized_request(f"/workspaces/{workspace_id}/workflows")
    _raise_for_status(status, value, 200, "could not list workflows")
    return value if isinstance(value, list) else []


def list_workflow_runs(workspace_id: str, workflow_id: str) -> list[dict[str, Any]]:
    status, value = _authorized_request(f"/workspaces/{workspace_id}/workflows/{workflow_id}/runs")
    _raise_for_status(status, value, 200, "could not list workflow runs")
    return value if isinstance(value, list) else []
