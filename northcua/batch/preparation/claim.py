"""Prepared sandbox claim resolution helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.contracts import PreparedSandboxFallbackReason, PreparedSandboxPolicy
from northcua.batch.preparation.events import PreparedSandboxEventEmitter

logger = logging.getLogger(__name__)


class PreparedSandboxClaimResolver:
    """Resolve claim-time prepared assignments without owning lifecycle work."""

    def __init__(
        self,
        *,
        config: BatchRunConfig,
        policy: PreparedSandboxPolicy,
        futures: dict[tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None]],
        assignments: dict[tuple[str, int], PreparedSandboxAssignment],
        event_emitter: PreparedSandboxEventEmitter,
        pool_depth_details: Callable[[], dict[str, object]],
        abandon_future: Callable[
            [tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None], str],
            None,
        ],
    ) -> None:
        self._config = config
        self._policy = policy
        self._futures = futures
        self._assignments = assignments
        self._event_emitter = event_emitter
        self._pool_depth_details = pool_depth_details
        self._abandon_future = abandon_future

    async def assignment_for_claim(
        self,
        *,
        task: BatchTaskSpec,
        attempt: int,
        key: tuple[str, int],
        claim_started: float,
    ) -> PreparedSandboxAssignment | None:
        """Return a ready assignment, wait briefly for one, or report fallback.

        Args:
            task: Batch task whose next attempt is trying to claim prepared supply.
            attempt: One-based attempt number represented by the prepared row.
            key: Stable ``(task_id, attempt)`` lookup key shared with the manager.
            claim_started: Monotonic timestamp used for timeout telemetry.

        Returns:
            Ready prepared assignment when one can be claimed; otherwise ``None``
            after emitting the appropriate prepared fallback reason.
        """

        assignment = self._assignments.get(key)
        if assignment is not None:
            return assignment
        future = self._futures.get(key)
        if future is None:
            self._event_emitter.fallback(
                task,
                attempt,
                PreparedSandboxFallbackReason.MISS,
                details={**self._pool_depth_details(), "claim_visible_future": False},
            )
            return None
        if future.cancelled() or _future_cancel_requested(future):
            return self._handle_future_cancel(task=task, attempt=attempt, key=key, future=future)
        wait_seconds = min(self._policy.claim_pending_wait_seconds, self._policy.claim_wait_seconds)
        if wait_seconds <= 0 and not future.done():
            self._abandon_future(key, future, "prepared sandbox abandoned after claim fallback")
            self._event_emitter.fallback(
                task,
                attempt,
                PreparedSandboxFallbackReason.MISS,
                details={
                    **self._pool_depth_details(),
                    "claim_visible_future": True,
                    "claim_pending_wait_seconds": wait_seconds,
                    "prepare_future_continued": True,
                },
            )
            return None
        try:
            assignment = await asyncio.wait_for(asyncio.shield(future), wait_seconds)
            if assignment is None:
                self._futures.pop(key, None)
                self._event_emitter.fallback(
                    task,
                    attempt,
                    PreparedSandboxFallbackReason.PREPARE_FAILED,
                    details=self._pool_depth_details(),
                )
            return assignment
        except TimeoutError:
            self._abandon_future(key, future, "prepared sandbox abandoned after claim wait timeout")
            self._event_emitter.fallback(
                task,
                attempt,
                PreparedSandboxFallbackReason.TIMEOUT,
                details={
                    **self._pool_depth_details(),
                    "claim_wait_seconds": max(time.monotonic() - claim_started, 0.0),
                    "claim_pending_wait_seconds": wait_seconds,
                    "prepare_future_continued": True,
                },
            )
            return None
        except asyncio.CancelledError:
            return self._handle_future_cancel(task=task, attempt=attempt, key=key, future=future)
        except Exception:
            logger.warning(
                "prepared sandbox future failed before assignment",
                extra={"batch_id": self._config.manifest.batch_id, "task_id": task.task_id, "attempt": attempt},
                exc_info=True,
            )
            self._futures.pop(key, None)
            self._event_emitter.fallback(
                task,
                attempt,
                PreparedSandboxFallbackReason.PREPARE_FAILED,
                details=self._pool_depth_details(),
            )
            return None

    def _handle_future_cancel(
        self,
        *,
        task: BatchTaskSpec,
        attempt: int,
        key: tuple[str, int],
        future: asyncio.Future[PreparedSandboxAssignment | None],
    ) -> PreparedSandboxAssignment | None:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise asyncio.CancelledError
        if not future.cancelled() and not _future_cancel_requested(future):
            raise asyncio.CancelledError
        self._futures.pop(key, None)
        logger.info(
            "prepared sandbox future was canceled before assignment; falling back",
            extra={"batch_id": self._config.manifest.batch_id, "task_id": task.task_id, "attempt": attempt},
        )
        self._event_emitter.fallback(
            task,
            attempt,
            PreparedSandboxFallbackReason.PREPARE_FAILED,
            details=self._pool_depth_details(),
        )
        return None


def _future_cancel_requested(future: asyncio.Future[PreparedSandboxAssignment | None]) -> bool:
    """Return whether a Task-like future has accepted cancellation."""

    cancelling = getattr(future, "cancelling", None)
    if not callable(cancelling):
        return False
    try:
        return bool(cancelling())
    except RuntimeError:
        return False
