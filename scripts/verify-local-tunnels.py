"""Real local API/frps proof; creates and removes only its two fixture workspaces.

No user workspace changes, external messages, public publication or credential logs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from outcomeci import cloud, tunnels


class Target(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        if self.path == "/stream":
            try:
                for _ in range(300):
                    self.wfile.write(b"streaming\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.wfile.write(b"outcomeci-live-frp-proof")


def identity() -> dict:
    spec = importlib.util.spec_from_file_location(
        "webhook_proof", Path(__file__).with_name("verify-local-webhooks.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.identity()


def main() -> None:
    who = identity()
    ids = []
    processes = []
    prior = os.environ.get("OUTCOMECI_CONFIG_HOME")
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target.daemon_threads = True
    threading.Thread(target=target.serve_forever, daemon=True).start()
    port = target.server_port
    results = []
    with tempfile.TemporaryDirectory(prefix="oci-frp-proof-") as directory:
        os.environ["OUTCOMECI_CONFIG_HOME"] = directory
        cloud._write_credentials(
            {"api_url": "http://127.0.0.1:8000", "access_token": who["access_token"]}
        )
        api = httpx.Client(
            base_url="http://127.0.0.1:8000/v1",
            headers={"Authorization": "Bearer " + who["access_token"]},
            trust_env=False,
            timeout=10,
        )
        public = httpx.Client(base_url="http://127.0.0.1:7402", trust_env=False, timeout=5)
        try:
            for name in ("a", "b"):
                response = api.post(
                    "/workspaces", json={"name": f"local-frp-proof-{name}-{time.time_ns()}"}
                )
                assert response.status_code == 201, (
                    f"workspace setup failed: {response.status_code}"
                )
                ids.append(response.json()["id"])
            binary = tunnels.client_binary()

            def launch(grant: dict, *, hostname: str | None = None) -> subprocess.Popen:
                modified = {**grant, "hostname": hostname or grant["hostname"]}
                path = Path(directory) / f"client-{len(processes)}.toml"
                path.write_text(tunnels.configuration(modified, port), encoding="utf-8")
                path.chmod(0o600)
                process = subprocess.Popen(
                    [str(binary), "-c", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                processes.append(process)
                return process

            def wait_forwarded(grant: dict) -> None:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    response = public.get("/", headers={"Host": grant["hostname"]})
                    if response.status_code == 200 and response.text == "outcomeci-live-frp-proof":
                        return
                    time.sleep(0.2)
                raise AssertionError("frp failed to forward")

            grant = api.post(
                f"/workspaces/{ids[0]}/tunnel", json={"local_port": port, "ttl_seconds": 120}
            ).json()
            other = api.post(
                f"/workspaces/{ids[1]}/tunnel", json={"local_port": port, "ttl_seconds": 120}
            ).json()
            first = launch(grant)
            wait_forwarded(grant)
            results.append("real HTTP forwarding")
            assert "credential" not in api.get(f"/workspaces/{ids[0]}/tunnel").json()
            assert public.get("/", headers={"Host": "not-assigned.local"}).status_code == 503
            assert (
                public.get(
                    "/", headers={"Host": grant["hostname"], "Upgrade": "websocket"}
                ).status_code
                == 400
            )
            substituted = launch(grant, hostname=other["hostname"])
            assert substituted.wait(timeout=10) != 0
            results.append("credential/hostname isolation and unsupported upgrades")
            first.terminate()
            first.wait(timeout=5)
            time.sleep(0.5)
            launch(grant)
            wait_forwarded(grant)
            results.append("reconnect without lease extension")
            with public.stream("GET", "/stream", headers={"Host": grant["hostname"]}) as response:
                iterator = response.iter_lines()
                assert next(iterator) == "streaming"
                start = time.monotonic()
                assert api.delete(f"/workspaces/{ids[0]}/tunnel").status_code == 200
                try:
                    for _ in iterator:
                        assert time.monotonic() - start < 5, "stream remained open after revocation"
                except httpx.RemoteProtocolError:
                    pass
                assert time.monotonic() - start < 5
            assert public.get("/", headers={"Host": grant["hostname"]}).status_code == 503
            results.append("active streaming revocation under five seconds")
            expiring = api.post(
                f"/workspaces/{ids[0]}/tunnel", json={"local_port": port, "ttl_seconds": 30}
            ).json()
            launch(expiring)
            wait_forwarded(expiring)
            # An older supervisor must never revoke a replacement session.
            assert (
                api.delete(
                    f"/workspaces/{ids[0]}/tunnel", params={"session_id": grant["id"]}
                ).status_code
                == 200
            )
            wait_forwarded(expiring)
            results.append("old session cleanup cannot revoke replacement")
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if api.get(f"/workspaces/{ids[0]}/tunnel").json()["status"] == "expired":
                    break
                time.sleep(1)
            assert public.get("/", headers={"Host": expiring["hostname"]}).status_code == 503
            expired = launch(expiring)
            assert expired.wait(timeout=10) != 0
            results.append("lease expiry and expired reconnect denial")
            supervisor = subprocess.Popen(
                [
                    str(Path(sys.executable).parent / "oci"),
                    "tunnel",
                    "start",
                    "--workspace",
                    ids[0],
                    "--target",
                    f"http://127.0.0.1:{port}",
                    "--public",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(supervisor)
            deadline = time.monotonic() + 10
            current = None
            while time.monotonic() < deadline:
                current = api.get(f"/workspaces/{ids[0]}/tunnel").json()
                if current and current["id"] != expiring["id"] and current["status"] == "connected":
                    break
                time.sleep(0.2)
            assert current and current["id"] != expiring["id"], "supervisor did not start"
            wait_forwarded(current)
            supervisor.send_signal(signal.SIGINT)
            assert supervisor.wait(timeout=10) == 0
            assert api.get(f"/workspaces/{ids[0]}/tunnel").json()["status"] == "revoked"
            assert not list((Path(directory) / ".broker/tunnels").glob("session-*"))
            results.append(
                "installed CLI supervision, interrupt revocation and private config cleanup"
            )
            print(json.dumps({"passed": results}, indent=2))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            for workspace_id in ids:
                response = api.delete(f"/workspaces/{workspace_id}")
                assert response.status_code == 204, "fixture cleanup failed"
            api.close()
            public.close()
            target.shutdown()
            if prior is None:
                os.environ.pop("OUTCOMECI_CONFIG_HOME", None)
            else:
                os.environ["OUTCOMECI_CONFIG_HOME"] = prior


if __name__ == "__main__":
    main()
