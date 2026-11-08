"""Maintenance plan builders for batch control-plane repair."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Iterable
from pathlib import Path

from northcua.batch.contracts import BatchManifest, BatchTaskState, BatchTaskStatus
from northcua.batch.maintenance.contracts import (
    ControllerTaskRepairMode,
    MaintenanceActionKind,
    MaintenancePlan,
    MaintenancePlanItem,
    SandboxTerminationScope,
)
from northcua.batch.maintenance.controller_ops import (
    ControllerMaintenanceClient,
    is_terminal_controller_state,
)
from northcua.batch.maintenance.sandbox_ops import is_terminatable_batch_sandbox
from northcua.batch.maintenance.sqlite_ops import SQLiteMaintenanceStore
from northcua.batch.planner import BatchTaskPlanner
from northcua.batch.retry_policy import task_state_blocks_retry
from northcua.sandboxes.capacity_contracts import SandboxInventoryItem
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.payloads import TaskStatusSnapshot
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)

_RECONCILE_STATUSES = {
    BatchTaskStatus.SUBMITTED,
    BatchTaskStatus.RUNNING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
}
_PRE_RESUME_STALE_STATUSES = {
    BatchTaskStatus.PENDING,
    BatchTaskStatus.STARTING_SANDBOX,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE,
    BatchTaskStatus.WAITING_DEVICE,
    BatchTaskStatus.SUBMITTED,
    BatchTaskStatus.RUNNING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
    BatchTaskStatus.CANCELED,
}
_PRE_RESUME_UNKNOWN_BACKOFF_SECONDS = 60.0
_MAINTENANCE_BATCH_LIST_MIN_LIMIT = 10_000
_PRE_RESUME_RESETTABLE_STATUSES = {
    BatchTaskStatus.PENDING,
    BatchTaskStatus.STARTING_SANDBOX,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE,
    BatchTaskStatus.WAITING_DEVICE,
    BatchTaskStatus.SUBMITTED,
    BatchTaskStatus.RUNNING,
}
_TERMINAL_BATCH_STATUSES = {
    BatchTaskStatus.SUCCEEDED,
    BatchTaskStatus.EVALUATION_PENDING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
    BatchTaskStatus.CANCELED,
}
_ASSIGNED_PREPARED_STATUSES = {"assigned", "cleanup_deferred"}
_RETRYABLE_TERMINAL_BATCH_STATUSES = {
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
}


class MaintenancePlanner:
    """Build dry-run maintenance plans from SQLite/controller/provider facts."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def doctor(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        sandbox_items: Iterable[SandboxInventoryItem] = (),
    ) -> MaintenancePlan:
        """Build a read-only discrepancy report for a batch.

        Args:
            batch_id: Batch namespace to inspect.
            batch_dir: Directory containing batch state/audit files.
            store: SQLite maintenance facade.
            sandbox_items: Provider inventory rows already filtered by batch.

        Returns:
            Diagnostic maintenance plan with no applyable mutations.
        """

        items: list[MaintenancePlanItem] = []
        for state in store.all_states():
            row = store.row_details(state.task_id)
            if state.status in _RECONCILE_STATUSES and state.controller_task_id:
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        controller_task_id=state.controller_task_id,
                        reason="submitted work has a controller task id; reconcile before retrying",
                        apply_supported=False,
                        details={"status": state.status.value, "row": row},
                    )
                )
            elif state.status in _RECONCILE_STATUSES:
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        reason="in-flight or failed row has no controller task id",
                        apply_supported=False,
                        details={"status": state.status.value, "row": row},
                    )
                )
        for sandbox in sandbox_items:
            items.append(
                self._item(
                    kind=MaintenanceActionKind.DIAGNOSE,
                    sandbox_id=sandbox.sandbox_id,
                    provider=sandbox.provider,
                    reason="active NorthCUA sandbox visible in provider inventory",
                    apply_supported=False,
                    details={"sandbox": sandbox.model_dump(mode="json")},
                )
            )
        return MaintenancePlan(batch_id=batch_id, batch_dir=batch_dir, command="doctor", items=items)

    def repair_stale(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        older_than_seconds: float,
        case_id: str = "",
    ) -> MaintenancePlan:
        """Plan safe stale-row repairs for expired pre-controller work.

        Args:
            batch_id: Batch namespace to repair.
            batch_dir: Directory containing batch state/audit files.
            store: SQLite maintenance facade.
            older_than_seconds: Minimum age for in-flight stale rows.
            case_id: Optional single case id.

        Returns:
            Maintenance plan containing only safe pre-controller resets and
            diagnostics for rows that require controller reconciliation first.
        """

        items: list[MaintenancePlanItem] = []
        stale_states = store.stale_in_flight_states(older_than_seconds=older_than_seconds, case_id=case_id)
        for state in stale_states:
            if state.controller_task_id:
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        controller_task_id=state.controller_task_id,
                        reason="stale row has a controller task id; run reconcile before repair-stale",
                        apply_supported=False,
                        details={"status": state.status.value},
                    )
                )
                continue
            items.append(
                self._item(
                    kind=MaintenanceActionKind.RESET_STALE_ROW,
                    case_id=state.task_id,
                    reason=f"reset stale pre-controller row older than {older_than_seconds:g}s",
                    details={
                        "status": state.status.value,
                        "expected_row": store.row_details(state.task_id),
                        "older_than_seconds": older_than_seconds,
                    },
                )
            )
        return MaintenancePlan(batch_id=batch_id, batch_dir=batch_dir, command="repair-stale", items=items)

    def pre_resume_stale_repair(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        snapshots: list[TaskStatusSnapshot],
        older_than_seconds: float,
        case_id: str = "",
        include_active_leases: bool = False,
    ) -> MaintenancePlan:
        """Plan DB-backed stale active repair before a resumed runner claims work.

        The controller DB snapshots are treated as the local authority. Matching
        snapshots are persisted into SQLite, including non-terminal tasks, so a
        resumed scheduler reconciles existing work instead of opening fresh
        submitter sockets. Stale rows with no matching snapshot are moved out of
        active statuses and into normal retry/backoff ownership.

        Args:
            batch_id: Batch namespace being repaired.
            batch_dir: Concrete batch output directory.
            store: Maintenance SQLite store for local task rows.
            snapshots: Controller DB snapshots used as authoritative evidence.
        older_than_seconds: Backward-compatible parameter retained for the
            public maintenance API. DB-backed pre-resume repair now uses
            controller authority plus expired/absent local leases instead of
            waiting for a wall-clock stale threshold.
            case_id: Optional single case id.
            include_active_leases: Also plan rows with unexpired runner leases.
                This is only safe for pre-resume hard-barrier cleanup after the
                previous runner has already been stopped.

        Returns:
            Maintenance plan describing reconciliations and stale-row releases.
        """

        snapshots_by_id = {snapshot.task_id: snapshot for snapshot in snapshots}
        listed_by_task_attempt = _listed_snapshots_by_task_attempt(snapshots)
        listed_by_task = _listed_snapshots_by_task(snapshots)
        with_controller_id = store.unleased_states_for_statuses(
            statuses=_PRE_RESUME_STALE_STATUSES,
            case_id=case_id,
            controller_task_id_required=True,
        )
        without_controller_id = store.unleased_states_for_statuses(
            statuses=_PRE_RESUME_STALE_STATUSES,
            case_id=case_id,
            controller_task_id_required=False,
        )
        active_lease_states: list[BatchTaskState] = []
        if include_active_leases:
            active_lease_states = [
                *store.live_leased_states_for_statuses(
                    statuses=_PRE_RESUME_STALE_STATUSES,
                    case_id=case_id,
                    controller_task_id_required=True,
                ),
                *store.live_leased_states_for_statuses(
                    statuses=_PRE_RESUME_STALE_STATUSES,
                    case_id=case_id,
                    controller_task_id_required=False,
                    include_fresh_pending=True,
                ),
            ]
        stale_states = sorted([*with_controller_id, *without_controller_id], key=lambda state: state.task_id)
        if active_lease_states:
            states_by_task = {state.task_id: state for state in stale_states}
            states_by_task.update({state.task_id: state for state in active_lease_states})
            stale_states = sorted(states_by_task.values(), key=lambda state: state.task_id)
        items: list[MaintenancePlanItem] = []
        active_lease_count = 0
        for state in stale_states:
            expected_row = store.row_details(state.task_id)
            active_lease = _row_has_active_lease(expected_row)
            if active_lease:
                active_lease_count += 1
            snapshot_candidates: list[tuple[TaskStatusSnapshot, int, ControllerTaskRepairMode]] = []
            if state.controller_task_id:
                explicit_snapshot = snapshots_by_id.get(state.controller_task_id)
                if explicit_snapshot is not None:
                    snapshot_attempt = max(1, int(state.attempts or 1))
                    explicit_attempt = _metadata_int(explicit_snapshot, "batch_attempt")
                    if explicit_attempt > 0:
                        snapshot_attempt = explicit_attempt
                    snapshot_candidates.append(
                        (explicit_snapshot, snapshot_attempt, ControllerTaskRepairMode.EXPLICIT_ID)
                    )
            for attempt in _pre_resume_candidate_attempts(state, expected_row=expected_row):
                deterministic_id, repair_mode = _deterministic_query_for_state(batch_id, state, attempt=attempt)
                candidate = snapshots_by_id.get(deterministic_id)
                if candidate is not None and _snapshot_metadata_matches_attempt(
                    candidate,
                    batch_id=batch_id,
                    state=state,
                    attempt=attempt,
                ):
                    snapshot_candidates.append((candidate, attempt, repair_mode))
            metadata_match, metadata_attempt = _metadata_snapshot_for_state(
                state,
                expected_row=expected_row,
                listed_by_task_attempt=listed_by_task_attempt,
                listed_by_task=listed_by_task,
            )
            if metadata_match is not None:
                snapshot_candidates.append(
                    (metadata_match, metadata_attempt, ControllerTaskRepairMode.BATCH_LIST_MATCH)
                )
            snapshot: TaskStatusSnapshot | None = None
            snapshot_attempt = max(1, int(state.attempts or 1))
            repair_mode = ControllerTaskRepairMode.UNAVAILABLE
            for candidate, attempt, mode in _ordered_pre_resume_snapshot_candidates(state, snapshot_candidates):
                if not _state_already_reflects_snapshot(state, candidate, snapshot_attempt=attempt):
                    snapshot = candidate
                    snapshot_attempt = attempt
                    repair_mode = mode
                    break
            if snapshot is not None:
                state_for_snapshot = state
                if snapshot_attempt > int(state.attempts or 0):
                    state_for_snapshot = state.model_copy(update={"attempts": snapshot_attempt})
                reconciled = ControllerMaintenanceClient.state_from_snapshot(state_for_snapshot, snapshot)
                terminal = is_terminal_controller_state(snapshot.state)
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.RECONCILE_CONTROLLER_TASK,
                        case_id=state.task_id,
                        controller_task_id=snapshot.task_id,
                        reason=(
                            "persist terminal controller DB snapshot before resume"
                            if terminal
                            else "preserve non-terminal controller DB snapshot before resume"
                        ),
                        details={
                            "repair_mode": repair_mode.value,
                            "controller_snapshot_attempt": snapshot_attempt,
                            "controller_snapshot": snapshot.model_dump(mode="json"),
                            "expected_row": expected_row,
                            "reconciled_state": reconciled.model_dump(mode="json"),
                            "allow_active_lease": active_lease and include_active_leases,
                            "clear_active_lease": active_lease and include_active_leases,
                        },
                    )
                )
                continue
            if state.status in _TERMINAL_BATCH_STATUSES or (
                state.status == BatchTaskStatus.PENDING and not state.controller_task_id
            ):
                if active_lease and include_active_leases:
                    items.append(
                        self._item(
                            kind=MaintenanceActionKind.CLEAR_ACTIVE_LEASE,
                            case_id=state.task_id,
                            controller_task_id=state.controller_task_id,
                            reason="clear dead runner lease before resume while preserving local task state",
                            details={
                                "expected_row": expected_row,
                                "last_reconciled_status": "dead_runner_lease_pre_resume",
                            },
                        )
                    )
                continue
            if active_lease and include_active_leases and state.status not in _PRE_RESUME_RESETTABLE_STATUSES:
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.CLEAR_ACTIVE_LEASE,
                        case_id=state.task_id,
                        controller_task_id=state.controller_task_id,
                        reason="clear dead runner lease before resume while preserving local task state",
                        details={
                            "expected_row": expected_row,
                            "last_reconciled_status": "dead_runner_lease_pre_resume",
                        },
                    )
                )
                continue
            details: JsonObject = {
                "expected_row": expected_row,
                "allowed_statuses": [
                    status.value for status in sorted(_PRE_RESUME_STALE_STATUSES, key=lambda s: s.value)
                ],
                "last_reconciled_status": "unknown" if state.controller_task_id else "local_stale_reset",
                "allow_active_lease_reset": active_lease and include_active_leases,
            }
            if state.controller_task_id:
                details["clear_controller_task_id"] = False
                details["last_reconciled_status"] = "dead_runner_lease_pre_resume" if active_lease else "unknown"
                details["next_attempt_after"] = time.time() + _PRE_RESUME_UNKNOWN_BACKOFF_SECONDS
            elif active_lease:
                details["last_reconciled_status"] = "dead_runner_lease_pre_resume"
            items.append(
                self._item(
                    kind=MaintenanceActionKind.RESET_STALE_IN_FLIGHT_ROW,
                    case_id=state.task_id,
                    controller_task_id=state.controller_task_id,
                    reason=(
                        "release stale active row before resume; controller DB snapshot was not found, "
                        "so preserve controller/resource ids for bounded reconcile before retry"
                        if state.controller_task_id
                        else "release stale local active row before resume; no controller task id is present"
                    ),
                    details=details,
                )
            )
        return MaintenancePlan(
            batch_id=batch_id,
            batch_dir=batch_dir,
            command="pre-resume-stale-repair",
            items=items,
            metadata={
                "include_active_leases": include_active_leases,
                "active_lease_states_planned": active_lease_count,
            },
        )

    async def reconcile(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        manifest: BatchManifest | None,
        controller: ControllerMaintenanceClient,
        controller_url: str,
        token: str,
        controller_db_path: Path | None = None,
        case_id: str = "",
    ) -> MaintenancePlan:
        """Plan SQLite reconciliation from controller task snapshots.

        Args:
            batch_id: Batch namespace to reconcile.
            batch_dir: Directory containing batch state/audit files.
            store: SQLite maintenance facade.
            manifest: Optional manifest snapshot used for deterministic ids.
            controller: Controller status facade.
            controller_url: Controller websocket URL.
            token: Controller read token.
            case_id: Optional single case id.

        Returns:
            Maintenance plan containing terminal-state SQLite reconciliations
            and diagnostics for unknown/non-terminal controller tasks.
        """

        states = _filter_states(store.all_states(), case_id=case_id)
        query = _controller_id_queries(batch_id=batch_id, states=states, manifest=manifest)
        warnings: list[str] = []
        snapshots = await _fetch_snapshots(
            controller,
            controller_url=controller_url,
            token=token,
            query_ids=list(query.values()),
            controller_db_path=controller_db_path,
        )
        try:
            listed = await controller.list_batch(
                controller_url=controller_url,
                token=token,
                batch_id=batch_id,
                limit=_batch_list_limit(len(states)),
                controller_db_path=controller_db_path,
            )
        except Exception as exc:
            logger.warning(
                "batch maintenance controller batch list failed",
                extra={"batch_id": batch_id, "controller_url": controller_url},
                exc_info=True,
            )
            warnings.append(f"controller batch list failed: {type(exc).__name__}: {exc}")
            listed = []
        snapshots_by_id = {snapshot.task_id: snapshot for snapshot in [*snapshots, *listed]}
        listed_by_task_attempt = _listed_snapshots_by_task_attempt(listed)
        listed_by_task = _listed_snapshots_by_task(listed)
        items: list[MaintenancePlanItem] = []
        for state in states:
            if state.status not in _RECONCILE_STATUSES and not state.controller_task_id:
                continue
            controller_task_id = query.get(state.task_id, "")
            repair_mode = (
                ControllerTaskRepairMode.EXPLICIT_ID
                if state.controller_task_id
                else ControllerTaskRepairMode.DETERMINISTIC_ID
            )
            if not controller_task_id:
                controller_task_id, repair_mode = _deterministic_query_for_state(batch_id, state)
            snapshot = snapshots_by_id.get(controller_task_id)
            if snapshot is None:
                metadata_match, _metadata_attempt = _metadata_snapshot_for_state(
                    state,
                    listed_by_task_attempt=listed_by_task_attempt,
                    listed_by_task=listed_by_task,
                )
                if metadata_match is not None:
                    snapshot = metadata_match
                    controller_task_id = snapshot.task_id
                    repair_mode = ControllerTaskRepairMode.BATCH_LIST_MATCH
            if snapshot is None:
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        controller_task_id=controller_task_id,
                        reason="controller task was not found",
                        apply_supported=False,
                        details={"repair_mode": repair_mode.value, "status": state.status.value},
                    )
                )
                continue
            if not is_terminal_controller_state(snapshot.state):
                items.append(
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        controller_task_id=snapshot.task_id,
                        reason="controller task is still non-terminal",
                        apply_supported=False,
                        details={"controller_state": snapshot.state.value, "repair_mode": repair_mode.value},
                    )
                )
                continue
            reconciled = controller.state_from_snapshot(state, snapshot)
            items.append(
                self._item(
                    kind=MaintenanceActionKind.RECONCILE_CONTROLLER_TASK,
                    case_id=state.task_id,
                    controller_task_id=snapshot.task_id,
                    reason="persist terminal controller state into SQLite",
                    details={
                        "repair_mode": repair_mode.value,
                        "controller_snapshot": snapshot.model_dump(mode="json"),
                        "expected_row": store.row_details(state.task_id),
                        "reconciled_state": reconciled.model_dump(mode="json"),
                    },
                )
            )
        return MaintenancePlan(
            batch_id=batch_id,
            batch_dir=batch_dir,
            command="reconcile",
            items=items,
            warnings=warnings,
        )

    def cancel_case(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        state: BatchTaskState,
    ) -> MaintenancePlan:
        """Plan a safe cancellation request for one case.

        Args:
            batch_id: Batch namespace.
            batch_dir: Directory containing batch state/audit files.
            state: Current SQLite task state for the case.

        Returns:
            Maintenance plan with either a controller cancel request or a local
            pre-controller canceled-state write. Submitted controller tasks are
            reconciled into SQLite only after the controller reports a terminal
            status.
        """

        if state.status in {
            BatchTaskStatus.SUCCEEDED,
            BatchTaskStatus.FAILED,
            BatchTaskStatus.TIMED_OUT,
            BatchTaskStatus.CANCELED,
        }:
            return MaintenancePlan(
                batch_id=batch_id,
                batch_dir=batch_dir,
                command="cancel-case",
                items=[
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        reason=f"terminal case cannot be canceled without explicit reclassification: {state.status.value}",
                        apply_supported=False,
                        details={"state": state.model_dump(mode="json")},
                    )
                ],
            )
        items: list[MaintenancePlanItem] = []
        if state.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING} and not state.controller_task_id:
            return MaintenancePlan(
                batch_id=batch_id,
                batch_dir=batch_dir,
                command="cancel-case",
                items=[
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=state.task_id,
                        reason="in-flight case has no controller task id; reconcile before cancel-case",
                        apply_supported=False,
                        details={"state": state.model_dump(mode="json")},
                    )
                ],
            )
        if state.controller_task_id:
            expected_row = store.row_details(state.task_id)
            return MaintenancePlan(
                batch_id=batch_id,
                batch_dir=batch_dir,
                command="cancel-case",
                items=[
                    self._item(
                        kind=MaintenanceActionKind.CANCEL_CONTROLLER_TASK,
                        case_id=state.task_id,
                        controller_task_id=state.controller_task_id,
                        reason=(
                            "send controller cancel request; run maintenance reconcile to persist "
                            "the terminal controller status"
                        ),
                        details={
                            "state": state.model_dump(mode="json"),
                            "expected_row": expected_row,
                        },
                    )
                ],
                warnings=[
                    (
                        f"case {state.task_id} has controller task {state.controller_task_id}; "
                        "local SQLite state is not marked canceled until reconcile confirms a terminal snapshot"
                    )
                ],
            )
        items.append(
            self._item(
                kind=MaintenanceActionKind.MARK_CASE_CANCELED,
                case_id=state.task_id,
                reason="operator requested case cancellation",
                details={
                    "state": state.model_dump(mode="json"),
                    "expected_row": store.row_details(state.task_id),
                },
            )
        )
        return MaintenancePlan(batch_id=batch_id, batch_dir=batch_dir, command="cancel-case", items=items)

    def terminate_sandboxes(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        items: Iterable[SandboxInventoryItem],
        case_id: str = "",
        include_connect_existing: bool = False,
        prepared_only: bool = False,
        force_active: bool = False,
    ) -> MaintenancePlan:
        """Plan sandbox termination for provider-owned batch resources.

        Args:
            batch_id: Batch namespace.
            batch_dir: Directory containing batch state/audit files.
            items: Provider inventory rows to filter.
            case_id: Optional single case id scope.
            include_connect_existing: Whether rows without task-attempt
                metadata are eligible for termination.
            prepared_only: Whether to terminate only prepared-sandbox inventory
                rows. This is used by pre-resume janitors so normal
                non-terminal controller work can be reconciled instead of
                being killed from underneath the controller.
            force_active: Whether sandboxes for actively leased SQLite rows are
                eligible.

        Returns:
            Maintenance plan containing sandbox termination actions only for
            inventory rows with explicit NorthCUA batch ownership metadata.
        """

        scope = SandboxTerminationScope.CASE if case_id else SandboxTerminationScope.BATCH
        planned: list[MaintenancePlanItem] = []
        for sandbox in items:
            if prepared_only and not _truthy(sandbox.metadata.get("prepared_sandbox")):
                continue
            if not is_terminatable_batch_sandbox(
                sandbox,
                batch_id=batch_id,
                case_id=case_id,
                include_connect_existing=include_connect_existing,
            ):
                continue
            row_safety = _sandbox_row_safety(store, sandbox, force_active=force_active)
            if not row_safety["terminatable"]:
                planned.append(
                    self._item(
                        kind=MaintenanceActionKind.DIAGNOSE,
                        case_id=str(sandbox.metadata.get("task_id", "")),
                        sandbox_id=sandbox.sandbox_id,
                        provider=sandbox.provider,
                        reason=str(row_safety["reason"]),
                        apply_supported=False,
                        details={"sandbox": sandbox.model_dump(mode="json"), "row_safety": row_safety},
                    )
                )
                continue
            planned.append(
                self._item(
                    kind=MaintenanceActionKind.TERMINATE_SANDBOX,
                    case_id=str(sandbox.metadata.get("task_id", "")),
                    sandbox_id=sandbox.sandbox_id,
                    provider=sandbox.provider,
                    reason="terminate active sandbox owned by batch",
                    details={
                        "scope": scope.value,
                        "include_connect_existing": include_connect_existing,
                        "force_active": force_active,
                        "row_safety": row_safety,
                        "sandbox": sandbox.model_dump(mode="json"),
                    },
                )
            )
        return MaintenancePlan(batch_id=batch_id, batch_dir=batch_dir, command="terminate-sandboxes", items=planned)

    def sweep_provider_orphans(
        self,
        *,
        batch_id: str,
        batch_dir: Path,
        store: SQLiteMaintenanceStore,
        items: Iterable[SandboxInventoryItem],
        case_id: str = "",
        include_connect_existing: bool = False,
    ) -> MaintenancePlan:
        """Plan provider-inventory orphan cleanup with attempt-level protection.

        This differs from ``terminate_sandboxes`` by protecting the currently
        tracked task attempt even when an older attempt for the same task is safe
        to terminate. It is intended for pre-resume hard barriers and janitors
        after SQLite/controller repair has already run.
        """

        planned: list[MaintenancePlanItem] = []
        inventory_count = 0
        protected_count = 0
        skipped_count = 0
        candidates_by_reason: dict[str, int] = {}
        protected_by_reason: dict[str, int] = {}
        skipped_by_reason: dict[str, int] = {}
        for sandbox in items:
            inventory_count += 1
            if not is_terminatable_batch_sandbox(
                sandbox,
                batch_id=batch_id,
                case_id=case_id,
                include_connect_existing=include_connect_existing,
            ):
                skipped_count += 1
                _increment(skipped_by_reason, "out_of_scope_or_unowned")
                continue
            safety = _provider_orphan_safety(store, sandbox)
            reason_key = str(safety.get("reason_key", "unknown"))
            if not bool(safety.get("terminatable")):
                protected_count += 1
                _increment(protected_by_reason, reason_key)
                continue
            _increment(candidates_by_reason, reason_key)
            planned.append(
                self._item(
                    kind=MaintenanceActionKind.TERMINATE_SANDBOX,
                    case_id=str(sandbox.metadata.get("task_id", "")),
                    sandbox_id=sandbox.sandbox_id,
                    provider=sandbox.provider,
                    reason=f"terminate provider orphan: {safety.get('reason', reason_key)}",
                    details={
                        "scope": (
                            SandboxTerminationScope.BATCH.value if not case_id else SandboxTerminationScope.CASE.value
                        ),
                        "include_connect_existing": include_connect_existing,
                        "force_active": False,
                        "allow_stale_attempt_active_lease": bool(safety.get("allow_stale_attempt_active_lease", False)),
                        "allow_prepared_sandbox_mismatch": bool(safety.get("allow_prepared_sandbox_mismatch", False)),
                        "row_safety": safety,
                        "sandbox": sandbox.model_dump(mode="json"),
                    },
                )
            )
        metadata: JsonObject = {
            "inventory_count": inventory_count,
            "protected_count": protected_count,
            "skipped_count": skipped_count,
            "candidate_count": len(planned),
            "candidates_by_reason": candidates_by_reason,
            "protected_by_reason": protected_by_reason,
            "skipped_by_reason": skipped_by_reason,
        }
        return MaintenancePlan(
            batch_id=batch_id,
            batch_dir=batch_dir,
            command="sweep-provider-orphans",
            items=planned,
            metadata=metadata,
        )

    def _item(
        self,
        *,
        kind: MaintenanceActionKind,
        reason: str,
        case_id: str = "",
        controller_task_id: str = "",
        sandbox_id: str = "",
        provider: str = "",
        apply_supported: bool = True,
        details: JsonObject | None = None,
    ) -> MaintenancePlanItem:
        return MaintenancePlanItem(
            action_id=f"maint-{next(self._ids):04d}",
            kind=kind,
            case_id=case_id,
            controller_task_id=controller_task_id,
            sandbox_id=sandbox_id,
            provider=provider,
            reason=reason,
            apply_supported=apply_supported,
            details=details or {},
        )


