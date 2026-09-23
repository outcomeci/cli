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
from .security import private_path


def _raise_for_status(
    status: int, value: Any, expected: int | set[int], fallback: str, *, require_dict: bool = False
) -> None:
    ok = status in expected if isinstance(expected, set) else status == expected
    if require_dict:
        ok = ok and isinstance(value, dict)
    if not ok:
        detail = value.get("detail") if isinstance(value, dict) else None
        raise ExecutionError(str(detail or fallback))


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


def start_email_trigger_proof(workspace_id: str) -> dict[str, Any]:
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/email-trigger-proofs", method="POST", body={}
    )
    _raise_for_status(status, value, 202, "could not start email trigger proof", require_dict=True)
    return value


def get_email_trigger_proof(workspace_id: str, proof_id: str) -> dict[str, Any]:
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/email-trigger-proofs/{proof_id}"
    )
    _raise_for_status(status, value, 200, "could not read email trigger proof", require_dict=True)
    return value


def sync_workflow(
    path: Path,
    workspace_id: str,
    name: str | None,
    mode: str,
    *,
    patch_path: Path | None = None,
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
    workflow_name = name or str((document.get("metadata") or {}).get("name") or "").strip()
    if not workflow_name:
        raise ExecutionError("workflow name is required; set metadata.name or pass --name")
    lineage: dict[str, Any] = {}
    expected_parent_sha256 = None
    if patch_path is not None:
        if mode != "version":
            raise ExecutionError("a lineage patch can only be synced as a new version")
        try:
            patch = yaml.safe_load(patch_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ExecutionError(f"could not read workflow patch: {exc}") from exc
        if not isinstance(patch, dict) or patch.get("kind") != "OutcomeWorkflowPatch":
            raise ExecutionError("lineage patch must be an OutcomeWorkflowPatch")
        metadata = patch.get("metadata", {})
        expected_parent_sha256 = metadata.get("parentContentSha256")
        if not isinstance(expected_parent_sha256, str):
            raise ExecutionError("lineage patch has no parent content digest")
        lineage = {
            "type": "learned_operation",
            "parent_workflow_revision": metadata.get("parentRevision"),
            "patch": patch_path.name,
            "reason": metadata.get("reason"),
            "derived_from": metadata.get("derivedFrom", {}),
        }
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
            "expected_parent_sha256": expected_parent_sha256,
            "lineage": lineage,
        },
    )
    _raise_for_status(status, value, 201, "workflow synchronization failed", require_dict=True)
    return value


def issue_debug_lease(
    workspace_id: str, workflow_id: str, *, invocation_id: str | None = None, ttl_seconds: int = 600
) -> dict[str, Any]:
    """Fetch a short-lived cloud vault lease for locally debugging one workflow."""
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflows/{workflow_id}/debug-lease",
        method="POST",
        body={"invocation_id": invocation_id, "ttl_seconds": ttl_seconds},
    )
    _raise_for_status(status, value, 200, "could not issue a debug lease", require_dict=True)
    return value


def complete_debug_lease(
    workspace_id: str, workflow_id: str, invocation_id: str, status_value: str
) -> None:
    status, value = _authorized_request(
        f"/workspaces/{workspace_id}/workflows/{workflow_id}/debug-lease/{invocation_id}/complete",
        method="POST",
        body={"status": status_value},
    )
    _raise_for_status(status, value, 200, "could not resolve the debug-claimed invocation")


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
        credential_type = values["credential_type"]
        inferred_secret = {
            "api_key": "api_key",
            "auth_header": "value",
            "oauth2": "client_secret",
            "oidc": "client_secret",
        }[credential_type]
        configuration = {
            key: values.get(key)
            for key in (
                "header_name",
                "prefix",
                "scheme",
                "token_url",
                "issuer_url",
                "client_id",
                "grant_type",
                "audience",
            )
            if values.get(key) is not None
        }
        if credential_type == "auth_header":
            configuration.setdefault("header_name", "Authorization")
            configuration.setdefault("scheme", "Bearer")
        elif credential_type == "api_key":
            configuration.setdefault("header_name", "X-API-Key")
        if values.get("scopes"):
            configuration["scopes"] = values["scopes"]
        method, path, expected = "POST", f"{base}/credentials", {201}
        body = {
            "path": values["path"],
            "display_name": values["display_name"],
            "service": values["provider"],
            "credential_type": credential_type,
            "configuration": configuration,
            "secrets": values.get("secrets")
            or {values.get("secret_name") or inferred_secret: values["value"]},
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
