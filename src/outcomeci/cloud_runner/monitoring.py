"""Sentry error monitoring for the cloud runner process.

A separate project/DSN from the api's own Sentry (api/app/monitoring.py) --
this process runs customer workflow content and live, single-use agent
credentials rather than API request data, so it gets its own scrubbing and
its own blast radius if something in the pipeline ever misbehaves.

Inert by default: only active when OUTCOMECI_RUNNER_SENTRY_DSN is set, which
happens only in the ECS runner task definition. Local and non-cloud `oci`
usage never sets it and never imports sentry_sdk.
"""

from __future__ import annotations

import os
from typing import Any

_enabled = False


def _strip_sensitive_context(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Keep exception diagnostics without customer workflow content or credentials."""
    event.pop("request", None)
    event.pop("user", None)
    event.pop("breadcrumbs", None)
    return event


def init_exception_monitoring() -> None:
    global _enabled
    dsn = os.environ.get("OUTCOMECI_RUNNER_SENTRY_DSN", "").strip()
    if not dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("OUTCOMECI_ENV", "production"),
        release=os.environ.get("OUTCOMECI_CLI_VERSION") or None,
        send_default_pii=False,
        include_local_variables=False,
        max_breadcrumbs=0,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        enable_logs=False,
        before_send=_strip_sensitive_context,
    )
    _enabled = True


def capture_exception(error: BaseException, **tags: str) -> None:
    """No-op unless init_exception_monitoring() found a DSN."""
    if not _enabled:
        return
    import sentry_sdk

    if tags:
        with sentry_sdk.push_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, value)
            sentry_sdk.capture_exception(error)
    else:
        sentry_sdk.capture_exception(error)
