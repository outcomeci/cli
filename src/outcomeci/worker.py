"""Detached, durable filesystem outcome worker."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .local import continue_run, respond, retry
from .security import atomic_write_json


def _write(path: Path, value: dict) -> None:
    atomic_write_json(path, value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("continue", "retry", "respond"))
    parser.add_argument("run_id")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--interaction-id")
    parser.add_argument("--message")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    worker_path = args.workspace / ".outcomeci" / "outcomes" / args.run_id / "worker.json"
    worker = {}
    for _ in range(100):
        try:
            worker = json.loads(worker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            worker = {}
        if worker.get("pid") == os.getpid():
            break
        time.sleep(0.01)
    if worker.get("worker_id") != args.worker_id:
        return 2
    worker.update(
        {"pid": os.getpid(), "status": "running", "running_at": datetime.now(UTC).isoformat()}
    )
    _write(worker_path, worker)
    exit_code = 0
    try:
        if args.operation == "continue":
            result = continue_run(args.workspace, args.config, args.run_id, args.approve)
        elif args.operation == "retry":
            result = retry(args.workspace, args.config, args.run_id)
        else:
            result = respond(
                args.workspace,
                args.config,
                args.run_id,
                args.interaction_id or "",
                args.message or "",
                approve=args.approve,
                reject=args.reject,
            )
        worker["result_status"] = result.get("status")
    except Exception as exc:  # The durable record must survive every worker failure.
        exit_code = 1
        worker["error"] = str(exc)
    worker.update(
        {
            "status": "completed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
    _write(worker_path, worker)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