async def _fetch_snapshots(
    controller: ControllerMaintenanceClient,
    *,
    controller_url: str,
    token: str,
    query_ids: list[str],
    controller_db_path: Path | None,
) -> list[TaskStatusSnapshot]:
    if not query_ids:
        return []
    return await controller.statuses(
        controller_url=controller_url,
        token=token,
        task_ids=query_ids,
        controller_db_path=controller_db_path,
    )


def _batch_list_limit(state_count: int) -> int:
    return max(_MAINTENANCE_BATCH_LIST_MIN_LIMIT, state_count * 2, state_count + 100)


def _listed_snapshots_by_task_attempt(
    snapshots: list[TaskStatusSnapshot],
) -> dict[tuple[str, int], TaskStatusSnapshot]:
    indexed: dict[tuple[str, int], TaskStatusSnapshot] = {}
    for snapshot in snapshots:
        task_id = _metadata_text(snapshot, "batch_task_id")
        attempt = _metadata_int(snapshot, "batch_attempt")
        if task_id and attempt > 0:
            indexed[(task_id, attempt)] = snapshot
    return indexed


def _listed_snapshots_by_task(snapshots: list[TaskStatusSnapshot]) -> dict[str, list[TaskStatusSnapshot]]:
    indexed: dict[str, list[TaskStatusSnapshot]] = {}
    for snapshot in snapshots:
        task_id = _metadata_text(snapshot, "batch_task_id")
        if task_id:
            indexed.setdefault(task_id, []).append(snapshot)
    return indexed


