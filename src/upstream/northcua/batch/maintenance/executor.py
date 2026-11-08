"""Executor for approved batch maintenance plans."""

from __future__ import annotations

import asyncio
import logging
import time

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.batch.maintenance.audit import MaintenanceAuditWriter
from northcua.batch.maintenance.contracts import (
    MaintenanceActionKind,
    MaintenanceAppliedItem,
    MaintenanceApplyResult,
    MaintenancePlan,
    MaintenancePlanItem,
)
from northcua.batch.maintenance.controller_ops import ControllerMaintenanceClient
from northcua.batch.maintenance.sandbox_ops import SandboxMaintenanceClient
from northcua.batch.maintenance.sqlite_ops import SQLiteMaintenanceStore
from northcua.maintenance.cleanup_controller_sandboxes import is_sandbox_missing_error
from northcua.sandboxes.capacity_contracts import SandboxInventoryItem
from northcua_protocol.capabilities import JsonObject

logger = logging.getLogger(__name__)

_TERMINAL_BATCH_STATUSES = {
    BatchTaskStatus.SUCCEEDED,
    BatchTaskStatus.EVALUATION_PENDING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
    BatchTaskStatus.CANCELED,
}
_ASSIGNED_PREPARED_STATUSES = {"assigned", "cleanup_deferred"}


