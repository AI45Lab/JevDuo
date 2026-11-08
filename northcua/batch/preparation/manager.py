"""Background manager for single-use prepared sandboxes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from northcua.batch.backpressure import ControllerBackpressureGate
from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec
from northcua.batch.metrics import BatchMetrics
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.assignment_ttl import (
    assignment_ttl_guard_seconds,
    estimated_provider_remaining_ttl_seconds,
    prepared_assignment_ttl_details,
    task_by_id,
)
from northcua.batch.preparation.assignment_ttl import task_timeout_seconds as resolve_task_timeout_seconds
from northcua.batch.preparation.claim import PreparedSandboxClaimResolver
from northcua.batch.preparation.contracts import (
    PreparedSandboxEventMessage,
    PreparedSandboxFailureKind,
    PreparedSandboxFallbackReason,
    PreparedSandboxPolicy,
    PreparedSandboxRecord,
    PreparedSandboxStatus,
)
from northcua.batch.preparation.events import PreparedSandboxEventEmitter
from northcua.batch.preparation.planner import PreparedSandboxCandidate, PreparedSandboxPlanner
from northcua.batch.preparation.preparer import PreparedSandboxPreparer
from northcua.batch.preparation.release import PreparedSandboxReleaseHelper
from northcua.batch.preparation.sqlite_store import SQLitePreparedSandboxStore
from northcua.batch.preparation.startup import PreparedSandboxStartupRunner
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.runner.leases import BatchTaskLeaseCoordinator
from northcua.batch.state.async_repository import prepared_repository_call as repository_call
from northcua.sandboxes.contracts import SandboxReadinessState
from northcua.telemetry.contracts import CaseStatus

logger = logging.getLogger(__name__)

_PREPARE_LOOP_SLEEP_SECONDS = 0.25
_CLOSE_CLEANUP_WAIT_SECONDS = 5.0
_POOL_STATUS_LOG_INTERVAL_SECONDS = 30.0


async def _wait_for_initial_ready(
    *,
    assignments: dict[tuple[str, int], PreparedSandboxAssignment],
    assigned: set[tuple[str, int]],
    futures: dict[tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None]],
    store: SQLitePreparedSandboxStore,
    runner_id: str,
    ready_changed: asyncio.Event,
    min_ready: int,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    started_at = time.monotonic()
    observed_prepare_activity = False
    ready_count = 0
    pending_futures = False
    active_rows = 0
    while time.monotonic() < deadline:
        ready_count = sum(1 for key in assignments if key not in assigned)
        pending_futures = any(not future.done() for future in futures.values())
        active_rows = await repository_call(lambda: store.active_count(runner_id=runner_id))
        observed_prepare_activity = observed_prepare_activity or bool(futures) or active_rows > 0
        if ready_count >= min_ready:
            return {
                "exit_reason": "ready",
                "elapsed_seconds": max(time.monotonic() - started_at, 0.0),
                "ready_count": ready_count,
                "pending_future_count": sum(1 for future in futures.values() if not future.done()),
                "active_rows": active_rows,
                "observed_prepare_activity": observed_prepare_activity,
            }
        if observed_prepare_activity and not pending_futures and active_rows <= 0:
            return {
                "exit_reason": "prepare_idle",
                "elapsed_seconds": max(time.monotonic() - started_at, 0.0),
                "ready_count": ready_count,
                "pending_future_count": 0,
                "active_rows": active_rows,
                "observed_prepare_activity": observed_prepare_activity,
            }
        timeout = max(deadline - time.monotonic(), 0.0)
        ready_changed.clear()
        try:
            await asyncio.wait_for(ready_changed.wait(), timeout=min(timeout, 1.0))
        except TimeoutError:
            continue
    ready_count = sum(1 for key in assignments if key not in assigned)
    pending_future_count = sum(1 for future in futures.values() if not future.done())
    active_rows = await repository_call(lambda: store.active_count(runner_id=runner_id))
    return {
        "exit_reason": "timeout",
        "elapsed_seconds": max(time.monotonic() - started_at, 0.0),
        "ready_count": ready_count,
        "pending_future_count": pending_future_count,
        "active_rows": active_rows,
        "observed_prepare_activity": observed_prepare_activity,
    }


async def _expire_prepared_records(
    *,
    config: BatchRunConfig,
    store: SQLitePreparedSandboxStore,
    events: BatchEventWriter,
    futures: dict[tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None]],
    claim_visible_futures: dict[tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None]],
    assignments: dict[tuple[str, int], PreparedSandboxAssignment],
    assigned: set[tuple[str, int]],
    schedule_release: Callable[..., None],
) -> None:
    expired_records = await repository_call(
        lambda: [
            *store.expire_old(),
            *store.expire_stale(stale_after_seconds=config.lease_timeout_seconds),
        ]
    )
    for record in expired_records:
        events.prepared_event(
            case_id=record.task_id,
            message=PreparedSandboxEventMessage.EXPIRED,
            status=CaseStatus.FAILED,
            attempt=record.attempt,
            details={
                "attempt": record.attempt,
                "sandbox_id": record.sandbox_id,
                "device_id": record.device_id,
                "error": record.error,
                "expires_at": record.expires_at,
                "prepare_seconds": record.prepare_seconds,
            },
            sandbox_id=record.sandbox_id,
            device_id=record.device_id,
        )
        key = record.key
        future = futures.pop(key, None)
        claim_visible_futures.pop(key, None)
        future_assignment: PreparedSandboxAssignment | None = None
        if future is not None:
            if not future.done():
                future.cancel(PreparedSandboxFailureKind.EXPIRED_BEFORE_ASSIGNMENT.value)
            results = await asyncio.gather(future, return_exceptions=True)
            finished = results[0] if results else None
            if isinstance(finished, PreparedSandboxAssignment):
                future_assignment = finished
        assignment = assignments.pop(key, None) or future_assignment
        task = task_by_id(config, record.task_id)
        if assignment is not None and key not in assigned and task is not None:
            schedule_release(
                task=task,
                assignment=assignment,
                final_status=PreparedSandboxStatus.FAILED,
                error="prepared sandbox expired before assignment",
            )


async def _claim_prepared_sandbox(
    self,
    *,
    task: BatchTaskSpec,
    attempt: int,
) -> PreparedSandboxAssignment | None:
    """Assign a ready prepared sandbox or return ``None`` for fallback.

    Args:
        task: Task being claimed by the batch worker.
        attempt: One-based attempt number being executed.

    Returns:
        Prepared assignment when available and still valid; otherwise
        ``None`` so the caller uses normal sandbox acquisition.

    Raises:
        asyncio.CancelledError: If the attempt task itself is being
        canceled rather than a prepared future being canceled internally.
    """

    if not self._policy.enabled:
        self._event_emitter.fallback(
            task,
            attempt,
            PreparedSandboxFallbackReason.DISABLED,
            details=self._pool_depth_details(),
        )
        return None
    claim_started = time.monotonic()
    key = (task.task_id, attempt)
    assignment = await self._claim_resolver.assignment_for_claim(
        task=task,
        attempt=attempt,
        key=key,
        claim_started=claim_started,
    )
    if assignment is None:
        self._prune_completed_futures()
        return None
    task_timeout_seconds = resolve_task_timeout_seconds(self._config, task)
    guard_seconds = assignment_ttl_guard_seconds(config=self._config, policy=self._policy, task=task)
    guard_now = time.time()
    guard_details = prepared_assignment_ttl_details(
        assignment,
        config=self._config,
        task=task,
        now=guard_now,
        guard_seconds=guard_seconds,
        task_timeout_seconds_value=task_timeout_seconds,
    )
    provider_remaining_ttl = estimated_provider_remaining_ttl_seconds(
        config=self._config,
        task=task,
        assignment=assignment,
        now=guard_now,
    )
    if guard_seconds > 0 and provider_remaining_ttl < guard_seconds:
        await self._release_unassigned_assignment(
            task=task,
            assignment=assignment,
            final_status=PreparedSandboxStatus.CANCELED,
            error=(
                "prepared sandbox provider lease TTL below assignment guard: "
                f"{provider_remaining_ttl:.3f}s < {guard_seconds:.3f}s"
            ),
        )
        self._event_emitter.fallback(
            task,
            attempt,
            PreparedSandboxFallbackReason.INSUFFICIENT_REMAINING_TTL,
            details={
                **self._pool_depth_details(),
                **guard_details,
                "claim_wait_seconds": max(time.monotonic() - claim_started, 0.0),
                "result": "ttl_guard_rejected",
            },
        )
        return None
    record = await repository_call(
        lambda: self._store.assign(
            task_id=task.task_id,
            attempt=attempt,
            runner_id=self._config.runner_id,
            min_remaining_ttl_seconds=0.0,
        )
    )
    if record is None:
        fresh_now = time.time()
        fresh_details = prepared_assignment_ttl_details(
            assignment,
            config=self._config,
            task=task,
            now=fresh_now,
            guard_seconds=guard_seconds,
            task_timeout_seconds_value=task_timeout_seconds,
        )
        fallback_reason = PreparedSandboxFallbackReason.CLAIM_REJECTED
        fresh_provider_remaining_ttl = estimated_provider_remaining_ttl_seconds(
            config=self._config,
            task=task,
            assignment=assignment,
            now=fresh_now,
        )
        if guard_seconds > 0 and fresh_provider_remaining_ttl < guard_seconds:
            fallback_reason = PreparedSandboxFallbackReason.INSUFFICIENT_REMAINING_TTL
        if key in self._assignments:
            await self._release_unassigned_assignment(
                task=task,
                assignment=self._assignments[key],
                final_status=PreparedSandboxStatus.CANCELED,
                error=f"prepared claim rejected for {task.task_id} attempt {attempt}",
            )
        self._event_emitter.fallback(
            task,
            attempt,
            fallback_reason,
            details={**self._pool_depth_details(), **fresh_details},
        )
        return None
    assigned = PreparedSandboxAssignment(
        record=record,
        lease=assignment.lease,
        lease_context=assignment.lease_context,
    )
    self._assignments[key] = assigned
    self._assigned.add(key)
    try:
        readiness = await assignment.lease_context.manager.wait_for_ready(
            record.device_id,
            timeout_seconds=self._config.device_readiness_timeout_seconds,
            batch_id=self._config.manifest.batch_id,
            task_id=task.task_id,
            sandbox_id=record.sandbox_id,
            provider=record.provider,
            reason="prepared_claim",
        )
    except asyncio.CancelledError:
        self._schedule_unassigned_assignment_release(
            task=task,
            assignment=assigned,
            final_status=PreparedSandboxStatus.CANCELED,
            error="prepared claim canceled after assignment",
        )
        raise
    if readiness != SandboxReadinessState.READY:
        await self._release_unassigned_assignment(
            task=task,
            assignment=assigned,
            final_status=PreparedSandboxStatus.FAILED,
            error=f"prepared device was not ready at claim: {readiness.value}",
        )
        self._event_emitter.fallback(
            task,
            attempt,
            PreparedSandboxFallbackReason.DEVICE_NOT_READY,
            details=self._pool_depth_details(),
        )
        return None
    assignment_details = prepared_assignment_ttl_details(
        assigned,
        config=self._config,
        task=task,
        now=time.time(),
        guard_seconds=guard_seconds,
        task_timeout_seconds_value=task_timeout_seconds,
    )
    self._events.prepared_event(
        case_id=task.task_id,
        message=PreparedSandboxEventMessage.ASSIGNED,
        status=CaseStatus.PROVISIONING,
        attempt=attempt,
        details={
            "attempt": attempt,
            "prepare_seconds": record.prepare_seconds,
            "claim_wait_seconds": max(time.monotonic() - claim_started, 0.0),
            "result": "hit",
            **assignment_details,
            **self._pool_depth_details(),
        },
        sandbox_id=record.sandbox_id,
        device_id=record.device_id,
    )
    logger.info(
        "prepared sandbox assigned",
        extra={
            "batch_id": self._config.manifest.batch_id,
            "task_id": task.task_id,
            "attempt": attempt,
            "sandbox_id": record.sandbox_id,
            "device_id": record.device_id,
        },
    )
    self._metrics.observe_prepared_hit()
    return assigned


async def _run_prepared_sandbox_loop(self) -> None:
    while not self._closed.is_set():
        await repository_call(lambda: self._store.heartbeat_runner(runner_id=self._config.runner_id))
        await self._expire_records()
        await self._refresh_pool_depth_store_counts()
        self._prune_completed_futures()
        self._preparation_control_blocked = await repository_call(self._store.control_blocks_preparation)
        if self._preparation_control_blocked:
            await self._cancel_pending_prepares(error="batch control flag stopped prepared sandbox work")
            self._maybe_log_pool_status(
                candidate_count=0,
                scheduled_count=0,
                skipped_future_count=0,
                skipped_assignment_count=0,
                skipped_backpressure_count=0,
                backpressure_active=False,
                control_blocked=True,
            )
            await asyncio.sleep(_PREPARE_LOOP_SLEEP_SECONDS)
            continue
        self._preparation_control_blocked = False
        backpressure_active = False
        if self._backpressure_gate is not None:
            snapshot = await self._backpressure_gate.active_snapshot(scope="prepare")
            backpressure_active = bool(getattr(snapshot, "active", False))
        candidates = await repository_call(
            lambda: self._planner.candidates(
                run_concurrency=self._run_concurrency,
                runner_id=self._config.runner_id,
            )
        )
        scheduled_count = 0
        skipped_future_count = 0
        skipped_assignment_count = 0
        skipped_backpressure_count = 0
        for candidate in candidates:
            key = (candidate.task.task_id, candidate.attempt)
            if key in self._futures:
                skipped_future_count += 1
                continue
            if key in self._assignments:
                skipped_assignment_count += 1
                continue
            if backpressure_active:
                skipped_backpressure_count += 1
                continue
            scheduled_at = time.monotonic()
            self._futures[key] = asyncio.create_task(
                self._preparer.prepare_with_semaphore(candidate, scheduled_at=scheduled_at),
                name=f"northcua-prepare-sandbox:{candidate.task.task_id}:{candidate.attempt}",
            )
            scheduled_count += 1
        self._maybe_log_pool_status(
            candidate_count=len(candidates),
            scheduled_count=scheduled_count,
            skipped_future_count=skipped_future_count,
            skipped_assignment_count=skipped_assignment_count,
            skipped_backpressure_count=skipped_backpressure_count,
            backpressure_active=backpressure_active,
            control_blocked=False,
        )
        await asyncio.sleep(_PREPARE_LOOP_SLEEP_SECONDS)


class PreparedSandboxManager:
    """Prepare per-case sandboxes before the task is claimed."""

    def __init__(
        self,
        *,
        config: BatchRunConfig,
        store: SQLitePreparedSandboxStore,
        planner: PreparedSandboxPlanner,
        coordinator: BatchTaskLeaseCoordinator,
        events: BatchEventWriter,
        metrics: BatchMetrics,
        output_dir: Path,
        run_concurrency: int,
        create_concurrency: int,
        backpressure_gate: ControllerBackpressureGate | None = None,
    ) -> None:
        self._config = config
        self._policy = config.prepared_sandbox_policy
        self._store = store
        self._planner = planner
        self._coordinator = coordinator
        self._events = events
        self._metrics = metrics
        self._output_dir = output_dir
        self._release_helper = PreparedSandboxReleaseHelper(
            config=config,
            store=store,
            coordinator=coordinator,
            events=events,
            metrics=metrics,
            output_dir=output_dir,
        )
        self._startup_runner = PreparedSandboxStartupRunner(config=config, policy=self._policy, events=events)
        self._event_emitter = PreparedSandboxEventEmitter(events=events, metrics=metrics)
        self._run_concurrency = run_concurrency
        self._backpressure_gate = backpressure_gate
        self._prepare_semaphore = asyncio.Semaphore(
            self._policy.effective_prepare_concurrency(create_concurrency=create_concurrency)
        )
        self._futures: dict[tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None]] = {}
        self._claim_visible_futures: dict[tuple[str, int], asyncio.Task[PreparedSandboxAssignment | None]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_inflight_keys: set[tuple[str, int]] = set()
        self._abandoned_claim_keys: set[tuple[str, int]] = set()
        self._assignments: dict[tuple[str, int], PreparedSandboxAssignment] = {}
        self._assigned: set[tuple[str, int]] = set()
        self._pool_depth_active_unassigned = 0
        self._pool_depth_budget_counts: dict[str, int] = {}
        self._pool_depth_status_counts: dict[str, int] = {}
        self._pool_depth_counts_updated_at = 0.0
        self._preparation_control_blocked = False
        self._closed = asyncio.Event()
        self._ready_changed = asyncio.Event()
        self._last_pool_status_log_at = 0.0
        self._preparer = PreparedSandboxPreparer(
            config=config,
            store=store,
            coordinator=coordinator,
            events=events,
            metrics=metrics,
            output_dir=output_dir,
            policy=self._policy,
            run_concurrency=run_concurrency,
            prepare_semaphore=self._prepare_semaphore,
            startup_runner=self._startup_runner,
            release_helper=self._release_helper,
            event_emitter=self._event_emitter,
            assignments=self._assignments,
            ready_changed=self._ready_changed,
            claim_visible=self._mark_future_claim_visible,
            backpressure_gate=backpressure_gate,
            stop_requested=lambda: self._closed.is_set() or self._preparation_control_blocked,
        )
        self._claim_resolver = PreparedSandboxClaimResolver(
            config=config,
            policy=self._policy,
            futures=self._claim_visible_futures,
            assignments=self._assignments,
            event_emitter=self._event_emitter,
            pool_depth_details=self._pool_depth_details,
            abandon_future=self._abandon_claim_visible_future,
        )
        self._loop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the bounded background preparation loop."""

        if not self._policy.enabled or self._loop_task is not None:
            return
        await self._refresh_pool_depth_store_counts()
        self._loop_task = asyncio.create_task(self._run_loop(), name="northcua-prepared-sandbox-manager")

    async def wait_for_initial_ready(self, *, min_ready: int, timeout_seconds: float) -> dict[str, object]:
        """Best-effort initial warmup wait used to make early tasks hit.

        Args:
            min_ready: Minimum number of ready unassigned sandboxes desired.
            timeout_seconds: Maximum wall-clock seconds to wait.
        """

        details = {
            "min_ready": min_ready,
            "timeout_seconds": timeout_seconds,
            **self._pool_depth_details(),
        }
        if min_ready <= 0 or timeout_seconds <= 0:
            result = {
                **details,
                "exit_reason": "skipped",
                "elapsed_seconds": 0.0,
                "ready_count": max(len(self._assignments) - len(self._assigned), 0),
                "pending_future_count": sum(1 for future in self._futures.values() if not future.done()),
            }
            self._events.prepared_event(
                case_id="",
                message=PreparedSandboxEventMessage.INITIAL_WARMUP_FINISHED,
                status=CaseStatus.PROVISIONING,
                details=result,
            )
            return result
        self._events.prepared_event(
            case_id="",
            message=PreparedSandboxEventMessage.INITIAL_WARMUP_STARTED,
            status=CaseStatus.PROVISIONING,
            details=details,
        )
        result = await _wait_for_initial_ready(
            assignments=self._assignments,
            assigned=self._assigned,
            futures=self._futures,
            store=self._store,
            runner_id=self._config.runner_id,
            ready_changed=self._ready_changed,
            min_ready=min_ready,
            timeout_seconds=timeout_seconds,
        )
        result = {**details, **result}
        self._events.prepared_event(
            case_id="",
            message=PreparedSandboxEventMessage.INITIAL_WARMUP_FINISHED,
            status=CaseStatus.PROVISIONING,
            details=result,
        )
        return result

    def has_foreign_claim_blocker(
        self,
        *,
        max_attempts_by_task: dict[str, int],
        stale_after_seconds: float,
    ) -> bool:
        """Return whether a live foreign reservation blocks a claimable task.

        Args:
            max_attempts_by_task: Per-task retry budget used by the scheduler.
            stale_after_seconds: Reservation heartbeat grace for other runners.

        Returns:
            ``True`` when another runner owns a reservation for an otherwise
            claimable task attempt.
        """

        return (
            self._store.foreign_claim_blocking_count(
                runner_id=self._config.runner_id,
                max_attempts_by_task=max_attempts_by_task,
                stale_after_seconds=stale_after_seconds,
            )
            > 0
        )

    async def has_foreign_claim_blocker_async(
        self,
        *,
        max_attempts_by_task: dict[str, int],
        stale_after_seconds: float,
    ) -> bool:
        """Return foreign blocker state without blocking the event loop.

        Args:
            max_attempts_by_task: Maximum allowed attempts by manifest task id.
            stale_after_seconds: Lease age after which another runner's claim
                no longer blocks this runner.

        Returns:
            ``True`` when another runner owns a live prepared reservation for a
            claimable task attempt.
        """

        return await repository_call(
            lambda: self.has_foreign_claim_blocker(
                max_attempts_by_task=max_attempts_by_task,
                stale_after_seconds=stale_after_seconds,
            )
        )

    def ready_unassigned_task_attempts(self) -> set[tuple[str, int]]:
        """Return prepared task attempts that can be claimed without waiting.

        This intentionally uses the manager's in-memory assignment map rather
        than SQLite alone: a ready row from a previous process cannot be safely
        reused without its live lease object, and pressure-mode claim filtering
        must not accidentally fall back to direct create.

        Returns:
            ``(task_id, attempt)`` pairs that already have a live prepared
            sandbox assignment and are not yet claimed.
        """

        return {key for key in self._assignments if key not in self._assigned}

    def is_ready_unassigned(self, *, task_id: str, attempt: int) -> bool:
        """Return whether a claim can still use a ready prepared assignment.

        Args:
            task_id: Batch task id being considered for claim.
            attempt: One-based attempt number.

        Returns:
            ``True`` when the prepared sandbox can be assigned immediately.
        """

        key = (task_id, attempt)
        return key in self.ready_unassigned_task_attempts() and self._store.assignable(
            task_id=task_id,
            attempt=attempt,
            runner_id=self._config.runner_id,
        )

    async def is_ready_unassigned_async(self, *, task_id: str, attempt: int) -> bool:
        """Return assignability without blocking the event loop.

        Args:
            task_id: Batch task id being considered for claim.
            attempt: One-based attempt number.

        Returns:
            ``True`` when a live prepared assignment is still ready for this
            task attempt.
        """

        key = (task_id, attempt)
        if key not in self.ready_unassigned_task_attempts():
            return False
        return await repository_call(
            lambda: self._store.assignable(task_id=task_id, attempt=attempt, runner_id=self._config.runner_id)
        )

    async def claim(
        self,
        *,
        task: BatchTaskSpec,
        attempt: int,
    ) -> PreparedSandboxAssignment | None:
        """Assign a ready prepared sandbox or return ``None`` for fallback.

        Args:
            task: Task being claimed by the batch worker.
            attempt: One-based attempt number being executed.

        Returns:
            Prepared assignment when available and still valid; otherwise
            ``None`` so the caller uses normal sandbox acquisition.

        Raises:
            asyncio.CancelledError: If the attempt task itself is being
            canceled rather than a prepared future being canceled internally.
        """

        return await _claim_prepared_sandbox(self, task=task, attempt=attempt)

    def mark_released(self, assignment: PreparedSandboxAssignment) -> None:
        """Record that the existing release path retired an assigned sandbox.

        Args:
            assignment: Assignment whose sandbox lease has been released by the
                standard task teardown path.
        """

        key = assignment.key
        self._release_helper.mark_released(assignment)
        self._assignments.pop(key, None)
        self._assigned.discard(key)
        self._forget_future(key)

    async def mark_released_async(self, assignment: PreparedSandboxAssignment) -> None:
        """Record a retired assigned sandbox without blocking the event loop.

        Args:
            assignment: Assignment whose sandbox lease has been released by the
                standard task teardown path.
        """

        key = assignment.key
        await self._release_helper.mark_released_async(assignment)
        self._assignments.pop(key, None)
        self._assigned.discard(key)
        self._forget_future(key)

    async def close(self) -> None:
        """Stop preparing and clean up unassigned prepared sandboxes."""

        self._closed.set()
        self._events.prepared_event(
            case_id="",
            message=PreparedSandboxEventMessage.MANAGER_CLOSING,
            status=CaseStatus.FINALIZING,
            details={"reason": "close", **self._pool_depth_details()},
        )
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        for key, future in list(self._futures.items()):
            if key in self._assignments:
                continue
            if not future.done():
                future.cancel(PreparedSandboxFailureKind.MANAGER_CLOSED_BEFORE_ASSIGNMENT.value)
            task = task_by_id(self._config, key[0])
            if task is not None:
                self._schedule_late_future_cleanup(
                    key=key,
                    task=task,
                    future=future,
                    error="prepared sandbox manager closed before assignment",
                )
            self._forget_future(key)
        await self._release_unassigned()
        await self._wait_for_cleanup_tasks()

    async def _run_loop(self) -> None:
        await _run_prepared_sandbox_loop(self)

    def _mark_future_claim_visible(self, key: tuple[str, int]) -> None:
        future = self._futures.get(key)
        if future is not None:
            self._claim_visible_futures[key] = future

    def _forget_future(self, key: tuple[str, int]) -> None:
        self._futures.pop(key, None)
        self._claim_visible_futures.pop(key, None)
        self._abandoned_claim_keys.discard(key)

    def _abandon_claim_visible_future(
        self,
        key: tuple[str, int],
        future: asyncio.Task[PreparedSandboxAssignment | None],
        error: str,
    ) -> None:
        if key in self._abandoned_claim_keys:
            return
        self._abandoned_claim_keys.add(key)
        task = task_by_id(self._config, key[0])
        if task is None:
            return
        self._schedule_late_future_cleanup(key=key, task=task, future=future, error=error)

    def _prune_completed_futures(self) -> None:
        """Drop completed prepare tasks that no longer represent usable supply."""

        for key, future in list(self._futures.items()):
            if key in self._assignments or key in self._assigned:
                continue
            if not future.done():
                continue
            with contextlib.suppress(BaseException):
                result = future.result()
                if isinstance(result, PreparedSandboxAssignment):
                    continue
            self._forget_future(key)

    async def run_deferred_startup(
        self,
        *,
        assignment: PreparedSandboxAssignment,
        task: BatchTaskSpec,
    ) -> None:
        """Run startup commands intentionally deferred until assignment.

        Args:
            assignment: Prepared lease assigned to this task attempt.
            task: Batch task whose startup commands should be completed.

        Raises:
            RuntimeError: If deferred startup leaves the device non-ready.
        """

        await self._startup_runner.run_deferred_startup(assignment=assignment, task=task)

    async def refresh_assigned_lease_timeout(
        self,
        *,
        assignment: PreparedSandboxAssignment,
        task: BatchTaskSpec,
        reason: str,
    ) -> dict[str, object]:
        """Return TTL guard telemetry without extending on the submit hot path.

        Args:
            assignment: Prepared sandbox assignment being submitted.
            task: Batch task that owns the assignment.
            reason: Telemetry reason describing the caller phase.

        Returns:
            TTL telemetry fields for submit diagnostics.
        """

        task_timeout_seconds = resolve_task_timeout_seconds(self._config, task)
        guard_seconds = assignment_ttl_guard_seconds(config=self._config, policy=self._policy, task=task)
        now = time.time()
        details = prepared_assignment_ttl_details(
            assignment,
            config=self._config,
            task=task,
            now=now,
            guard_seconds=guard_seconds,
            task_timeout_seconds_value=task_timeout_seconds,
        )
        details["reason"] = reason
        details["lease_extension_requested"] = False
        details["lease_extension_succeeded"] = False
        details["lease_extension_skipped"] = "provider_timeout_set_at_prepare_create"
        return details

    async def _release_unassigned(self) -> None:
        for key, assignment in list(self._assignments.items()):
            if key in self._assigned:
                continue
            if key in self._cleanup_inflight_keys:
                continue
            task = task_by_id(self._config, assignment.record.task_id)
            if task is None:
                continue
            self._schedule_unassigned_assignment_release(
                task=task,
                assignment=assignment,
                final_status=PreparedSandboxStatus.CANCELED,
                error="prepared sandbox manager closed before assignment",
            )

    def _schedule_unassigned_assignment_release(
        self,
        *,
        task: BatchTaskSpec,
        assignment: PreparedSandboxAssignment,
        final_status: PreparedSandboxStatus,
        error: str,
    ) -> None:
        key = assignment.key
        if key in self._cleanup_inflight_keys:
            return
        self._cleanup_inflight_keys.add(key)
        cleanup_task = asyncio.create_task(
            self._release_unassigned_assignment(
                task=task,
                assignment=assignment,
                final_status=final_status,
                error=error,
            ),
            name=f"northcua-prepare-release:{task.task_id}:{assignment.record.attempt}",
        )
        self._track_cleanup_task(cleanup_task, key=key)

    async def _release_unassigned_assignment(
        self,
        *,
        task: BatchTaskSpec,
        assignment: PreparedSandboxAssignment,
        final_status: PreparedSandboxStatus,
        error: str,
    ) -> None:
        key = assignment.key
        try:
            await asyncio.wait_for(
                self._release_helper.release_unassigned_assignment(
                    task=task,
                    assignment=assignment,
                    final_status=final_status,
                    error=error,
                ),
                timeout=_CLOSE_CLEANUP_WAIT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "prepared sandbox unassigned release timed out",
                extra={
                    "batch_id": self._config.manifest.batch_id,
                    "task_id": task.task_id,
                    "attempt": assignment.record.attempt,
                    "sandbox_id": assignment.record.sandbox_id,
                },
            )
            await self._release_helper.mark_cleanup_failed_async(
                assignment,
                error=f"{error}; terminate_timeout: cleanup timed out after {_CLOSE_CLEANUP_WAIT_SECONDS:.1f}s",
            )
        finally:
            self._assignments.pop(key, None)
            self._assigned.discard(key)
            self._forget_future(key)

    async def _cancel_pending_prepares(self, *, error: str) -> None:
        for key, future in list(self._futures.items()):
            if key in self._assignments:
                continue
            if not future.done():
                failure_kind = (
                    PreparedSandboxFailureKind.PREPARATION_STOPPED_BY_CONTROL
                    if error == "batch control flag stopped prepared sandbox work"
                    else PreparedSandboxFailureKind.MANAGER_CLOSED_BEFORE_ASSIGNMENT
                )
                future.cancel(failure_kind.value)
            task = task_by_id(self._config, key[0])
            if task is not None:
                self._schedule_late_future_cleanup(key=key, task=task, future=future, error=error)
            self._forget_future(key)
        await self._release_unassigned()

    def _schedule_late_future_cleanup(
        self,
        *,
        key: tuple[str, int],
        task: BatchTaskSpec,
        future: asyncio.Task[PreparedSandboxAssignment | None],
        error: str,
    ) -> None:
        if key in self._cleanup_inflight_keys:
            return
        self._cleanup_inflight_keys.add(key)
        cleanup_task = asyncio.create_task(
            self._cleanup_late_future(key=key, task=task, future=future, error=error),
            name=f"northcua-prepare-cleanup:{task.task_id}:{key[1]}",
        )
        self._track_cleanup_task(cleanup_task, key=key)

    def _track_cleanup_task(self, cleanup_task: asyncio.Task[None], *, key: tuple[str, int]) -> None:
        self._cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(lambda done, cleanup_key=key: self._finish_cleanup_task(done, cleanup_key))

    def _finish_cleanup_task(self, cleanup_task: asyncio.Task[None], key: tuple[str, int]) -> None:
        self._cleanup_inflight_keys.discard(key)
        self._cleanup_tasks.discard(cleanup_task)

    async def _cleanup_late_future(
        self,
        *,
        key: tuple[str, int],
        task: BatchTaskSpec,
        future: asyncio.Task[PreparedSandboxAssignment | None],
        error: str,
    ) -> None:
        results = await asyncio.gather(future, return_exceptions=True)
        finished = results[0] if results else None
        assignment = finished if isinstance(finished, PreparedSandboxAssignment) else self._assignments.pop(key, None)
        if assignment is None:
            self._forget_future(key)
            return
        if key in self._assigned:
            return
        await self._release_unassigned_assignment(
            task=task,
            assignment=assignment,
            final_status=PreparedSandboxStatus.CANCELED,
            error=error,
        )

    async def _wait_for_cleanup_tasks(self) -> None:
        if not self._cleanup_tasks:
            return
        done, pending = await asyncio.wait(self._cleanup_tasks, timeout=_CLOSE_CLEANUP_WAIT_SECONDS)
        for task in done:
            with contextlib.suppress(BaseException):
                task.result()
        if not pending:
            return
        logger.warning(
            "prepared sandbox cleanup tasks still running after close timeout",
            extra={"batch_id": self._config.manifest.batch_id, "pending_cleanup_tasks": len(pending)},
        )

    def mark_cleanup_failed(self, assignment: PreparedSandboxAssignment, *, error: str) -> None:
        """Mark an assigned prepared row for operator cleanup.

        Args:
            assignment: Prepared assignment whose normal release failed.
            error: Redacted cleanup status from the attempt release path.
        """

        self._release_helper.mark_cleanup_failed(assignment, error=error)

    async def mark_cleanup_failed_async(self, assignment: PreparedSandboxAssignment, *, error: str) -> None:
        """Mark an assigned prepared row for operator cleanup off-loop.

        Args:
            assignment: Prepared assignment whose normal release failed.
            error: Redacted cleanup status from the attempt release path.
        """

        await self._release_helper.mark_cleanup_failed_async(assignment, error=error)

    async def mark_cleanup_deferred_async(self, assignment: PreparedSandboxAssignment, *, error: str) -> None:
        """Record that an assigned sandbox is waiting for controller reconcile.

        Args:
            assignment: Prepared assignment whose release is intentionally
                deferred because controller state may still own it.
            error: Operator-facing deferred cleanup reason.
        """

        await self._release_helper.mark_cleanup_deferred_async(assignment, error=error)

    def _pool_depth_details(self) -> dict[str, object]:
        """Return current prepared-pool depth and row counts for telemetry."""

        future_done = sum(1 for future in self._futures.values() if future.done())
        future_pending = len(self._futures) - future_done
        claim_visible_done = sum(1 for future in self._claim_visible_futures.values() if future.done())
        claim_visible_pending = len(self._claim_visible_futures) - claim_visible_done
        future_assigned = sum(1 for key in self._futures if key in self._assigned)
        future_ready_unassigned = sum(
            1 for key in self._futures if key in self._assignments and key not in self._assigned
        )
        budget_counts = self._pool_depth_budget_counts
        prepare_buffer = self._policy.effective_buffer(run_concurrency=self._run_concurrency)
        live_limit = self._run_concurrency + prepare_buffer
        live_total = int(budget_counts.get("live_total", 0))
        return {
            "run_concurrency": self._run_concurrency,
            "prepare_buffer": prepare_buffer,
            "prepared_live_limit": live_limit,
            "prepared_live_total": live_total,
            "assigned_live": int(budget_counts.get("assigned_live", 0)),
            "assigned_terminal": int(budget_counts.get("assigned_terminal", 0)),
            "cleanup_deferred": int(budget_counts.get("cleanup_deferred", 0)),
            "live_e2b_budget_overrun": max(live_total - live_limit, 0),
            "active_unassigned": self._pool_depth_active_unassigned,
            "pool_depth_store_counts_age_seconds": (
                round(max(time.monotonic() - self._pool_depth_counts_updated_at, 0.0), 3)
                if self._pool_depth_counts_updated_at > 0
                else None
            ),
            "future_count": len(self._futures),
            "future_pending_count": future_pending,
            "future_done_count": future_done,
            "scheduled_future_count": len(self._futures),
            "scheduled_future_pending_count": future_pending,
            "claim_visible_future_count": len(self._claim_visible_futures),
            "claim_visible_future_pending_count": claim_visible_pending,
            "claim_visible_future_done_count": claim_visible_done,
            "pre_reserve_future_count": max(len(self._futures) - len(self._claim_visible_futures), 0),
            "future_assigned_count": future_assigned,
            "future_ready_unassigned_count": future_ready_unassigned,
            "ready_assignment_count": max(len(self._assignments) - len(self._assigned), 0),
            "assigned_count": len(self._assigned),
            "cleanup_task_count": len(self._cleanup_tasks),
            "prepared_status_counts": dict(self._pool_depth_status_counts),
            "prepared_live_budget_counts": dict(budget_counts),
        }

    async def _refresh_pool_depth_store_counts(self) -> None:
        active_unassigned, status_counts, budget_counts = await repository_call(
            lambda: (
                self._store.active_count(runner_id=self._config.runner_id),
                self._store.status_counts(runner_id=self._config.runner_id),
                self._store.live_budget_counts(runner_id=self._config.runner_id),
            )
        )
        self._pool_depth_active_unassigned = active_unassigned
        self._pool_depth_status_counts = status_counts
        self._pool_depth_budget_counts = budget_counts
        self._pool_depth_counts_updated_at = time.monotonic()

    def _maybe_log_pool_status(
        self,
        *,
        candidate_count: int,
        scheduled_count: int,
        skipped_future_count: int,
        skipped_assignment_count: int,
        skipped_backpressure_count: int,
        backpressure_active: bool,
        control_blocked: bool,
    ) -> None:
        now = time.monotonic()
        if now - self._last_pool_status_log_at < _POOL_STATUS_LOG_INTERVAL_SECONDS:
            return
        self._last_pool_status_log_at = now
        future_done = sum(1 for future in self._futures.values() if future.done())
        future_pending = len(self._futures) - future_done
        claim_visible_done = sum(1 for future in self._claim_visible_futures.values() if future.done())
        claim_visible_pending = len(self._claim_visible_futures) - claim_visible_done
        active_unassigned = self._pool_depth_active_unassigned
        budget_counts = self._pool_depth_budget_counts
        prepare_buffer = self._policy.effective_buffer(run_concurrency=self._run_concurrency)
        live_limit = self._run_concurrency + prepare_buffer
        live_total = int(budget_counts.get("live_total", 0))
        logger.info(
            "prepared sandbox manager status: "
            "candidate_count=%s scheduled_count=%s skipped_future_count=%s "
            "future_pending_count=%s claim_visible_future_pending_count=%s active_unassigned=%s "
            "prepared_live_total=%s live_e2b_budget_overrun=%s "
            "backpressure_active=%s",
            candidate_count,
            scheduled_count,
            skipped_future_count,
            future_pending,
            claim_visible_pending,
            active_unassigned,
            live_total,
            max(live_total - live_limit, 0),
            backpressure_active,
            extra={
                "batch_id": self._config.manifest.batch_id,
                "runner_id": self._config.runner_id,
                "candidate_count": candidate_count,
                "scheduled_count": scheduled_count,
                "skipped_future_count": skipped_future_count,
                "skipped_assignment_count": skipped_assignment_count,
                "skipped_backpressure_count": skipped_backpressure_count,
                "future_count": len(self._futures),
                "future_pending_count": future_pending,
                "future_done_count": future_done,
                "scheduled_future_count": len(self._futures),
                "scheduled_future_pending_count": future_pending,
                "claim_visible_future_count": len(self._claim_visible_futures),
                "claim_visible_future_pending_count": claim_visible_pending,
                "claim_visible_future_done_count": claim_visible_done,
                "pre_reserve_future_count": max(len(self._futures) - len(self._claim_visible_futures), 0),
                "active_unassigned": active_unassigned,
                "prepare_buffer": prepare_buffer,
                "prepared_live_limit": live_limit,
                "prepared_live_total": live_total,
                "assigned_live": int(budget_counts.get("assigned_live", 0)),
                "assigned_terminal": int(budget_counts.get("assigned_terminal", 0)),
                "cleanup_deferred": int(budget_counts.get("cleanup_deferred", 0)),
                "live_e2b_budget_overrun": max(live_total - live_limit, 0),
                "prepared_live_budget_counts": dict(budget_counts),
                "backpressure_active": backpressure_active,
                "control_blocked": control_blocked,
            },
        )

    async def _expire_records(self) -> None:
        await _expire_prepared_records(
            config=self._config,
            store=self._store,
            events=self._events,
            futures=self._futures,
            claim_visible_futures=self._claim_visible_futures,
            assignments=self._assignments,
            assigned=self._assigned,
            schedule_release=self._schedule_unassigned_assignment_release,
        )
