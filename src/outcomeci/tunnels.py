"""Private, foreground-supervised frpc sessions. Never expose credentials to agents."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import ssl
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from .cloud import _authorized_request, credentials_path
from .process import ExecutionError

VERSION = "0.71.0"
CHECKSUMS = {
    ("linux", "amd64"): "84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716",
    ("linux", "arm64"): "f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266",
    ("darwin", "amd64"): "1b1b4e2f1836e21e8733f1dddaacd4ed9ae67d7dbee39046b9d7b7eda6253637",
    ("darwin", "arm64"): "45be02b186860d375ed49a8941ae9569628a54bf14e67fc36b29c98c99dabcc6",
}


def validate_target(target: str) -> int:
    try:
        value = urlparse(target)
        port = value.port
    except ValueError as exc:
        raise ExecutionError("Specify an HTTP loopback target with a port") from exc
    if (
        value.scheme != "http"
        or value.hostname not in {"127.0.0.1", "localhost"}
        or value.username is not None
        or value.password is not None
        or value.path not in {"", "/"}
        or value.query
        or value.fragment
        or port is None
        or not 1024 <= port <= 65535
    ):
        raise ExecutionError(
            "Target must be http://127.0.0.1:PORT (1024–65535), without credentials or paths"
        )
    return port


def _request(
    workspace_id: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    session_id: str | None = None,
) -> dict | None:
    if not workspace_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in workspace_id
    ):
        raise ExecutionError("Invalid workspace identifier")
    suffix = f"?session_id={UUID(session_id)}" if session_id else ""
    status, result = _authorized_request(
        f"/workspaces/{workspace_id}/tunnel{suffix}", method=method, body=body
    )
    if status not in {200, 201}:
        # Do not relay backend bodies or provider diagnostics that could contain credentials.
        raise ExecutionError(f"Tunnel request failed (HTTP {status})")
    return result


def status(workspace_id: str) -> dict | None:
    return _request(workspace_id)


def stop(workspace_id: str) -> dict | None:
    return _request(workspace_id, method="DELETE")


def client_binary() -> Path:
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    digest = CHECKSUMS.get((system, arch))
    if digest is None:
        raise ExecutionError("frpc is supported on Linux and macOS amd64/arm64")
    root = credentials_path().parent / ".broker" / "tunnels"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ExecutionError("Tunnel binary cache must not be a symlink")
    archive = root / f"frp-{VERSION}-{system}-{arch}.tar.gz"
    if archive.exists():
        data = archive.read_bytes()
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(
                f"https://github.com/fatedier/frp/releases/download/v{VERSION}/frp_{VERSION}_{system}_{arch}.tar.gz",
                timeout=60,
            ) as response:
                data = response.read(64 * 1024 * 1024 + 1)
        except OSError as exc:
            raise ExecutionError("Could not download the pinned frpc release") from exc
    if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise ExecutionError("frpc release checksum does not match")
    if not archive.exists():
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    # Extract only the expected regular file, never arbitrary tar paths.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as package:
        member = package.getmember(f"frp_{VERSION}_{system}_{arch}/frpc")
        if not member.isfile():
            raise ExecutionError("Pinned frpc archive is invalid")
        source = package.extractfile(member)
        if source is None:
            raise ExecutionError("Pinned frpc binary is absent")
        binary_data = source.read()
    binary = root / f"frpc-{VERSION}-{system}-{arch}"
    # Always regenerate from the verified archive, not a trusted cache pathname.
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
        output.write(binary_data)
        temporary = Path(output.name)
    temporary.chmod(0o700)
    temporary.replace(binary)
    return binary


def configuration(grant: dict, port: int, *, ca_file: Path | None = None) -> str:
    server_host = grant.get("server_host")
    transport = grant.get("transport")
    if transport == "tcp":
        if server_host not in {"127.0.0.1", "localhost"}:
            raise ExecutionError("Unverified TCP transport is allowed on loopback only")
    elif transport == "wss":
        if (
            not isinstance(server_host, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", server_host)
            or "." not in server_host
            or server_host == "127.0.0.1"
            or ca_file is None
            or not ca_file.is_file()
        ):
            raise ExecutionError(
                "Public WSS transport requires a DNS hostname and trusted CA bundle"
            )
    else:
        raise ExecutionError("Unsupported tunnel transport")
    host = grant.get("hostname")
    credential = grant.get("credential")
    server_port = grant.get("server_port")
    if (
        not isinstance(host, str)
        or not isinstance(credential, str)
        or len(credential) < 32
        or not isinstance(server_port, int)
        or not (1024 <= server_port <= 65535 if transport == "tcp" else server_port == 443)
    ):
        raise ExecutionError("Tunnel grant is invalid")
    if grant.get("local_port") != port:
        raise ExecutionError("Tunnel grant does not match the approved target")
    quote = json.dumps
    tls = "transport.tls.enable = true\n"
    if transport == "wss":
        tls += f'transport.protocol = "wss"\ntransport.tls.serverName = {quote(server_host)}\ntransport.tls.trustedCaFile = {quote(str(ca_file))}\n'
    return (
        f"serverAddr = {quote(server_host)}\nserverPort = {server_port}\n"
        "loginFailExit = true\n"
        + tls
        + "transport.heartbeatInterval = 2\ntransport.heartbeatTimeout = 8\n"
        f"metadatas.hostname = {quote(host)}\nmetadatas.credential = {quote(credential)}\n"
        'log.to = "console"\nlog.level = "error"\n'
        f'[[proxies]]\nname = {quote(host)}\ntype = "http"\n'
        f'localIP = "127.0.0.1"\nlocalPort = {port}\ncustomDomains = [{quote(host)}]\n'
    )


def start(workspace_id: str, target: str, *, ttl_seconds: int = 900, public: bool = False) -> None:
    if not public:
        raise ExecutionError("A tunnel exposes the target publicly. Confirm with --public")
    port = validate_target(target)
    if not 30 <= ttl_seconds <= 3600:
        raise ExecutionError("Tunnel duration must be 30–3600 seconds")
    binary = client_binary()
    grant = _request(
        workspace_id, method="POST", body={"local_port": port, "ttl_seconds": ttl_seconds}
    )
    if not isinstance(grant, dict):
        raise ExecutionError("Tunnel grant is absent")
    process = None
    try:
        root = credentials_path().parent / ".broker" / "tunnels"
        with tempfile.TemporaryDirectory(prefix="session-", dir=root) as directory:
            ca_file = None
            if grant.get("transport") == "wss":
                ca_file = Path(directory) / "trusted-ca.pem"
                certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
                if not certificates:
                    raise ExecutionError("No system trust roots are available")
                ca_file.write_text(
                    "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in certificates),
                    encoding="ascii",
                )
                ca_file.chmod(0o600)
            content = configuration(grant, port, ca_file=ca_file)
            path = Path(directory) / "frpc.toml"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(content)
            # frpc never gets vault/agent credentials from the parent's environment.
            environment = {
                k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TZ", "SYSTEMROOT"}
            }
            process = subprocess.Popen(
                [str(binary), "-c", str(path)],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(
                f"Tunnel session: {grant['hostname']} (expires {grant['expires_at']})", flush=True
            )
            print(
                f"HTTPS URL: https://{grant['hostname']}"
                if grant.get("transport") == "wss"
                else "Local gateway: http://127.0.0.1:7402; send the assigned Host header. Ctrl+C stops exposure.",
                flush=True,
            )
            try:
                result = process.wait(timeout=ttl_seconds)
                if result != 0:
                    raise ExecutionError("frpc stopped before session completion")
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        try:
            _request(workspace_id, method="DELETE", session_id=str(grant["id"]))
        except ExecutionError:
            print(
                "Could not confirm revocation; the bounded server lease still expires.", flush=True
            )