def _metadata_snapshot_for_state(
    state: BatchTaskState,
    *,
    expected_row: JsonObject | None = None,
    listed_by_task_attempt: dict[tuple[str, int], TaskStatusSnapshot],
    listed_by_task: dict[str, list[TaskStatusSnapshot]],
) -> tuple[TaskStatusSnapshot | None, int]:
    for attempt in _pre_resume_candidate_attempts(state, expected_row=expected_row):
        snapshot = listed_by_task_attempt.get((state.task_id, attempt))
        if snapshot is not None:
            return snapshot, attempt
    matches = [
        snapshot
        for snapshot in listed_by_task.get(state.task_id, [])
        if _metadata_int(snapshot, "batch_attempt") in _pre_resume_candidate_attempts(state, expected_row=expected_row)
    ]
    if len(matches) == 1:
        return matches[0], _metadata_int(matches[0], "batch_attempt")
    return None, 0


def _snapshot_metadata_matches_attempt(
    snapshot: TaskStatusSnapshot,
    *,
    batch_id: str,
    state: BatchTaskState,
    attempt: int,
) -> bool:
    return (
        _metadata_text(snapshot, "batch_id") == batch_id
        and _metadata_text(snapshot, "batch_task_id") == state.task_id
        and _metadata_int(snapshot, "batch_attempt") == attempt
    )


