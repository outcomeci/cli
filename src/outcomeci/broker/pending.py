"""Drive one managed call from a pending answer to a terminal state.

A managed provider's `connector-call` may answer `pending`: the platform has
accepted the job and the runner polls the same `request_id` until it
completes, is cancelled, or the step's deadline passes. `poll` is a pure
loop over injected `call`, `cancel`, `clock` and `sleep`; it never touches
the journal, a lock, a thread or the api client. The caller records every
attempt it reports, under its own lock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

POLL_FLOOR_SECONDS = 5
POLL_CAP_SECONDS = 60
DEFAULT_RETRY_AFTER_SECONDS = 10

PENDING = "pending"
COMPLETED = "completed"
CANCELLED = "cancelled"
TIMEOUT = "timeout"
RUN_CANCELLED = "run_cancelled"


@dataclass
class Attempt:
    """One request made for a managed call: a `connector-call` or a cancel."""

    kind: str  # "call" | "cancel"
    requested_at: str  # UTC ISO
    state: str | None  # pending | completed | cancelled | None (error)
    retry_after_seconds: int | None
    http_status: int | None
    error: str | None
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def wait_seconds(retry_after: Any, remaining: float | None = None) -> float:
    """How long to sleep before the next poll: the api's hint within the
    floor and cap, clipped to the remaining budget so the deadline is never
    overshot by more than one floor."""
    hint = (
        retry_after
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
        else None
    )
    if hint is None or hint <= 0:
        hint = DEFAULT_RETRY_AFTER_SECONDS
    wait = float(min(max(hint, POLL_FLOOR_SECONDS), POLL_CAP_SECONDS))
    if remaining is not None:
        wait = max(min(wait, remaining), 0.0)
    return wait


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _retryable(error: BaseException) -> bool:
    return bool(getattr(error, "retryable", False))


def request(
    call: Callable[[], dict[str, Any]], *, clock: Callable[[], float] = time.monotonic
) -> tuple[Attempt, dict[str, Any] | None, BaseException | None]:
    """Send one `connector-call` and describe it as an attempt. Returns the
    attempt with either the response or the error that ended it."""
    started = clock()
    attempt = Attempt(
        kind="call",
        requested_at=_now(),
        state=None,
        retry_after_seconds=None,
        http_status=None,
        error=None,
        duration_ms=0,
    )
    try:
        response = call()
    except Exception as exc:  # noqa: BLE001 - described, then the caller decides
        attempt.error = str(exc)
        attempt.duration_ms = int((clock() - started) * 1000)
        return attempt, None, exc
    attempt.duration_ms = int((clock() - started) * 1000)
    if isinstance(response, dict):
        attempt.state = response.get("state")
        retry_after = response.get("retry_after_seconds")
        attempt.retry_after_seconds = retry_after if isinstance(retry_after, int) else None
        status = response.get("status")
        attempt.http_status = status if isinstance(status, int) else None
    return attempt, response, None


def poll(
    first: dict[str, Any],
    *,
    call: Callable[[], dict[str, Any]],
    cancel: Callable[[], Any] | None,
    deadline: float | None,
    stop: Callable[[], bool] | None,
    record: Callable[[Attempt], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, dict[str, Any]]:
    """Return `(outcome, response)` for a call whose first answer was pending.

    `outcome` is `completed`, `cancelled` (the api cancelled the job),
    `timeout` (the deadline passed; one cancel was sent) or `run_cancelled`
    (`stop()` became true; one cancel was sent). `response` is the last
    answer from the api. `call()` sends one more `connector-call` for the
    same request id; `cancel()` sends the one cancel request. A retryable
    error from `call` is one more attempt and the same backoff; any other
    error propagates. A failing cancel is recorded and changes nothing.
    """
    response = first

    def send_cancel() -> None:
        if cancel is None:
            return
        started = clock()
        attempt = Attempt(
            kind="cancel",
            requested_at=_now(),
            state=None,
            retry_after_seconds=None,
            http_status=None,
            error=None,
            duration_ms=0,
        )
        try:
            answer = cancel()
            if isinstance(answer, dict):
                attempt.state = answer.get("state")
        except Exception as exc:  # noqa: BLE001 - journalled, never fatal
            attempt.error = str(exc)
        attempt.duration_ms = int((clock() - started) * 1000)
        record(attempt)

    def stopped() -> str | None:
        if stop is not None and stop():
            return RUN_CANCELLED
        if deadline is not None and clock() >= deadline:
            return TIMEOUT
        return None

    while True:
        reason = stopped()
        if reason is not None:
            send_cancel()
            return reason, response
        remaining = None if deadline is None else max(deadline - clock(), 0.0)
        sleep(wait_seconds(response.get("retry_after_seconds"), remaining))
        reason = stopped()
        if reason is not None:
            send_cancel()
            return reason, response
        attempt, answer, error = request(call, clock=clock)
        record(attempt)
        if error is not None:
            if _retryable(error):
                continue
            raise error
        response = answer or {}
        if response.get("state") == COMPLETED:
            return COMPLETED, response
        if response.get("state") == CANCELLED:
            return CANCELLED, response
