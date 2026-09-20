"""Bounded subprocess execution without shell interpolation."""

from __future__ import annotations

import fcntl
import os
import pty
import selectors
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

PTY_ROWS = 50
PTY_COLUMNS = 500


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    on_output: Callable[[str], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    terminal: bool = False,
    input_provider: Callable[[], str | None] | None = None,
) -> ProcessResult:
    master = slave = None
    if terminal:
        master, slave = pty.openpty()
        # openpty() reports a 0x0 winsize by default; terminal UIs that lay out
        # or wrap content to that width (e.g. a boxed credential display) will
        # wrap or clip long output, silently truncating anything we later parse
        # out of the captured transcript. A wide, explicit size avoids that.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0))
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        streams = [(master, "stdout")]
    else:
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        assert child.stdout is not None and child.stderr is not None
        streams = [(child.stdout.fileno(), "stdout"), (child.stderr.fileno(), "stderr")]
    prior_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def forward(signum, _frame):
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signum)

    for sig in prior_handlers:
        signal.signal(sig, forward)
    selector = selectors.DefaultSelector()
    for descriptor, name in streams:
        selector.register(descriptor, selectors.EVENT_READ, name)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            if on_tick:
                on_tick()
            if input_provider and master is not None:
                supplied = input_provider()
                if supplied is not None:
                    os.write(master, b"\x1b[200~" + supplied.encode() + b"\x1b[201~\r")
            if time.monotonic() >= deadline:
                raise TimeoutError("agent process timed out")
            for key, _ in selector.select(timeout=min(0.5, max(0, deadline - time.monotonic()))):
                try:
                    chunk = os.read(key.fd, 8192)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                target = buffers[key.data]
                target.extend(chunk)
                if len(target) > 256_000:
                    del target[:-256_000]
                if on_output:
                    on_output(chunk.decode("utf-8", errors="replace"))
        returncode = child.wait(timeout=max(0.1, deadline - time.monotonic()))
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise TimeoutError("agent process timed out") from error
    except BaseException:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise
    finally:
        selector.close()
        for sig, handler in prior_handlers.items():
            signal.signal(sig, handler)
        if master is not None:
            os.close(master)
        else:
            assert child.stdout is not None and child.stderr is not None
            child.stdout.close()
            child.stderr.close()
    return ProcessResult(
        returncode,
        buffers["stdout"].decode(errors="replace"),
        buffers["stderr"].decode(errors="replace"),
    )