def _pre_resume_candidate_attempts(
    state: BatchTaskState,
    *,
    expected_row: JsonObject | None = None,
) -> list[int]:
    current = max(1, int(state.attempts or 1))
    attempts = [current]
    if state.status == BatchTaskStatus.PENDING:
        attempts = [current] if state.controller_task_id or int(state.attempts or 0) > 0 else []
    elif state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT}:
        max_attempts = _max_attempts_from_row(expected_row)
        if int(state.retry_budget_remaining or 0) > 0 or max_attempts <= 0 or current < max_attempts:
            attempts.append(current + 1)
    return sorted(set(attempts))


def _ordered_pre_resume_snapshot_candidates(
    state: BatchTaskState,
    candidates: list[tuple[TaskStatusSnapshot, int, ControllerTaskRepairMode]],
) -> list[tuple[TaskStatusSnapshot, int, ControllerTaskRepairMode]]:
    deduped: dict[tuple[str, int], tuple[TaskStatusSnapshot, int, ControllerTaskRepairMode]] = {}
    for snapshot, attempt, mode in candidates:
        deduped.setdefault((snapshot.task_id, attempt), (snapshot, attempt, mode))
    values = list(deduped.values())
    if state.status in {BatchTaskStatus.PENDING, *_TERMINAL_BATCH_STATUSES}:
        return sorted(values, key=lambda item: item[1], reverse=True)
    return sorted(values, key=lambda item: item[1])


