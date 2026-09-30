"""Best-effort resource telemetry for one cloud workflow execution.

The report intentionally contains numeric counters only. Collection failures are
isolated per source so telemetry can never change the workflow result.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Literal, NotRequired, Required, TypedDict

SAMPLE_INTERVAL_SECONDS = 1.0
MAX_COUNTER = (1 << 63) - 1


class ResourceUsageReport(TypedDict, total=False):
    schema_version: Required[Literal[1]]
    sample_count: Required[int]
    sampled_milliseconds: Required[int]
    cpu_usage_usec: NotRequired[int]
    cpu_throttled_usec: NotRequired[int]
    cpu_nr_throttled: NotRequired[int]
    cpu_peak_millicores: NotRequired[int]
    cpu_limit_millicores: NotRequired[int]
    memory_peak_bytes: NotRequired[int]
    memory_limit_bytes: NotRequired[int]
    workspace_peak_bytes: NotRequired[int]
    workspace_final_bytes: NotRequired[int]
    workspace_limit_bytes: NotRequired[int]


def _bounded(value: int | None) -> int | None:
    if value is None or value < 0 or value > MAX_COUNTER:
        return None
    return value


def _read_int(path: Path) -> int | None:
    try:
        return _bounded(int(path.read_text(encoding="ascii").strip()))
    except (OSError, UnicodeError, ValueError):
        return None


def _read_cpu_stat(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            value = _bounded(int(fields[1]))
        except ValueError:
            continue
        if value is not None:
            values[fields[0]] = value
    return values


def _cgroup_directory(root: Path, membership: Path) -> Path | None:
    """Resolve this process's cgroup v2 directory without leaving `root`."""
    try:
        lines = membership.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        if not line.startswith("0::"):
            continue
        relative = line.removeprefix("0::").lstrip("/")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        return candidate
    return None