class MaintenanceExecutor:
    """Apply typed maintenance actions through narrow operation facades."""

    def __init__(
        self,
        *,
        store: SQLiteMaintenanceStore,
        controller: ControllerMaintenanceClient | None = None,
        sandboxes: SandboxMaintenanceClient | None = None,
        sandbox_terminate_concurrency: int = 8,
    ) -> None:
        self._store = store
        self._controller = controller or ControllerMaintenanceClient()
        self._sandboxes = sandboxes or SandboxMaintenanceClient()
        self._sandbox_terminate_concurrency = max(1, sandbox_terminate_concurrency)

    async def execute(
        self,
        *,
        plan: MaintenancePlan,
        apply: bool,
        controller_url: str = "",
        token: str = "",
    ) -> MaintenanceApplyResult:
        """Execute or dry-run a maintenance plan and write audit events.

        Args:
            plan: Typed maintenance plan to process.
            apply: Whether mutations should be executed.
            controller_url: Controller URL used by cancel actions.
            token: Controller token used by cancel actions.

        Returns:
            Aggregate apply result plus per-item audit summaries.
        """

        effective_plan = plan.model_copy(update={"dry_run": not apply})
        audit = MaintenanceAuditWriter(effective_plan.batch_dir)
        results: list[MaintenanceAppliedItem] = []
        errors: list[str] = []
        if effective_plan.command in {"terminate-sandboxes", "sweep-provider-orphans"}:
            results = await self._execute_terminate_items(
                effective_plan.items,
                plan=effective_plan,
                apply=apply,
                controller_url=controller_url,
                token=token,
            )
        else:
            results = await self._execute_ordered_items(
                effective_plan.items,
                plan=effective_plan,
                apply=apply,
                controller_url=controller_url,
                token=token,
            )
        for result in results:
            if result.error:
                errors.append(result.error)
            audit.write_event(plan=effective_plan, item=result)
        applied_count = sum(1 for item in results if item.applied)
        skipped_count = sum(1 for item in results if item.skipped)
        return MaintenanceApplyResult(
            batch_id=effective_plan.batch_id,
            command=effective_plan.command,
            dry_run=effective_plan.dry_run,
            planned_count=effective_plan.planned_count,
            applied_count=applied_count,
            skipped_count=skipped_count,
            errors=errors,
            audit_path=str(audit.path),
            plan=effective_plan,
            items=results,
        )

    async def _execute_ordered_items(
        self,
        items: list[MaintenancePlanItem],
        *,
        plan: MaintenancePlan,
        apply: bool,
        controller_url: str,
        token: str,
    ) -> list[MaintenanceAppliedItem]:
        failed_controller_cancel_cases: set[str] = set()
        results: list[MaintenanceAppliedItem] = []
        for item in items:
            if item.kind == MaintenanceActionKind.MARK_CASE_CANCELED and item.case_id in failed_controller_cancel_cases:
                results.append(_skipped_with_error(item, "skipped local cancel because controller cancel failed"))
                continue
            result = await self._execute_item(
                item,
                plan=plan,
                apply=apply,
                controller_url=controller_url,
                token=token,
            )
            if item.kind == MaintenanceActionKind.CANCEL_CONTROLLER_TASK and result.error:
                failed_controller_cancel_cases.add(item.case_id)
            results.append(result)
        return results

    async def _execute_terminate_items(
        self,
        items: list[MaintenancePlanItem],
        *,
        plan: MaintenancePlan,
        apply: bool,
        controller_url: str,
        token: str,
    ) -> list[MaintenanceAppliedItem]:
        """Execute sandbox termination items with bounded provider concurrency."""

        semaphore = asyncio.Semaphore(self._sandbox_terminate_concurrency)

        async def run(item: MaintenancePlanItem) -> MaintenanceAppliedItem:
            """Run one termination item under the shared concurrency semaphore.

            Args:
                item: Planned sandbox termination or diagnostic item.

            Returns:
                Per-item maintenance result after the provider call completes
                or is skipped.
            """

            async with semaphore:
                return await self._execute_item(
                    item,
                    plan=plan,
                    apply=apply,
                    controller_url=controller_url,
                    token=token,
                )

        return list(await asyncio.gather(*(run(item) for item in items)))

    async def _execute_item(
        self,
        item: MaintenancePlanItem,
        *,
        plan: MaintenancePlan,
        apply: bool,
        controller_url: str,
        token: str,
    ) -> MaintenanceAppliedItem:
        if not apply or not item.apply_supported or item.kind == MaintenanceActionKind.DIAGNOSE:
            return _skipped(item)
        try:
            if item.kind == MaintenanceActionKind.RESET_STALE_ROW:
                expected_row = item.details.get("expected_row")
                older_than_seconds = item.details.get("older_than_seconds")
                before, after = self._store.reset_stale_pre_controller(
                    case_id=item.case_id,
                    reason=item.reason,
                    expected_row=expected_row if isinstance(expected_row, dict) else None,
                    older_than_seconds=(
                        float(older_than_seconds) if isinstance(older_than_seconds, int | float) else None
                    ),
                )
                return _applied(item, before=before, after=after)
            if item.kind == MaintenanceActionKind.RESET_STALE_IN_FLIGHT_ROW:
                expected_row = item.details.get("expected_row")
                older_than_seconds = item.details.get("older_than_seconds")
                next_attempt_after = item.details.get("next_attempt_after")
                allowed_status_values = item.details.get("allowed_statuses")
                allowed_statuses = (
                    {BatchTaskStatus(str(value)) for value in allowed_status_values if isinstance(value, str)}
                    if isinstance(allowed_status_values, list)
                    else set()
                )
                before, after = self._store.reset_stale_in_flight(
                    case_id=item.case_id,
                    reason=item.reason,
                    allowed_statuses=allowed_statuses,
                    expected_row=expected_row if isinstance(expected_row, dict) else None,
                    older_than_seconds=(
                        float(older_than_seconds) if isinstance(older_than_seconds, int | float) else None
                    ),
                    clear_controller_task_id=bool(item.details.get("clear_controller_task_id", False)),
                    last_reconciled_status=str(item.details.get("last_reconciled_status", "local_stale_reset")),
                    next_attempt_after=(
                        float(next_attempt_after) if isinstance(next_attempt_after, int | float) else None
                    ),
                    allow_active_lease=bool(item.details.get("allow_active_lease_reset", False)),
                )
                return _applied(item, before=before, after=after)
            if item.kind == MaintenanceActionKind.CLEAR_ACTIVE_LEASE:
                expected_row = item.details.get("expected_row")
                before, after = self._store.clear_active_lease(
                    case_id=item.case_id,
                    reason=item.reason,
                    expected_row=expected_row if isinstance(expected_row, dict) else None,
                    last_reconciled_status=str(
                        item.details.get("last_reconciled_status", "dead_runner_lease_pre_resume")
                    ),
                )
                return _applied(item, before=before, after=after)
            if item.kind == MaintenanceActionKind.RECONCILE_CONTROLLER_TASK:
                state = BatchTaskState.model_validate(item.details["reconciled_state"])
                expected_row = item.details.get("expected_row")
                before, after = self._store.persist_reconciled_state(
                    state,
                    expected_row=expected_row if isinstance(expected_row, dict) else None,
                    allow_active_lease=bool(item.details.get("allow_active_lease", False)),
                    clear_active_lease=bool(item.details.get("clear_active_lease", False)),
                )
                return _applied(item, before=before, after=after)
            if item.kind == MaintenanceActionKind.CANCEL_CONTROLLER_TASK:
                state = self._store.get_state(item.case_id)
                if state is None:
                    raise RuntimeError(f"unknown batch case: {item.case_id}")
                expected_row = item.details.get("expected_row")
                self._store.assert_row_unchanged(
                    item.case_id,
                    expected_row=expected_row if isinstance(expected_row, dict) else None,
                    action_name="cancel controller task",
                )
                if state.controller_task_id != item.controller_task_id:
                    raise RuntimeError(
                        f"refusing to cancel {item.case_id}; controller task changed since plan was built"
                    )
                await self._controller.cancel(
                    controller_url=controller_url,
                    token=token,
                    state=state,
                    reason=item.reason,
                )
                return _applied(
                    item,
                    before=state.model_dump(mode="json"),
                    after={"controller_cancel_sent": True},
                )
            if item.kind == MaintenanceActionKind.MARK_CASE_CANCELED:
                expected_row = item.details.get("expected_row")
                before, after = self._store.mark_case_canceled(
                    case_id=item.case_id,
                    reason=item.reason,
                    expected_row=expected_row if isinstance(expected_row, dict) else None,
                )
                return _applied(item, before=before, after=after)
            if item.kind == MaintenanceActionKind.TERMINATE_SANDBOX:
                sandbox = SandboxInventoryItem.model_validate(item.details["sandbox"])
                _assert_sandbox_termination_still_safe(
                    store=self._store,
                    sandbox=sandbox,
                    force_active=bool(item.details.get("force_active", False)),
                    allow_stale_attempt_active_lease=bool(item.details.get("allow_stale_attempt_active_lease", False)),
                    allow_prepared_sandbox_mismatch=bool(item.details.get("allow_prepared_sandbox_mismatch", False)),
                )
                await self._sandboxes.terminate(
                    item=sandbox,
                    batch_id=plan.batch_id,
                    include_connect_existing=bool(item.details.get("include_connect_existing", False)),
                )
                prepared_resolution = _mark_prepared_cleanup_resolved(self._store, sandbox)
                return _applied(
                    item,
                    before={"sandbox": sandbox.model_dump(mode="json")},
                    after={"terminated": True, "prepared_cleanup_resolution": prepared_resolution},
                )
        except Exception as exc:
            missing_result = _provider_missing_result(item, store=self._store, exc=exc)
            if missing_result is not None:
                return missing_result
            logger.warning(
                "batch maintenance action failed",
                extra={
                    "action_id": item.action_id,
                    "kind": item.kind.value,
                    "case_id": item.case_id,
                    "controller_task_id": item.controller_task_id,
                    "sandbox_id": item.sandbox_id,
                },
                exc_info=True,
            )
            return _error(item, error=f"{type(exc).__name__}: {exc}")
        return _error(item, error=f"unsupported maintenance action: {item.kind.value}")


