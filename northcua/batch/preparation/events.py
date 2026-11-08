"""Event helpers for prepared sandbox lifecycle telemetry."""

from __future__ import annotations

from northcua.batch.contracts import BatchTaskSpec
from northcua.batch.metrics import BatchMetrics
from northcua.batch.preparation.contracts import PreparedSandboxEventMessage, PreparedSandboxFallbackReason
from northcua.batch.runner.events import BatchEventWriter
from northcua.telemetry.contracts import CaseStatus


class PreparedSandboxEventEmitter:
    """Emit prepared-sandbox lifecycle events with consistent metrics."""

    def __init__(self, *, events: BatchEventWriter, metrics: BatchMetrics) -> None:
        self._events = events
        self._metrics = metrics

    def fallback(
        self,
        task: BatchTaskSpec,
        attempt: int,
        reason: PreparedSandboxFallbackReason,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        """Record that a task used normal sandbox acquisition.

        Args:
            task: Claimed task that could not use prepared capacity.
            attempt: One-based task attempt number.
            reason: Normalized fallback reason.
        """

        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.FALLBACK,
            status=CaseStatus.PROVISIONING,
            attempt=attempt,
            details={"attempt": attempt, "reason": reason.value, **(details or {})},
        )
        if reason != PreparedSandboxFallbackReason.DISABLED:
            self._metrics.observe_prepared_miss()

    def prepare_failed(self, *, task: BatchTaskSpec, attempt: int, error: str) -> None:
        """Record a background prepare failure.

        Args:
            task: Task whose prepared sandbox failed.
            attempt: One-based task attempt number.
            error: Redacted error summary.
        """

        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.FAILED,
            status=CaseStatus.FAILED,
            attempt=attempt,
            details={"attempt": attempt, "error": error[:500]},
        )
