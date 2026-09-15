"""Verify the real filesystem boundary without invoking a model or using secrets."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from outcomeci import process


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="oci-sandbox-proof-") as directory:
        workspace = Path(directory)
        (workspace / ".outcomeci" / ".broker").mkdir(parents=True)
        (workspace / ".env").write_text("synthetic-secret")
        (workspace / ".outcomeci" / "vault.enc").write_text("synthetic-ciphertext")
        (workspace / ".outcomeci" / ".broker" / "journal.json").write_text("private-map")
        calls = []
        with patch.object(
            process,
            "command",
            side_effect=lambda argv, **kwargs: calls.append(argv) or process.Result(0, "", ""),
        ):
            process.invoke(
                "codex",
                None,
                "unused",
                workspace,
                30,
                allow_local_auth=True,
                writable_paths=[],
            )
        arguments = calls[0]
        boundary = arguments[: arguments.index("--") + 1]
        check = """
import pathlib, socket
for name in ['.env', '.outcomeci/vault.enc']:
    try:
        assert pathlib.Path(name).read_text() == ''
    except PermissionError:
        pass
assert not pathlib.Path('.outcomeci/.broker/journal.json').exists()
assert not (pathlib.Path.home() / '.ssh').exists()
assert pathlib.Path('/etc/resolv.conf').is_file()
socket.getaddrinfo('example.com', 443)
print('PASS: DNS, Vault/environment masks, private broker journal and SSH isolation')
"""
        result = subprocess.run(
            [*boundary, sys.executable, "-c", check],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