def _max_attempts_from_row(expected_row: JsonObject | None) -> int:
    if not isinstance(expected_row, dict):
        return 0
    value = expected_row.get("max_attempts")
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _state_already_reflects_snapshot(
    state: BatchTaskState,
    snapshot: TaskStatusSnapshot,
    *,
    snapshot_attempt: int,
) -> bool:
    if (
        state.controller_task_id != snapshot.task_id
        or int(state.attempts or 0) < snapshot_attempt
        or state.last_reconciled_status != snapshot.state.value
    ):
        return False
    if is_terminal_controller_state(snapshot.state):
        state_for_snapshot = state
        if snapshot_attempt > int(state.attempts or 0):
            state_for_snapshot = state.model_copy(update={"attempts": snapshot_attempt})
        projected = ControllerMaintenanceClient.state_from_snapshot(state_for_snapshot, snapshot)
        if state.terminal_state != projected.terminal_state:
            return False
        if state.status != projected.status:
            return False
        if state.last_failure_category != projected.last_failure_category:
            return False
        if state.status != BatchTaskStatus.SUCCEEDED and state.error != projected.error:
            return False
        return True
    return state.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}


def _metadata_text(snapshot: TaskStatusSnapshot, key: str) -> str:
    value = snapshot.metadata.get(key)
    return str(value).strip() if value not in {None, ""} else ""


