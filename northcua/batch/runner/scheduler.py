"""Batch-level scheduling over fresh and prepared task attempts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.backpressure import ControllerBackpressureGate
from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.metrics import BatchMetrics
from northcua.batch.preparation.contracts import PreparedSandboxEventMessage
from northcua.batch.preparation.manager import PreparedSandboxManager
from northcua.batch.preparation.planner import PreparedSandboxPlanner
from northcua.batch.preparation.sqlite_store import SQLitePreparedSandboxStore
from northcua.batch.retry_policy import task_state_blocks_retry
from northcua.batch.runner.attempt_helpers import PreparedSandboxBackpressureDeferred
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.runner.executor import BatchAttemptExecutor
from northcua.batch.runner.lease_heartbeat import BatchLeaseLostError
from northcua.batch.runner.resume import BatchStatusReconciler
from northcua.batch.runner.scheduler_helpers import BatchSchedulerHelperMixin
from northcua.batch.runner.stale_active_repair import (
    StaleActiveRepairLoop,
    _log_repair_result,
    repair_stale_active_rows_once,
    start_stale_active_repair_loop,
)
from northcua.batch.runner.validation import validate_prepared_sandbox_allocations
from northcua.batch.state import BatchStateRepository, BatchTaskLease, LeasePolicy
from northcua.batch.state.async_repository import prepared_repository_call, repository_call
from northcua.reliability import FailureAction
from northcua.sandboxes.contracts import SandboxDeviceBinding
from northcua.sandboxes.device.readiness import DeviceReadinessProbe
from northcua.sandboxes.lifecycle.task_lease import SandboxTaskLeaseManager
from northcua.sandboxes.provider.registry import SandboxProviderRegistry
from northcua_protocol.capabilities import Capability

logger = logging.getLogger(__name__)
_CLAIM_LOOP_STATUS_LOG_INTERVAL_SECONDS = 30.0
try:
    _CONTROL_FLAG_POLL_INTERVAL_SECONDS = max(
        float(os.environ.get("NORTHCUA_BATCH_CONTROL_FLAG_POLL_INTERVAL_SECONDS", "1.0") or 1.0),
        0.1,
    )
except ValueError:
    _CONTROL_FLAG_POLL_INTERVAL_SECONDS = 1.0


class _ControlFlagPoller:
    """Keep batch control flags off the runner event-loop hot path."""

    def __init__(
        self,
        *,
        repository: BatchStateRepository,
        initial_flags: Mapping[str, bool],
        interval_seconds: float = _CONTROL_FLAG_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._repository = repository
        self._interval_seconds = max(interval_seconds, 0.1)
        self._flags = dict(initial_flags)
        self._task: asyncio.Task[None] | None = None

    def stopped(self) -> bool:
        """Return true when pause, drain, or cancel has been observed."""

        return _control_stopped(self._flags)

    def cancel_requested(self) -> bool:
        """Return true when a batch cancel has been observed."""

        return bool(self._flags.get("cancel_requested"))

    async def stopped_fresh(self) -> bool:
        """Refresh flags and return true when pause, drain, or cancel is set."""

        return _control_stopped(await self.refresh_once())

    async def cancel_requested_fresh(self) -> bool:
        """Refresh flags and return true when a batch cancel is set."""

        return bool((await self.refresh_once()).get("cancel_requested"))

    async def refresh_once(self) -> Mapping[str, bool]:
        """Refresh flags through the async repository adapter."""

        flags = await repository_call(self._repository.control_flags)
        self._flags = dict(flags)
        return self._flags

    def start(self) -> None:
        """Start the background refresh loop."""

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="batch-control-flag-poller")

    async def close(self) -> None:
        """Stop the background refresh loop."""

        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("batch control flag poll failed", exc_info=True)
            await asyncio.sleep(self._interval_seconds)


@dataclass
class _ClaimLoopStats:
    total_workers: int
    last_log_at: float = 0.0
    active_workers: int = 0
    claim_success_count: int = 0
    claim_none_count: int = 0
    prepared_ready_only_miss_count: int = 0
    released_due_not_ready_prepared: int = 0
    claim_backpressure_active: int = 0
    foreign_claim_blocker_count: int = 0
    next_claim_after_count: int = 0

    def maybe_log(self, *, config: BatchRunConfig, force: bool = False) -> None:
        """Emit and reset periodic claim-loop counters.

        Args:
            config: Active batch configuration used for structured log ids.
            force: Emit immediately even if the normal interval has not elapsed.
        """

        now = time.monotonic()
        if not force and now - self.last_log_at < _CLAIM_LOOP_STATUS_LOG_INTERVAL_SECONDS:
            return
        self.last_log_at = now
        logger.info(
            "batch scheduler claim loop status: "
            "workers_idle=%s claim_success_count=%s claim_none_count=%s "
            "prepared_ready_only_miss_count=%s released_due_not_ready_prepared=%s "
            "claim_backpressure_active=%s foreign_claim_blocker_count=%s next_claim_after_count=%s",
            max(self.total_workers - self.active_workers, 0),
            self.claim_success_count,
            self.claim_none_count,
            self.prepared_ready_only_miss_count,
            self.released_due_not_ready_prepared,
            self.claim_backpressure_active,
            self.foreign_claim_blocker_count,
            self.next_claim_after_count,
            extra={
                "batch_id": config.manifest.batch_id,
                "runner_id": config.runner_id,
                "total_workers": self.total_workers,
                "active_workers": self.active_workers,
                "workers_idle": max(self.total_workers - self.active_workers, 0),
                "claim_success_count": self.claim_success_count,
                "claim_none_count": self.claim_none_count,
                "prepared_ready_only_miss_count": self.prepared_ready_only_miss_count,
                "released_due_not_ready_prepared": self.released_due_not_ready_prepared,
                "claim_backpressure_active": self.claim_backpressure_active,
                "foreign_claim_blocker_count": self.foreign_claim_blocker_count,
                "next_claim_after_count": self.next_claim_after_count,
            },
        )
        self.claim_success_count = 0
        self.claim_none_count = 0
        self.prepared_ready_only_miss_count = 0
        self.released_due_not_ready_prepared = 0
        self.claim_backpressure_active = 0
        self.foreign_claim_blocker_count = 0
        self.next_claim_after_count = 0


class BatchScheduler(BatchSchedulerHelperMixin):
    """Coordinate a full batch run."""

    def __init__(
        self,
        *,
        provider_registry: SandboxProviderRegistry,
        executor: BatchAttemptExecutor,
        binding_factory: SandboxDeviceBindingFactory | None = None,
        resume_reconciler: BatchStatusReconciler | None = None,
        readiness_probe: DeviceReadinessProbe | None = None,
    ) -> None:
        self._providers = provider_registry
        self._executor = executor
        self._binding_factory = binding_factory or SandboxDeviceBindingFactory()
        self._resume_reconciler = resume_reconciler
        self._readiness_probe = readiness_probe

    async def run(
        self,
        *,
        config: BatchRunConfig,
        repository: BatchStateRepository,
        metrics: BatchMetrics,
        events: BatchEventWriter,
        output_dir: Path,
    ) -> list[BatchTaskState]:
        """Run all tasks in a batch and return terminal local states.

        Args:
            config: Active batch runtime configuration.
            repository: Authoritative local task state and lease repository.
            metrics: Metrics collector for sandbox and task timings.
            events: Writer for task and sandbox lifecycle snapshots.
            output_dir: Concrete batch output directory.

        Returns:
            Latest state for each manifest task in manifest order.
        """

        return await self._run_impl(
            config=config,
            repository=repository,
            metrics=metrics,
            events=events,
            output_dir=output_dir,
        )

    async def _run_impl(
        self,
        *,
        config: BatchRunConfig,
        repository: BatchStateRepository,
        metrics: BatchMetrics,
        events: BatchEventWriter,
        output_dir: Path,
    ) -> list[BatchTaskState]:
        """Run all tasks in a batch and return terminal local states.

        Args:
            config: Active batch runtime configuration.
            repository: Authoritative local task state and lease repository.
            metrics: Metrics collector for sandbox and task timings.
            events: Writer for task and sandbox lifecycle snapshots.
            output_dir: Concrete batch output directory.

        Returns:
            Latest state for each manifest task in manifest order.

        Raises:
            ValueError: If mutually exclusive sandbox allocation features are
                enabled together or prepared queue cannot use SQLite state.
        """

        concurrency = config.concurrency or config.manifest.defaults.concurrency
        create_concurrency = self._sandbox_create_concurrency(config)
        create_semaphore = asyncio.Semaphore(create_concurrency) if create_concurrency > 0 else None
        submit_concurrency = config.controller_submit_concurrency
        submit_semaphore = asyncio.Semaphore(submit_concurrency) if submit_concurrency > 0 else None
        status_concurrency = self._controller_status_concurrency(config)
        status_semaphore = asyncio.Semaphore(status_concurrency) if status_concurrency > 0 else None
        policy = LeasePolicy.from_timeout(config.lease_timeout_seconds)
        flags = await repository_call(repository.control_flags)
        if flags.get("paused") or flags.get("drain_requested") or flags.get("cancel_requested"):
            return await repository_call(repository.all)
        control_flags = _ControlFlagPoller(repository=repository, initial_flags=flags)
        backpressure_gate = ControllerBackpressureGate.from_config(config)
        if config.prepared_sandbox_policy.enabled:
            validate_prepared_sandbox_allocations(
                manifest=config.manifest,
                policy=config.prepared_sandbox_policy,
            )
        tasks_by_id = {task.task_id: task for task in config.manifest.tasks}
        max_attempts_by_task = {
            task.task_id: self._total_attempt_budget(config=config, task=task) for task in config.manifest.tasks
        }
        prepared_manager: PreparedSandboxManager | None = None
        stale_repair_loop: StaleActiveRepairLoop | None = None
        try:
            control_flags.start()
            if backpressure_gate is not None:
                await backpressure_gate.start()
            try:
                startup_repair = await repair_stale_active_rows_once(
                    config=config,
                    repository=repository,
                    max_attempts_by_task=max_attempts_by_task,
                )
                _log_repair_result(config=config, result=startup_repair)
            except Exception:
                logger.warning(
                    "batch stale active startup repair failed",
                    extra={"batch_id": config.manifest.batch_id, "runner_id": config.runner_id},
                    exc_info=True,
                )
            stale_repair_loop = start_stale_active_repair_loop(
                config=config,
                repository=repository,
                max_attempts_by_task=max_attempts_by_task,
                stop_requested=control_flags.stopped,
            )
            prepared_manager = await self._maybe_start_prepared_manager(
                config=config,
                repository=repository,
                events=events,
                metrics=metrics,
                output_dir=output_dir,
                concurrency=concurrency,
                create_concurrency=create_concurrency,
                backpressure_gate=backpressure_gate,
            )
            claim_stats = _ClaimLoopStats(total_workers=max(concurrency, 1))

            async def _worker() -> None:
                while True:
                    prepared_ready_task_attempts: set[tuple[str, int]] | None = None
                    pressure = await backpressure_gate.active_snapshot(scope="claim")
                    pressure_reconcile_only = False
                    if pressure.active:
                        claim_stats.claim_backpressure_active += 1
                        claim_stats.maybe_log(config=config)
                        prepared_ready_task_attempts = (
                            prepared_manager.ready_unassigned_task_attempts() if prepared_manager is not None else set()
                        )
                        pressure_reconcile_only = True
                    claim = await repository_call(
                        lambda: repository.claim_next(
                            lease_owner=config.runner_id,
                            policy=policy,
                            max_attempts_by_task=max_attempts_by_task,
                            respect_prepared_reservations=config.prepared_sandbox_policy.enabled,
                            prepared_reservation_stale_after_seconds=policy.lease_timeout_seconds,
                            prepared_ready_task_attempts=prepared_ready_task_attempts,
                        )
                    )
                    if claim is None:
                        claim_stats.claim_none_count += 1
                        if prepared_ready_task_attempts is not None:
                            claim_stats.prepared_ready_only_miss_count += 1
                            claim_stats.maybe_log(config=config)
                            states = await repository_call(repository.all)
                            if _batch_has_no_remaining_work(states, max_attempts_by_task):
                                return
                            flags = await control_flags.refresh_once()
                            if _control_stopped(flags):
                                return
                            if not await backpressure_gate.wait_if_needed(
                                scope="claim",
                                stop_requested=control_flags.stopped_fresh,
                            ):
                                return
                            continue
                        has_foreign_claim_blocker = (
                            await prepared_manager.has_foreign_claim_blocker_async(
                                max_attempts_by_task=max_attempts_by_task,
                                stale_after_seconds=policy.lease_timeout_seconds,
                            )
                            if prepared_manager is not None
                            else False
                        )
                        if has_foreign_claim_blocker:
                            claim_stats.foreign_claim_blocker_count += 1
                            claim_stats.maybe_log(config=config)
                            flags = await control_flags.refresh_once()
                            if flags.get("paused") or flags.get("drain_requested") or flags.get("cancel_requested"):
                                return
                            await asyncio.sleep(min(max(policy.lease_timeout_seconds, 1.0), 5.0))
                            continue
                        next_claim_after = await repository_call(
                            lambda: repository.next_claim_after(
                                lease_owner=config.runner_id,
                                max_attempts_by_task=max_attempts_by_task,
                            )
                        )
                        if next_claim_after is not None:
                            claim_stats.next_claim_after_count += 1
                            claim_stats.maybe_log(config=config)
                            flags = await control_flags.refresh_once()
                            if flags.get("paused") or flags.get("drain_requested") or flags.get("cancel_requested"):
                                return
                            await asyncio.sleep(max(0.0, min(next_claim_after - time.time(), 5.0)))
                            continue
                        claim_stats.maybe_log(config=config)
                        return
                    claim_stats.claim_success_count += 1
                    task = tasks_by_id[claim.task_id]
                    if (
                        prepared_ready_task_attempts is not None
                        and prepared_manager is not None
                        and not await prepared_manager.is_ready_unassigned_async(
                            task_id=claim.task_id, attempt=claim.state.attempts + 1
                        )
                        and not _state_needs_controller_reconciliation(
                            claim.state, max_attempts_by_task=max_attempts_by_task
                        )
                    ):
                        await self._release_claim_async(
                            repository=repository, claim=claim, state=claim.state, runner_id=config.runner_id
                        )
                        claim_stats.released_due_not_ready_prepared += 1
                        claim_stats.maybe_log(config=config)
                        await asyncio.sleep(backpressure_gate.sleep_seconds)
                        continue
                    claim_stats.active_workers += 1
                    claim_stats.maybe_log(config=config)
                    try:
                        await self._run_claimed(
                            config=config,
                            task=task,
                            claim=claim,
                            repository=repository,
                            metrics=metrics,
                            events=events.for_lease(claim),
                            output_dir=output_dir,
                            policy=policy,
                            create_semaphore=create_semaphore,
                            submit_semaphore=submit_semaphore,
                            status_semaphore=status_semaphore,
                            prepared_manager=prepared_manager,
                            backpressure_gate=backpressure_gate,
                            control_flags=control_flags,
                            defer_fresh_retry_after_reconcile=pressure_reconcile_only,
                        )
                    finally:
                        claim_stats.active_workers = max(claim_stats.active_workers - 1, 0)
                        claim_stats.maybe_log(config=config)

            await asyncio.gather(*(_worker() for _ in range(max(concurrency, 1))))
            claim_stats.maybe_log(config=config, force=True)
            return await repository_call(repository.all)
        finally:
            if stale_repair_loop is not None:
                await stale_repair_loop.close()
            if prepared_manager is not None:
                await prepared_manager.close()
            if backpressure_gate is not None:
                await backpressure_gate.close()
            await control_flags.close()

    @staticmethod
    def _total_attempt_budget(*, config: BatchRunConfig, task: BatchTaskSpec) -> int:
        """Return the maximum number of attempts allowed for a task."""

        defaults = config.manifest.defaults
        max_retries = defaults.max_retries if task.max_retries < 0 else task.max_retries
        return max_retries + 1

    async def _run_claimed(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        claim: BatchTaskLease,
        repository: BatchStateRepository,
        metrics: BatchMetrics,
        events: BatchEventWriter,
        output_dir: Path,
        policy: LeasePolicy,
        create_semaphore: asyncio.Semaphore | None,
        submit_semaphore: asyncio.Semaphore | None,
        status_semaphore: asyncio.Semaphore | None,
        prepared_manager: PreparedSandboxManager | None = None,
        backpressure_gate: ControllerBackpressureGate | None = None,
        control_flags: _ControlFlagPoller | None = None,
        defer_fresh_retry_after_reconcile: bool = False,
    ) -> BatchTaskState:
        existing = claim.state
        control_flags = control_flags or _ControlFlagPoller(repository=repository, initial_flags={})
        try:
            if existing is not None:
                reconciled = await self._reconcile_existing(
                    config=config,
                    task=task,
                    events=events,
                    existing=existing,
                    status_semaphore=status_semaphore,
                    defer_fresh_retry=defer_fresh_retry_after_reconcile,
                )
                if reconciled is not None:
                    await self._release_claim_async(
                        repository=repository, claim=claim, state=reconciled, runner_id=config.runner_id
                    )
                    return reconciled
            cancel_flag = output_dir / "cancel.requested"
            if cancel_flag.exists():
                state = BatchTaskState(task_id=task.task_id, status=BatchTaskStatus.CANCELED, error="batch canceled")
                await events.upsert_task_async(state)
                await self._release_claim_async(
                    repository=repository, claim=claim, state=state, runner_id=config.runner_id
                )
                return state
            state = await self._executor.run_task(
                config=config,
                task=task,
                events=events,
                metrics=metrics,
                output_dir=output_dir,
                claim=claim,
                policy=policy,
                create_semaphore=create_semaphore,
                submit_semaphore=submit_semaphore,
                prepared_manager=prepared_manager,
                backpressure_gate=backpressure_gate,
                backpressure_stop_requested=control_flags.cancel_requested_fresh,
            )
        except asyncio.CancelledError:
            latest = (
                await repository_call(lambda: repository.get(claim.task_id))
                or existing
                or BatchTaskState(task_id=task.task_id)
            )
            latest = latest.model_copy(
                update={
                    "retry_budget_remaining": max(
                        self._total_attempt_budget(config=config, task=task) - latest.attempts,
                        0,
                    )
                }
            )
            await self._release_claim_async(
                repository=repository, claim=claim, state=latest, runner_id=config.runner_id
            )
            logger.info(
                "batch task attempt cancelled; released local batch lease for resume",
                extra={
                    "task_id": task.task_id,
                    "runner_id": config.runner_id,
                    "status": latest.status.value,
                    "controller_task_id": latest.controller_task_id,
                },
            )
            raise
        except PreparedSandboxBackpressureDeferred:
            deferred = existing or BatchTaskState(task_id=task.task_id)
            await self._release_claim_async(
                repository=repository, claim=claim, state=deferred, runner_id=config.runner_id
            )
            return deferred
        except BatchLeaseLostError:
            logger.warning(
                "batch task attempt stopped after lease ownership was lost",
                extra={"task_id": task.task_id, "runner_id": config.runner_id},
            )
            latest = await repository_call(lambda: repository.get(task.task_id)) or existing
            if latest.status == BatchTaskStatus.CANCELED:
                await self._release_claim_async(
                    repository=repository, claim=claim, state=latest, runner_id=config.runner_id
                )
            return latest
        await self._release_claim_async(repository=repository, claim=claim, state=state, runner_id=config.runner_id)
        return state

    @staticmethod
    def _sandbox_create_concurrency(config: BatchRunConfig) -> int:
        """Return the runtime sandbox-create cap for this batch.

        E2B create calls are the scarce resource in real batches. A zero CLI
        value lets operators use an environment default while fake/local
        providers remain unconstrained.
        """

        if config.dry_run:
            return 0
        if config.sandbox_create_concurrency > 0:
            return config.sandbox_create_concurrency
        providers = {(task.sandbox or config.manifest.defaults.sandbox).provider for task in config.manifest.tasks}
        if "e2b" not in providers:
            return 0
        raw = os.environ.get("NORTHCUA_E2B_CREATE_CONCURRENCY", "1")
        try:
            return max(int(raw), 0)
        except ValueError:
            logger.warning("invalid NORTHCUA_E2B_CREATE_CONCURRENCY value; falling back to 1", extra={"value": raw})
            return 1

    @staticmethod
    def _controller_status_concurrency(config: BatchRunConfig) -> int:
        """Return the cap for controller status websocket requests.

        Status reconcile can lag fresh submit. Keeping this cap smaller than
        submit prevents resume polling from becoming a second handshake burst.
        """

        if config.controller_status_concurrency > 0:
            return config.controller_status_concurrency
        if config.controller_submit_concurrency > 0:
            return min(config.controller_submit_concurrency, 5)
        return 0

    async def _maybe_start_prepared_manager(
        self,
        *,
        config: BatchRunConfig,
        repository: BatchStateRepository,
        events: BatchEventWriter,
        metrics: BatchMetrics,
        output_dir: Path,
        concurrency: int,
        create_concurrency: int,
        backpressure_gate: ControllerBackpressureGate,
    ) -> PreparedSandboxManager | None:
        policy = config.prepared_sandbox_policy
        if not policy.enabled or config.dry_run:
            return None
        sqlite_path = getattr(repository, "path", None)
        if not isinstance(sqlite_path, Path):
            raise ValueError("prepared_sandbox queue requires SQLite batch state")
        max_attempts_by_task = {
            task.task_id: self._total_attempt_budget(config=config, task=task) for task in config.manifest.tasks
        }
        store = await prepared_repository_call(lambda: SQLitePreparedSandboxStore(sqlite_path))
        planner = PreparedSandboxPlanner(
            manifest=config.manifest,
            store=store,
            policy=policy,
            max_attempts_by_task=max_attempts_by_task,
        )
        manager = PreparedSandboxManager(
            config=config,
            store=store,
            planner=planner,
            coordinator=self._executor.lease_coordinator,
            events=events,
            metrics=metrics,
            output_dir=output_dir,
            run_concurrency=concurrency,
            create_concurrency=create_concurrency,
            backpressure_gate=backpressure_gate,
        )
        initial_candidates = await prepared_repository_call(
            lambda: planner.candidates(run_concurrency=concurrency, runner_id=config.runner_id)
        )
        initial_candidate_count = len(initial_candidates)
        await manager.start()
        initial_pressure = await backpressure_gate.active_snapshot(scope="prepare_initial")
        if bool(getattr(initial_pressure, "active", False)):
            events.prepared_event(
                case_id="",
                message=PreparedSandboxEventMessage.INITIAL_WARMUP_FINISHED,
                details={
                    "exit_reason": "backpressure",
                    "min_ready": 0,
                    "timeout_seconds": 0.0,
                    "initial_candidate_count": initial_candidate_count,
                    "prepare_buffer": policy.effective_buffer(run_concurrency=concurrency),
                    "run_concurrency": concurrency,
                },
            )
            return manager
        initial_ready = min(1, initial_candidate_count)
        await manager.wait_for_initial_ready(
            min_ready=initial_ready,
            timeout_seconds=policy.initial_warmup_wait_seconds,
        )
        return manager


def _control_stopped(flags: Mapping[str, object]) -> bool:
    return bool(flags.get("paused") or flags.get("drain_requested") or flags.get("cancel_requested"))


def _state_needs_controller_reconciliation(
    state: BatchTaskState,
    *,
    max_attempts_by_task: Mapping[str, int] | None = None,
) -> bool:
    if state.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
        return bool(state.controller_task_id)
    if state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT}:
        budget = (max_attempts_by_task or {}).get(state.task_id)
        return (
            bool(state.controller_task_id)
            and state.last_reconciled_status == "unknown"
            and (budget is None or state.attempts < budget)
        )
    return False


def _batch_has_no_remaining_work(
    states: list[BatchTaskState],
    max_attempts_by_task: dict[str, int],
) -> bool:
    for state in states:
        if state.status in {
            BatchTaskStatus.STARTING_SANDBOX,
            BatchTaskStatus.BOOTSTRAPPING_DEVICE,
            BatchTaskStatus.WAITING_DEVICE,
            BatchTaskStatus.SUBMITTED,
            BatchTaskStatus.RUNNING,
        }:
            return False
        if state.status == BatchTaskStatus.PENDING:
            budget = max_attempts_by_task.get(state.task_id)
            if budget is None or state.attempts < budget:
                return False
        if state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT}:
            if task_state_blocks_retry(state):
                continue
            budget = max_attempts_by_task.get(state.task_id)
            if budget is None or state.attempts < budget:
                return False
    return True
