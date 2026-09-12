"""OutcomeCI Cloud device authentication and workflow synchronization."""

from __future__ import annotations

import base64
import json
import os
import stat
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import yaml

from .config import compile_workflow
from .process import ExecutionError


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
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            value = {}
        return exc.code, value
    except OSError as exc:
        raise ExecutionError(f"could not reach OutcomeCI Cloud: {exc}") from exc


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


def sync_workflow(path: Path, workspace_id: str, name: str | None, mode: str) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise ExecutionError(f"workflow file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix not in {".yml", ".yaml", ".json"}:
        raise ExecutionError("workflow must be YAML or JSON")
    compile_workflow(path)
    content = path.read_text(encoding="utf-8")
    document = json.loads(content) if suffix == ".json" else yaml.safe_load(content)
    workflow_name = name or str((document.get("metadata") or {}).get("name") or "").strip()
    if not workflow_name:
        raise ExecutionError("workflow name is required; set metadata.name or pass --name")
    files: dict[str, str] = {}
    support_root = path.parent / ".outcomeci"
    if support_root.is_dir():
        total = 0
        for support in sorted(
            item
            for item in support_root.rglob("*")
            if item.is_file() and "outcomes" not in item.relative_to(support_root).parts
        ):
            content = support.read_bytes()
            total += len(content)
            if len(content) > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
                raise ExecutionError(
                    "workflow support files exceed the 20 MiB synchronization limit"
                )
            files[str(Path(".outcomeci") / support.relative_to(support_root))] = base64.b64encode(
                content
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
    if status != 201:
        detail = value.get("detail") if isinstance(value, dict) else None
        raise ExecutionError(str(detail or "workflow synchronization failed"))
    return value  # type: ignore[return-value]


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
    if status not in expected:
        detail = result.get("detail") if isinstance(result, dict) else None
        raise ExecutionError(str(detail or f"Vault request failed ({status})"))
    return result