def _cpu_limit_millicores(path: Path) -> int | None:
    try:
        fields = path.read_text(encoding="ascii").split()
        if len(fields) != 2 or fields[0] == "max":
            return None
        quota, period = int(fields[0]), int(fields[1])
    except (OSError, UnicodeError, ValueError):
        return None
    if quota < 0 or period <= 0:
        return None
    return _bounded(quota * 1000 // period)


def _finite_limit(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if value == "max":
        return None
    try:
        return _bounded(int(value))
    except ValueError:
        return None


def _filesystem_usage(path: Path) -> tuple[int, int] | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    block_size = stats.f_frsize or stats.f_bsize
    used = _bounded(block_size * (stats.f_blocks - stats.f_bfree))
    limit = _bounded(block_size * stats.f_blocks)
    return (used, limit) if used is not None and limit is not None else None


class ResourceUsageSampler:
    """Sample cgroup v2 and execution-filesystem counters at a low cadence."""

    def __init__(
        self,
        execution_root: Path,
        *,
        interval_seconds: float = SAMPLE_INTERVAL_SECONDS,
        cgroup_directory: Path | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        cgroup_membership: Path = Path("/proc/self/cgroup"),
    ) -> None:
        self._execution_root = execution_root
        self._interval_seconds = max(0.01, interval_seconds)
        self._cgroup = cgroup_directory or _cgroup_directory(cgroup_root, cgroup_membership)
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._report: ResourceUsageReport | None = None
        self._sample_count = 0
        self._cpu_baseline: dict[str, int] | None = None
        self._previous_cpu: tuple[int, float] | None = None
        self._cpu_latest: dict[str, int] = {}
        self._cpu_peak_millicores: int | None = None
        self._memory_peak: int | None = None
        self._workspace_baseline: int | None = None
        self._workspace_peak: int | None = None
        self._workspace_final: int | None = None
        self._workspace_limit: int | None = None
        self._cpu_limit: int | None = None
        self._memory_limit: int | None = None

    def start(self) -> None:
        """Start sampling. This method intentionally never raises."""
        try:
            with self._lock:
                if self._started_at is not None:
                    return
                self._started_at = time.monotonic()
            self._sample()
            self._thread = Thread(target=self._sample_until_stopped, daemon=True)
            self._thread.start()
        except Exception:
            # Resource evidence is useful but never part of workflow correctness.
            return

    def stop(self) -> ResourceUsageReport | None:
        """Stop, take a final sample, and return one stable report for retries."""
        try:
            with self._lock:
                if self._stopped_at is not None:
                    return self._report
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
            self._sample()
            with self._lock:
                if self._stopped_at is None:
                    self._stopped_at = time.monotonic()
                    self._report = self._build_report()
                return self._report
        except Exception:
            return None

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._sample()
            except Exception:
                # A transient counter read must not terminate workflow execution.
                continue

    def _sample(self) -> None:
        now = time.monotonic()
        cpu = _read_cpu_stat(self._cgroup / "cpu.stat") if self._cgroup else {}
        memory_current = _read_int(self._cgroup / "memory.current") if self._cgroup else None
        memory_peak = _read_int(self._cgroup / "memory.peak") if self._cgroup else None
        cpu_limit = _cpu_limit_millicores(self._cgroup / "cpu.max") if self._cgroup else None
        memory_limit = _finite_limit(self._cgroup / "memory.max") if self._cgroup else None
        filesystem = _filesystem_usage(self._execution_root)
        measured = bool(cpu) or memory_current is not None or memory_peak is not None
        measured = (
            measured or cpu_limit is not None or memory_limit is not None or filesystem is not None
        )

        with self._lock:
            if measured:
                self._sample_count += 1
            if cpu:
                if self._cpu_baseline is None:
                    self._cpu_baseline = cpu.copy()
                usage = cpu.get("usage_usec")
                if usage is not None:
                    if self._previous_cpu is not None:
                        previous_usage, previous_time = self._previous_cpu
                        elapsed = now - previous_time
                        if usage >= previous_usage and elapsed > 0:
                            peak = _bounded(round((usage - previous_usage) / (elapsed * 1000)))
                            if peak is not None:
                                self._cpu_peak_millicores = max(
                                    self._cpu_peak_millicores or 0, peak
                                )
                    self._previous_cpu = (usage, now)
                self._cpu_latest = cpu
            observed_memory = (
                max(
                    value
                    for value in (memory_current, memory_peak, self._memory_peak)
                    if value is not None
                )
                if any(
                    value is not None for value in (memory_current, memory_peak, self._memory_peak)
                )
                else None
            )
            self._memory_peak = observed_memory
            self._cpu_limit = cpu_limit if cpu_limit is not None else self._cpu_limit
            self._memory_limit = memory_limit if memory_limit is not None else self._memory_limit
            if filesystem is not None:
                used, limit = filesystem
                if self._workspace_baseline is None:
                    self._workspace_baseline = used
                delta = max(0, used - self._workspace_baseline)
                self._workspace_peak = max(self._workspace_peak or 0, delta)
                self._workspace_final = delta
                self._workspace_limit = _bounded(limit)

    def _build_report(self) -> ResourceUsageReport | None:
        if self._sample_count == 0 or self._started_at is None or self._stopped_at is None:
            return None
        report: ResourceUsageReport = {
            "schema_version": 1,
            "sample_count": self._sample_count,
            "sampled_milliseconds": max(0, round((self._stopped_at - self._started_at) * 1000)),
        }
        baseline = self._cpu_baseline or {}
        for source, target in (
            ("usage_usec", "cpu_usage_usec"),
            ("throttled_usec", "cpu_throttled_usec"),
            ("nr_throttled", "cpu_nr_throttled"),
        ):
            latest = self._cpu_latest.get(source)
            initial = baseline.get(source)
            if latest is not None and initial is not None and latest >= initial:
                report[target] = latest - initial  # type: ignore[literal-required]
        optional = {
            "cpu_peak_millicores": self._cpu_peak_millicores,
            "cpu_limit_millicores": self._cpu_limit,
            "memory_peak_bytes": self._memory_peak,
            "memory_limit_bytes": self._memory_limit,
            "workspace_peak_bytes": self._workspace_peak,
            "workspace_final_bytes": self._workspace_final,
            "workspace_limit_bytes": self._workspace_limit,
        }
        for key, value in optional.items():
            bounded = _bounded(value)
            if bounded is not None:
                report[key] = bounded  # type: ignore[literal-required]
        return report if len(report) > 3 else None
