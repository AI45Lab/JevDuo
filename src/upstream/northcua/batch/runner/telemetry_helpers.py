"""Telemetry helpers for measuring batch attempt cleanup phases."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.runner.leases import BatchTaskLeaseContext, BatchTaskLeaseCoordinator
from northcua.sandboxes.contracts import SandboxLease
from northcua.telemetry.contracts import CasePhase, CaseStatus, TelemetryEntityKind
from northcua.telemetry.redaction import redact_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Result of a sandbox cleanup attempt.

    Attributes:
        ok: Whether the release completed without raising.
        cleanup_status: Redacted status persisted to the task row.
    """

    ok: bool
    cleanup_status: str


class CleanupRelease(Protocol):
    """Coroutine signature used to release task-owned sandbox capacity."""

    async def __call__(
        self,
        *,
        coordinator: BatchTaskLeaseCoordinator,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        state: BatchTaskState,
        lease: SandboxLease | None,
        lease_context: BatchTaskLeaseContext | None,
        output_dir: Path,
        attempt: int,
    ) -> None:
        """Release a task-attempt sandbox lease.

        Args:
            coordinator: Sandbox lease coordinator that owns per-task releases.
            config: Active batch runtime configuration.
            task: Manifest task being cleaned up.
            state: Latest task state.
            lease: Sandbox lease acquired for the task, if any.
            lease_context: Per-task lease context returned by the coordinator.
            output_dir: Concrete batch output directory.
            attempt: One-based attempt count.

        """


async def _persist_cleanup_status_best_effort(
    *,
    events: BatchEventWriter,
    state: BatchTaskState,
    cleanup_status: str,
    config: BatchRunConfig,
    attempt: int,
) -> bool:
    """Persist cleanup status without changing the cleanup outcome."""

    try:
        return await events.upsert_task_async(state.model_copy(update={"cleanup_status": cleanup_status}))
    except Exception:
        logger.warning(
            "sandbox cleanup status write failed after cleanup completed",
            extra={
                "batch_id": config.manifest.batch_id,
                "task_id": state.task_id,
                "attempt": attempt,
                "cleanup_status": cleanup_status,
            },
            exc_info=True,
        )
        return False


async def release_with_telemetry(
    *,
    release: CleanupRelease,
    coordinator: BatchTaskLeaseCoordinator,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    state: BatchTaskState,
    lease: SandboxLease | None,
    lease_context: BatchTaskLeaseContext | None,
    output_dir: Path,
    attempt: int,
    events: BatchEventWriter,
) -> CleanupResult:
    """Release sandbox capacity while measuring cleanup latency.

    Args:
        release: Cleanup coroutine used by the batch attempt helper.
        coordinator: Sandbox lease coordinator that owns per-task releases.
        config: Active batch runtime configuration.
        task: Manifest task being cleaned up.
        state: Latest task state.
        lease: Sandbox lease acquired for the task, if any.
        lease_context: Per-task lease context returned by the coordinator.
        output_dir: Concrete batch output directory.
        attempt: One-based attempt count.
        events: Batch event writer carrying the telemetry recorder.

    Returns:
        Cleanup result carrying both boolean status and the exact redacted
        cleanup status persisted to the task row.
    """

    try:
        with events.phase_timer(
            case_id=task.task_id,
            phase=CasePhase.CLEANUP,
            status=CaseStatus.FINALIZING,
            attempt=attempt,
            entity_kind=TelemetryEntityKind.SANDBOX,
            entity_id=lease.handle.sandbox_id if lease is not None else task.task_id,
        ):
            await release(
                coordinator=coordinator,
                config=config,
                task=task,
                state=state,
                lease=lease,
                lease_context=lease_context,
                output_dir=output_dir,
                attempt=attempt,
            )
    except Exception as exc:
        logger.warning(
            "sandbox cleanup failed; preserving case terminal state for janitor",
            extra={"batch_id": config.manifest.batch_id, "task_id": task.task_id, "attempt": attempt},
            exc_info=True,
        )
        cleanup_status = f"failed: {redact_message(str(exc))}"
        await _persist_cleanup_status_best_effort(
            events=events,
            state=state,
            cleanup_status=cleanup_status,
            config=config,
            attempt=attempt,
        )
        return CleanupResult(ok=False, cleanup_status=cleanup_status)
    await _persist_cleanup_status_best_effort(
        events=events,
        state=state,
        cleanup_status="ok",
        config=config,
        attempt=attempt,
    )
    return CleanupResult(ok=True, cleanup_status="ok")
