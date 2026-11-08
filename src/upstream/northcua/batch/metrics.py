"""Simple latency and throughput metrics for batch runs."""

from __future__ import annotations

import time
from collections import Counter
from statistics import median

from northcua.telemetry.contracts import FailureCategory


class BatchMetrics:
    """Collect timing metrics without coupling to a metrics backend."""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.sandbox_create_seconds: list[float] = []
        self.prepared_sandbox_seconds: list[float] = []
        self.prepared_sandbox_hits = 0
        self.prepared_sandbox_misses = 0
        self.prepared_sandbox_failures = 0
        self.prepared_sandbox_cleanup_failures = 0
        self.task_seconds: list[float] = []
        self.attempt_failure_categories: Counter[str] = Counter()

    def observe_sandbox_create(self, seconds: float) -> None:
        """Record sandbox creation latency.

        Args:
            seconds: Elapsed wall-clock seconds spent acquiring sandbox capacity.
        """

        self.sandbox_create_seconds.append(seconds)

    def observe_prepared_sandbox(self, seconds: float) -> None:
        """Record background prepared sandbox setup latency.

        Args:
            seconds: Elapsed seconds from reservation to ready sandbox.
        """

        self.prepared_sandbox_seconds.append(seconds)

    def observe_prepared_hit(self) -> None:
        """Record that a claimed task used a prepared sandbox."""

        self.prepared_sandbox_hits += 1

    def observe_prepared_miss(self) -> None:
        """Record that a claimed task fell back to normal acquisition."""

        self.prepared_sandbox_misses += 1

    def observe_prepared_failure(self) -> None:
        """Record a background prepare failure."""

        self.prepared_sandbox_failures += 1

    def observe_prepared_cleanup_failure(self) -> None:
        """Record a cleanup/janitor failure for prepared sandbox capacity."""

        self.prepared_sandbox_cleanup_failures += 1

    def observe_task(self, seconds: float) -> None:
        """Record end-to-end task latency.

        Args:
            seconds: Elapsed wall-clock seconds between controller submission
                and terminal task result.
        """

        self.task_seconds.append(seconds)

    def observe_attempt_failure(self, category: FailureCategory | None) -> None:
        """Record a failed attempt by normalized failure category.

        Args:
            category: Failure category inferred for this attempt, if any.
        """

        if category is not None:
            self.attempt_failure_categories[category.value] += 1

    def summary(self, *, completed_tasks: int) -> dict[str, float | int | dict[str, int]]:
        """Return aggregate latency and throughput values.

        Args:
            completed_tasks: Number of terminal tasks.

        Returns:
            JSON-serializable metrics summary.
        """

        elapsed = max(time.monotonic() - self.started_at, 0.001)
        return {
            "elapsed_seconds": elapsed,
            "completed_tasks": completed_tasks,
            "tasks_per_hour": completed_tasks / elapsed * 3600.0,
            "sandbox_create_p50_seconds": _p50(self.sandbox_create_seconds),
            "prepared_sandbox_p50_seconds": _p50(self.prepared_sandbox_seconds),
            "prepared_sandbox_hits": self.prepared_sandbox_hits,
            "prepared_sandbox_misses": self.prepared_sandbox_misses,
            "prepared_sandbox_failures": self.prepared_sandbox_failures,
            "prepared_sandbox_cleanup_failures": self.prepared_sandbox_cleanup_failures,
            "prepared_sandbox_hit_rate": _ratio(
                self.prepared_sandbox_hits,
                self.prepared_sandbox_hits + self.prepared_sandbox_misses,
            ),
            "task_p50_seconds": _p50(self.task_seconds),
            "task_p95_seconds": _p95(self.task_seconds),
            "attempt_failure_breakdown": dict(self.attempt_failure_categories),
        }


def _p50(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = min(len(sorted_values) - 1, int(round((len(sorted_values) - 1) * 0.95)))
    return float(sorted_values[index])


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0
