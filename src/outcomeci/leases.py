"""Resolve a run's credentials from a Vault lease, and write rotated secrets back.

A lease holds the values of the credentials granted to one workflow. When a
provider rotates a secret during the run, such as a refresh token that is
revoked once used, `rotate` persists the new value through `on_rotate`
before the run uses it again, then keeps it for the rest of the run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .process import ExecutionError

OnRotate = Callable[[str, dict[str, str]], None]


class LeaseResolver:
    def __init__(
        self,
        values: dict[str, Any],
        expires_at: datetime | str,
        *,
        on_rotate: OnRotate | None = None,
        error: type[Exception] = ExecutionError,
        expired: str = "the run's credential lease expired; run the command again",
        not_granted: str = (
            "credential {path!r} is not granted to this workflow; grant it with `oci vault grant`"
        ),
    ) -> None:
        self.values = values
        self.expires = (
            datetime.fromisoformat(expires_at) if isinstance(expires_at, str) else expires_at
        )
        self.on_rotate = on_rotate
        self.error = error
        self.expired = expired
        self.not_granted = not_granted
        # Only a lease whose rotations can be persisted offers `rotate`.
        self.rotate = self._rotate if on_rotate is not None else None

    def __call__(self, reference: str) -> Any:
        if datetime.now(UTC) >= self.expires:
            raise self.error(self.expired)
        if not reference.startswith("vault:"):
            raise self.error("cloud credentials must use vault references")
        path = reference.removeprefix("vault:")
        if path not in self.values:
            raise self.error(self.not_granted.format(path=path))
        return self.values[path]

    def _rotate(self, reference: str, secrets: dict[str, str]) -> None:
        path = reference.removeprefix("vault:")
        current = self.values.get(path)
        if not isinstance(current, dict) or not isinstance(current.get("secrets"), dict):
            raise ExecutionError(f"Vault entry {path} is not a typed credential")
        assert self.on_rotate is not None
        self.on_rotate(path, secrets)
        self.values[path] = {**current, "secrets": {**current["secrets"], **secrets}}