def _assert_sandbox_termination_still_safe(
    *,
    store: SQLiteMaintenanceStore,
    sandbox: SandboxInventoryItem,
    force_active: bool,
    allow_stale_attempt_active_lease: bool = False,
    allow_prepared_sandbox_mismatch: bool = False,
) -> None:
    task_id = str(sandbox.metadata.get("task_id", ""))
    if not task_id:
        return
    state = store.get_state(task_id)
    if state is None:
        return
    row = store.row_details(task_id)
    now = time.time()
    active_lease = bool(row.get("lease_token_present")) and _float_value(row.get("lease_expires_at")) > now
    if state.status in _TERMINAL_BATCH_STATUSES:
        return
    if _truthy(sandbox.metadata.get("prepared_sandbox")) and not force_active:
        if allow_prepared_sandbox_mismatch:
            prepared_row = store.prepared_row_details(
                case_id=task_id,
                attempt=_int_value(sandbox.metadata.get("attempt"), default=0),
            )
            prepared_sandbox_id = str((prepared_row or {}).get("sandbox_id") or "")
            if (
                prepared_sandbox_id
                and prepared_sandbox_id != sandbox.sandbox_id
                and state.sandbox_id != sandbox.sandbox_id
            ):
                return
        prepared_row = store.prepared_row_details(
            case_id=task_id,
            attempt=_int_value(sandbox.metadata.get("attempt"), default=0),
        )
        if prepared_row is None:
            return
        status = str(prepared_row.get("status", ""))
        expires_at = _float_value(prepared_row.get("expires_at"))
        if status in _ASSIGNED_PREPARED_STATUSES and active_lease:
            raise RuntimeError("refusing to terminate assigned prepared sandbox with active SQLite lease")
        if status in _ASSIGNED_PREPARED_STATUSES and state.controller_task_id:
            raise RuntimeError("refusing to terminate controller-owned assigned prepared sandbox before reconcile")
        if status in _ASSIGNED_PREPARED_STATUSES:
            return
        if status in {"expired", "cleanup_failed", "failed", "canceled", "released"}:
            return
        if status not in _ASSIGNED_PREPARED_STATUSES and expires_at <= now:
            return
        raise RuntimeError("refusing to terminate active prepared sandbox; rebuild plan or use force-active")
    if active_lease and not force_active:
        if allow_stale_attempt_active_lease:
            sandbox_attempt = _int_value(sandbox.metadata.get("attempt"), default=0)
            state_attempt = int(state.attempts or 0)
            if sandbox_attempt > 0 and sandbox_attempt < state_attempt and state.sandbox_id != sandbox.sandbox_id:
                return
        raise RuntimeError("refusing to terminate sandbox with active SQLite lease")


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


