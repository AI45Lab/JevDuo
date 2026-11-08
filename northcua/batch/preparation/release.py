"""Release helpers for prepared sandbox leases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.metrics import BatchMetrics
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.contracts import PreparedSandboxEventMessage, PreparedSandboxStatus
from northcua.batch.preparation.sqlite_store import SQLitePreparedSandboxStore
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.runner.leases import BatchTaskLeaseContext, BatchTaskLeaseCoordinator
from northcua.batch.state.async_repository import prepared_repository_call as repository_call
from northcua.telemetry.contracts import CasePhase, CaseStatus

logger = logging.getLogger(__name__)

_PREPARED_RELEASE_RETRY_DELAYS_SECONDS = (0.5, 1.5)
_BROKEN_PIPE_MARKERS = ("Broken pipe", "[Errno 32]")


@dataclass(frozen=True)
class PreparedSandboxReleaseResult:
    """Outcome of best-effort prepared sandbox release.

    Attributes:
        ok: Whether release completed successfully.
        error: Last release error message.
        attempts: Number of release attempts performed.
        retryable: Whether the last release error looked retryable.
    """

    ok: bool
    error: str = ""
    attempts: int = 0
    retryable: bool = False

    def __bool__(self) -> bool:
        return self.ok


def _is_retryable_release_error(exc: Exception) -> bool:
    if isinstance(exc, BrokenPipeError):
        return True
    text = str(exc)
    return any(marker in text for marker in _BROKEN_PIPE_MARKERS)


def _cleanup_failed_error(*, close_reason: str, terminate_error: str) -> str:
    if not close_reason:
        return f"terminate_failed: {terminate_error}"
    return f"{close_reason}; terminate_failed: {terminate_error}"


class PreparedSandboxReleaseHelper:
    """Release and mark prepared sandbox leases consistently."""

    def __init__(
        self,
        *,
        config: BatchRunConfig,
        store: SQLitePreparedSandboxStore,
        coordinator: BatchTaskLeaseCoordinator,
        events: BatchEventWriter,
        metrics: BatchMetrics,
        output_dir: Path,
    ) -> None:
        self._config = config
        self._store = store
        self._coordinator = coordinator
        self._events = events
        self._metrics = metrics
        self._output_dir = output_dir

    def mark_released(self, assignment: PreparedSandboxAssignment) -> None:
        """Record that the standard attempt teardown released a sandbox.

        Args:
            assignment: Assigned prepared sandbox lease that was released.
        """

        self._store.mark_released(assignment.record)
        self._events.prepared_event(
            case_id=assignment.record.task_id,
            message=PreparedSandboxEventMessage.RELEASED,
            status=CaseStatus.FINALIZING,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "cleanup_result": "released"},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )

    async def mark_released_async(self, assignment: PreparedSandboxAssignment) -> None:
        """Record a standard attempt teardown without blocking the event loop.

        Args:
            assignment: Assigned prepared sandbox lease that was released.
        """

        await repository_call(lambda: self._store.mark_released(assignment.record))
        self._events.prepared_event(
            case_id=assignment.record.task_id,
            message=PreparedSandboxEventMessage.RELEASED,
            status=CaseStatus.FINALIZING,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "cleanup_result": "released"},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )

    def mark_cleanup_failed(self, assignment: PreparedSandboxAssignment, *, error: str) -> None:
        """Mark an assigned prepared row for operator cleanup.

        Args:
            assignment: Prepared assignment whose normal release failed.
            error: Redacted cleanup status from the attempt release path.
        """

        self._store.mark_cleanup_failed(assignment.record, error=error)
        self._metrics.observe_prepared_cleanup_failure()
        self._events.prepared_event(
            case_id=assignment.record.task_id,
            message=PreparedSandboxEventMessage.CLEANUP_FAILED,
            status=CaseStatus.FAILED,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "cleanup_result": "failed", "error": error[:500]},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )

    async def mark_cleanup_failed_async(self, assignment: PreparedSandboxAssignment, *, error: str) -> None:
        """Mark an assigned prepared row for operator cleanup off-loop.

        Args:
            assignment: Prepared assignment whose normal release failed.
            error: Redacted cleanup status from the attempt release path.
        """

        await repository_call(lambda: self._store.mark_cleanup_failed(assignment.record, error=error))
        self._metrics.observe_prepared_cleanup_failure()
        self._events.prepared_event(
            case_id=assignment.record.task_id,
            message=PreparedSandboxEventMessage.CLEANUP_FAILED,
            status=CaseStatus.FAILED,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "cleanup_result": "failed", "error": error[:500]},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )

    async def mark_cleanup_deferred_async(self, assignment: PreparedSandboxAssignment, *, error: str) -> None:
        """Mark an assigned row whose release is intentionally deferred.

        Args:
            assignment: Prepared assignment still tied to controller-owned work.
            error: Operator-facing reason explaining the deferred cleanup.
        """

        await repository_call(lambda: self._store.mark_cleanup_deferred(assignment.record, error=error))
        self._events.prepared_event(
            case_id=assignment.record.task_id,
            message=PreparedSandboxEventMessage.CLEANUP_DEFERRED,
            status=CaseStatus.FINALIZING,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "cleanup_result": "deferred", "error": error[:500]},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )

    async def release_unassigned_assignment(
        self,
        *,
        task: BatchTaskSpec,
        assignment: PreparedSandboxAssignment,
        final_status: PreparedSandboxStatus,
        error: str,
    ) -> None:
        """Release an unclaimed prepared sandbox and update SQLite state.

        Args:
            task: Manifest task reserved by the prepared sandbox.
            assignment: Prepared assignment to release.
            final_status: Terminal prepared state requested after cleanup.
            error: Operator-facing cleanup reason.
        """

        release_result = await self.try_release_prepared_context(
            task=task,
            attempt=assignment.record.attempt,
            context=assignment.lease_context,
            close_reason=error,
        )
        if release_result:
            if final_status == PreparedSandboxStatus.CANCELED:
                canceled = await repository_call(lambda: self._store.mark_canceled(assignment.record))
                if canceled is None:
                    await repository_call(lambda: self._store.mark_released(assignment.record))
            elif final_status == PreparedSandboxStatus.FAILED:
                await repository_call(lambda: self._store.mark_failed(assignment.record, error=error))
        else:
            await repository_call(
                lambda: self._store.mark_cleanup_failed(
                    assignment.record,
                    error=_cleanup_failed_error(close_reason=error, terminate_error=release_result.error),
                )
            )

    async def try_release_prepared_context(
        self,
        *,
        task: BatchTaskSpec,
        attempt: int,
        context: BatchTaskLeaseContext,
        close_reason: str = "",
    ) -> PreparedSandboxReleaseResult:
        """Best-effort release for background prepared sandbox capacity.

        Args:
            task: Manifest task associated with the prepared lease.
            attempt: One-based attempt number.
            context: Lease context to release.
            close_reason: Optional high-level reason this unassigned sandbox is
                being released, separate from provider terminate failures.

        Returns:
            Structured release result. Truthy when release completed; otherwise
            falsey after writing cleanup-failure telemetry.
        """

        release_attempt = 0
        retryable = False
        while True:
            release_attempt += 1
            try:
                await self._release_prepared_context(task=task, attempt=attempt, context=context)
            except Exception as exc:
                retryable = _is_retryable_release_error(exc)
                delay_index = release_attempt - 1
                if retryable and delay_index < len(_PREPARED_RELEASE_RETRY_DELAYS_SECONDS):
                    delay = _PREPARED_RELEASE_RETRY_DELAYS_SECONDS[delay_index]
                    logger.warning(
                        "prepared sandbox cleanup failed with retryable error; retrying",
                        extra={
                            "batch_id": self._config.manifest.batch_id,
                            "task_id": task.task_id,
                            "attempt": attempt,
                            "release_attempt": release_attempt,
                            "retry_delay_seconds": delay,
                        },
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "prepared sandbox cleanup failed",
                    extra={
                        "batch_id": self._config.manifest.batch_id,
                        "task_id": task.task_id,
                        "attempt": attempt,
                        "release_attempts": release_attempt,
                        "retryable": retryable,
                    },
                    exc_info=True,
                )
                terminate_error = str(exc)
                details = {
                    "attempt": attempt,
                    "cleanup_result": "terminate_failed",
                    "terminate_error": terminate_error[:500],
                    "release_attempts": release_attempt,
                    "retryable": retryable,
                }
                if close_reason:
                    details["close_reason"] = close_reason
                    details["error"] = _cleanup_failed_error(
                        close_reason=close_reason,
                        terminate_error=terminate_error,
                    )[:500]
                else:
                    details["error"] = terminate_error[:500]
                self._events.prepared_event(
                    case_id=task.task_id,
                    message=PreparedSandboxEventMessage.CLEANUP_FAILED,
                    status=CaseStatus.FAILED,
                    attempt=attempt,
                    details=details,
                    sandbox_id=context.lease.handle.sandbox_id,
                    device_id=context.lease.device_id,
                )
                self._metrics.observe_prepared_cleanup_failure()
                return PreparedSandboxReleaseResult(
                    ok=False,
                    error=terminate_error,
                    attempts=release_attempt,
                    retryable=retryable,
                )
            return PreparedSandboxReleaseResult(ok=True, attempts=release_attempt)

    async def _release_prepared_context(
        self,
        *,
        task: BatchTaskSpec,
        attempt: int,
        context: BatchTaskLeaseContext,
    ) -> None:
        state = BatchTaskState(
            task_id=task.task_id,
            attempts=attempt,
            status=BatchTaskStatus.CANCELED,
            error="prepared sandbox was not assigned",
        )
        await self._coordinator.release(
            config=self._config,
            task=task,
            state=state,
            context=context,
            output_dir=self._output_dir,
            attempt=attempt,
            events=self._events,
            log_dir_override=_prepared_log_dir(self._output_dir, task=task, attempt=attempt),
            sandbox_phase_override=CasePhase.PREPARED_SANDBOX,
        )


def _prepared_log_dir(output_dir: Path, *, task: BatchTaskSpec, attempt: int) -> Path:
    return output_dir / "sandbox-logs" / task.task_id / f"attempt-{attempt}-prepared"
