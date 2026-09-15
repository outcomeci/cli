"""Isolated real-Codex SQS proof; no product repositories mounted writable."""

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path


def main():
    proof = Path(__file__).with_name("verify-local-webhooks.py").resolve()
    spec = importlib.util.spec_from_file_location("webhook_proof", proof)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    private = Path(tempfile.mkdtemp(prefix="oci-sqs-agent-auth-"))
    auth = private / "proof-auth.json"
    auth.write_text(json.dumps(module.identity()))
    auth.chmod(0o600)
    os.chmod(private, 0o700)
    # Auth is copied into ephemeral CODEX_HOME; no session or token writes can
    # mutate the user's local Codex installation. All run artifacts live /tmp.
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        str(os.getuid()),
        "--security-opt",
        "seccomp=unconfined",
        "--security-opt",
        "apparmor=unconfined",
        "--security-opt",
        "systempaths=unconfined",
        "-v",
        f"{proof}:/proof.py:ro",
        "-v",
        f"{auth}:/proof-auth.json:ro",
        "-v",
        f"{Path.home() / '.codex/auth.json'}:/codex-auth.json:ro",
        "--entrypoint",
        "/bin/sh",
        "outcomeci-outcome:milestone-local",
        "-c",
        "export CODEX_HOME=$(mktemp -d /tmp/oci-codex-XXXXXX); "
        'cp /codex-auth.json "$CODEX_HOME/auth.json"; '
        "python /proof.py --real-agent --auth-file /proof-auth.json",
    ]
    try:
        subprocess.run(command, check=True, timeout=240)
    finally:
        auth.unlink()
        private.rmdir()


if __name__ == "__main__":
    main()