def _applied(
    item: MaintenancePlanItem,
    *,
    before: JsonObject,
    after: JsonObject,
) -> MaintenanceAppliedItem:
    return MaintenanceAppliedItem(
        action_id=item.action_id,
        kind=item.kind,
        case_id=item.case_id,
        controller_task_id=item.controller_task_id,
        sandbox_id=item.sandbox_id,
        applied=True,
        before=before,
        after=after,
    )


def _mark_prepared_cleanup_resolved(
    store: SQLiteMaintenanceStore,
    sandbox: SandboxInventoryItem,
) -> JsonObject | None:
    if not _truthy(sandbox.metadata.get("prepared_sandbox")):
        return None
    task_id = str(sandbox.metadata.get("task_id") or "")
    attempt = _int_value(sandbox.metadata.get("attempt"), default=0)
    if not task_id or attempt <= 0:
        return None
    resolved = store.mark_prepared_cleanup_resolved(
        case_id=task_id,
        attempt=attempt,
        sandbox_id=sandbox.sandbox_id,
    )
    if resolved is None:
        return None
    before, after = resolved
    return {"before": before, "after": after}


def _provider_missing_result(
    item: MaintenancePlanItem,
    *,
    store: SQLiteMaintenanceStore,
    exc: Exception,
) -> MaintenanceAppliedItem | None:
    if item.kind != MaintenanceActionKind.TERMINATE_SANDBOX:
        return None
    sandbox_payload = item.details.get("sandbox")
    if not isinstance(sandbox_payload, dict):
        return None
    sandbox = SandboxInventoryItem.model_validate(sandbox_payload)
    if not is_sandbox_missing_error(exc, sandbox_id=sandbox.sandbox_id):
        return None
    prepared_resolution = _mark_prepared_cleanup_resolved(store, sandbox)
    return _applied(
        item,
        before={"sandbox": sandbox.model_dump(mode="json")},
        after={
            "provider_missing": True,
            "error": f"{type(exc).__name__}: {exc}",
            "prepared_cleanup_resolution": prepared_resolution,
        },
    )


def _skipped(item: MaintenancePlanItem) -> MaintenanceAppliedItem:
    return MaintenanceAppliedItem(
        action_id=item.action_id,
        kind=item.kind,
        case_id=item.case_id,
        controller_task_id=item.controller_task_id,
        sandbox_id=item.sandbox_id,
        skipped=True,
        before={"details": item.details},
    )


def _skipped_with_error(item: MaintenancePlanItem, error: str) -> MaintenanceAppliedItem:
    return MaintenanceAppliedItem(
        action_id=item.action_id,
        kind=item.kind,
        case_id=item.case_id,
        controller_task_id=item.controller_task_id,
        sandbox_id=item.sandbox_id,
        skipped=True,
        error=error,
        before={"details": item.details},
    )


def _error(item: MaintenancePlanItem, *, error: str) -> MaintenanceAppliedItem:
    return MaintenanceAppliedItem(
        action_id=item.action_id,
        kind=item.kind,
        case_id=item.case_id,
        controller_task_id=item.controller_task_id,
        sandbox_id=item.sandbox_id,
        error=error,
        before={"details": item.details},
    )