def _metadata_int(snapshot: TaskStatusSnapshot, key: str) -> int:
    value = snapshot.metadata.get(key)
    if not isinstance(value, int | float | str) or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _filter_states(states: list[BatchTaskState], *, case_id: str) -> list[BatchTaskState]:
    return [state for state in states if not case_id or state.task_id == case_id]


def _controller_id_queries(
    *,
    batch_id: str,
    states: list[BatchTaskState],
    manifest: BatchManifest | None,
) -> dict[str, str]:
    del manifest
    query: dict[str, str] = {}
    for state in states:
        if state.controller_task_id:
            query[state.task_id] = state.controller_task_id
            continue
        if state.status in _RECONCILE_STATUSES:
            controller_task_id, _ = _deterministic_query_for_state(batch_id, state)
            query[state.task_id] = controller_task_id
    return query


def _deterministic_query_for_state(
    batch_id: str,
    state: BatchTaskState,
    *,
    attempt: int | None = None,
) -> tuple[str, ControllerTaskRepairMode]:
    attempt = max(1, int(attempt if attempt is not None else state.attempts or 1))
    controller_task_id, _ = BatchTaskPlanner._deterministic_ids(
        batch_id=batch_id,
        task_id=state.task_id,
        attempt=attempt,
    )
    return controller_task_id, ControllerTaskRepairMode.DETERMINISTIC_ID


def _sandbox_row_safety(
    store: SQLiteMaintenanceStore,
    item: SandboxInventoryItem,
    *,
    force_active: bool,
) -> JsonObject:
    task_id = str(item.metadata.get("task_id", ""))
    if not task_id:
        return {"terminatable": True, "reason": "sandbox has no case row and is treated as orphan"}
    state = store.get_state(task_id)
    if state is None:
        return {"terminatable": True, "reason": "sandbox case id is absent from SQLite and is treated as orphan"}
    row = store.row_details(task_id)
    terminal = state.status in _TERMINAL_BATCH_STATUSES
    active_lease = bool(row.get("lease_token_present")) and _float_value(row.get("lease_expires_at")) > time.time()
    if _truthy(item.metadata.get("prepared_sandbox")) and not force_active:
        prepared_row = store.prepared_row_details(
            case_id=task_id,
            attempt=_int_value(item.metadata.get("attempt"), default=0),
        )
        if prepared_row is None:
            return {
                "terminatable": True,
                "reason": "prepared sandbox has no live reservation row and is treated as orphan",
                "row": row,
                "prepared_row": {},
            }
        status = str(prepared_row.get("status", ""))
        expires_at = _float_value(prepared_row.get("expires_at"), default=0.0)
        active_unassigned = status not in _ASSIGNED_PREPARED_STATUSES and expires_at > time.time()
        if status in {"expired", "cleanup_failed", "failed", "canceled", "released"} or (
            status not in _ASSIGNED_PREPARED_STATUSES and expires_at <= time.time()
        ):
            return {
                "terminatable": True,
                "reason": "prepared sandbox reservation is terminal or expired",
                "row": row,
                "prepared_row": prepared_row,
            }
        if terminal:
            if active_unassigned and _terminal_row_can_retry(state=state, row=row, prepared_row=prepared_row):
                return {
                    "terminatable": False,
                    "reason": "prepared sandbox is active for a retryable terminal case",
                    "row": row,
                    "prepared_row": prepared_row,
                }
            return {
                "terminatable": True,
                "reason": "prepared sandbox belongs to a terminal case with no claimable retry",
                "row": row,
                "prepared_row": prepared_row,
            }
        if status in _ASSIGNED_PREPARED_STATUSES and active_lease:
            return {
                "terminatable": False,
                "reason": "assigned prepared sandbox has an active SQLite lease; pass --force-active to clear",
                "row": row,
                "prepared_row": prepared_row,
            }
        if status in _ASSIGNED_PREPARED_STATUSES and state.controller_task_id:
            return {
                "terminatable": False,
                "reason": (
                    "assigned prepared sandbox still has a controller task id; "
                    "reconcile controller terminal state or pass --force-active to clear"
                ),
                "row": row,
                "prepared_row": prepared_row,
            }
        if status in _ASSIGNED_PREPARED_STATUSES:
            return {
                "terminatable": True,
                "reason": "assigned prepared sandbox has no active SQLite lease and is treated as stale orphan",
                "row": row,
                "prepared_row": prepared_row,
            }
        return {
            "terminatable": False,
            "reason": "prepared sandbox is active; pass --force-active to clear",
            "row": row,
            "prepared_row": prepared_row,
        }
    if terminal:
        return {"terminatable": True, "reason": "case is terminal", "row": row}
    if active_lease and not force_active:
        return {"terminatable": False, "reason": "case has an active SQLite lease", "row": row}
    return {"terminatable": True, "reason": "case has no active SQLite lease", "row": row}


def _provider_orphan_safety(store: SQLiteMaintenanceStore, item: SandboxInventoryItem) -> JsonObject:
    metadata = item.metadata
    task_id = str(metadata.get("task_id", ""))
    attempt = _int_value(metadata.get("attempt"), default=0)
    if not task_id:
        return {
            "terminatable": True,
            "reason_key": "missing_task_metadata",
            "reason": "batch-owned provider sandbox has no task metadata",
        }
    state = store.get_state(task_id)
    if state is None:
        return {
            "terminatable": True,
            "reason_key": "missing_sqlite_row",
            "reason": "sandbox case id is absent from SQLite",
        }
    row = store.row_details(task_id)
    current_sandbox_id = str(state.sandbox_id or "")
    if current_sandbox_id and current_sandbox_id == item.sandbox_id and state.status not in _TERMINAL_BATCH_STATUSES:
        return {
            "terminatable": False,
            "reason_key": "protected_current_sandbox_id",
            "reason": "provider sandbox matches the current SQLite task sandbox id",
            "row": row,
        }
    if _truthy(metadata.get("prepared_sandbox")):
        prepared_row = store.prepared_row_details(case_id=task_id, attempt=attempt)
        prepared_sandbox_id = str((prepared_row or {}).get("sandbox_id") or "")
        if prepared_sandbox_id and prepared_sandbox_id != item.sandbox_id:
            return {
                "terminatable": True,
                "reason_key": "prepared_sandbox_mismatch",
                "reason": "prepared provider row does not match the live prepared sandbox id",
                "allow_prepared_sandbox_mismatch": True,
                "row": row,
                "prepared_row": prepared_row or {},
            }
        prepared_safety = _sandbox_row_safety(store, item, force_active=False)
        if prepared_safety["terminatable"]:
            return {
                **prepared_safety,
                "reason_key": "prepared_orphan",
            }
        return {
            **prepared_safety,
            "reason_key": "protected_prepared",
        }
    if state.status in _TERMINAL_BATCH_STATUSES:
        return {
            "terminatable": True,
            "reason_key": "terminal_case",
            "reason": f"case is terminal: {state.status.value}",
            "row": row,
        }
    active_lease = bool(row.get("lease_token_present")) and _float_value(row.get("lease_expires_at")) > time.time()
    current_attempt = int(state.attempts or 0)
    if attempt <= 0:
        return {
            "terminatable": False,
            "reason_key": "protected_ambiguous_attempt",
            "reason": "non-prepared sandbox lacks attempt metadata",
            "row": row,
        }
    if attempt < current_attempt:
        return {
            "terminatable": True,
            "reason_key": "old_attempt",
            "reason": f"provider sandbox attempt {attempt} is older than SQLite attempt {current_attempt}",
            "allow_stale_attempt_active_lease": active_lease,
            "row": row,
        }
    if (
        state.status == BatchTaskStatus.PENDING
        and not active_lease
        and not state.controller_task_id
        and not current_sandbox_id
    ):
        return {
            "terminatable": True,
            "reason_key": "pending_without_owner",
            "reason": "pending row has no controller, lease, or current sandbox owner",
            "row": row,
        }
    return {
        "terminatable": False,
        "reason_key": "protected_current_or_future_attempt",
        "reason": f"provider sandbox attempt {attempt} is not older than SQLite attempt {current_attempt}",
        "row": row,
    }


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _terminal_row_can_retry(*, state: BatchTaskState, row: JsonObject, prepared_row: JsonObject) -> bool:
    if state.status not in _RETRYABLE_TERMINAL_BATCH_STATUSES:
        return False
    if bool(row.get("cancel_requested", False)):
        return False
    if _int_value(prepared_row.get("attempt"), default=0) != int(state.attempts or 0) + 1:
        return False
    if task_state_blocks_retry(state):
        return False
    if int(state.retry_budget_remaining or 0) > 0:
        return True
    max_attempts = _int_value(row.get("max_attempts"), default=0)
    return max_attempts > 0 and int(state.attempts or 0) < max_attempts


def _row_has_active_lease(row: JsonObject) -> bool:
    return bool(row.get("lease_token_present")) and _float_value(row.get("lease_expires_at")) > time.time()


def _float_value(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _int_value(value: object, *, default: int) -> int:
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
