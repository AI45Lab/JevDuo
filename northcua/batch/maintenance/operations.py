"""Product-facing operations for batch maintenance CLI commands."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from northcua.batch.contracts import BatchManifest, BatchRunResumeConfig, BatchTaskState, BatchTaskStatus
from northcua.batch.controller_db_status import LocalControllerDBStatusReader
from northcua.batch.maintenance.contracts import (
    MaintenanceActionKind,
    MaintenanceApplyResult,
    MaintenancePlan,
    MaintenancePlanItem,
)
from northcua.batch.maintenance.controller_ops import ControllerMaintenanceClient
from northcua.batch.maintenance.executor import MaintenanceExecutor
from northcua.batch.maintenance.planner import MaintenancePlanner
from northcua.batch.maintenance.sandbox_ops import SandboxMaintenanceClient
from northcua.batch.maintenance.sqlite_ops import SQLiteMaintenanceStore
from northcua.batch.planner import BatchTaskPlanner
from northcua.maintenance.cleanup_controller_residue import cleanup_controller_residue
from northcua.sandboxes.capacity_contracts import SandboxInventoryItem, SandboxInventoryState
from northcua_protocol.capabilities import JsonObject

logger = logging.getLogger(__name__)


class BatchMaintenanceOperations:
    """High-level maintenance use cases independent from argparse."""

    def __init__(
        self,
        *,
        planner: MaintenancePlanner | None = None,
        controller: ControllerMaintenanceClient | None = None,
        sandboxes: SandboxMaintenanceClient | None = None,
    ) -> None:
        self._planner = planner or MaintenancePlanner()
        self._controller = controller or ControllerMaintenanceClient()
        self._sandboxes = sandboxes or SandboxMaintenanceClient()

    async def _inventory(self, *, batch_dir: Path, **kwargs):
        items = await self._sandboxes.inventory(**kwargs)
        # Local controllers may use the same human-readable batch ID. Never
        # sweep allocations belonging to another output/state namespace.
        return [
            item
            for item in items
            if item.provider != "local-process"
            or item.metadata.get("local_batch_output_dir") == str(batch_dir.resolve())
        ]

    async def doctor(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        provider: str = "e2b",
        skip_sandboxes: bool = False,
    ) -> MaintenanceApplyResult:
        """Produce a read-only SQLite/controller/sandbox discrepancy report.

        Args:
            batch_id: Batch namespace to inspect.
            output_dir: Root batch output directory.
            provider: Sandbox provider id used for inventory.
            skip_sandboxes: Whether to skip provider inventory.

        Returns:
            Dry-run maintenance result containing diagnostics and warnings.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        warnings: list[str] = []
        sandbox_items = []
        if not skip_sandboxes:
            try:
                sandbox_items = await self._inventory(
                    batch_dir=batch_dir,
                    provider_id=provider,
                    batch_id=batch_id,
                )
            except Exception as exc:
                logger.warning(
                    "batch maintenance sandbox inventory failed",
                    extra={"batch_id": batch_id, "provider": provider},
                    exc_info=True,
                )
                warnings.append(f"sandbox inventory failed: {type(exc).__name__}: {exc}")
        plan = self._planner.doctor(batch_id=batch_id, batch_dir=batch_dir, store=store, sandbox_items=sandbox_items)
        plan = plan.model_copy(update={"warnings": [*plan.warnings, *warnings]})
        return await self._executor(store).execute(plan=plan, apply=False)

    async def reconcile(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        controller_url: str = "",
        token: str = "",
        controller_db: Path | None = None,
        case_id: str = "",
        apply: bool = False,
    ) -> MaintenanceApplyResult:
        """Reconcile SQLite task rows from controller terminal snapshots.

        Args:
            batch_id: Batch namespace to reconcile.
            output_dir: Root batch output directory.
            controller_url: Optional controller URL override.
            token: Optional controller read token override.
            controller_db: Optional local controller SQLite DB for DB-first status.
            case_id: Optional single case id.
            apply: Whether to write reconciled terminal states.

        Returns:
            Dry-run or applied reconciliation result.

        Raises:
            ValueError: If an explicit controller DB path is provided but cannot
                be opened for local authoritative status reads.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        manifest = self._manifest(batch_id=batch_id, output_dir=output_dir)
        resume_config = self._resume_config(batch_id=batch_id, output_dir=output_dir)
        resolved_controller_db = _resolved_controller_db(
            output_dir=output_dir,
            batch_id=batch_id,
            resume_config=resume_config,
            controller_db=controller_db,
        )
        if controller_db is not None and not LocalControllerDBStatusReader(resolved_controller_db).available():
            raise ValueError(
                f"readable controller DB is required for explicit controller DB reconcile: {controller_db}"
            )
        resolved_url, resolved_token = self._controller_runtime(
            manifest=manifest,
            resume_config=resume_config,
            controller_url=controller_url,
            token=token,
            controller_db=resolved_controller_db,
        )
        plan = await self._planner.reconcile(
            batch_id=batch_id,
            batch_dir=batch_dir,
            store=store,
            manifest=manifest,
            controller=self._controller,
            controller_url=resolved_url,
            token=resolved_token,
            controller_db_path=resolved_controller_db,
            case_id=case_id,
        )
        return await self._executor(store).execute(
            plan=plan,
            apply=apply,
            controller_url=resolved_url,
            token=resolved_token,
        )

    async def repair_stale(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        older_than_seconds: float,
        case_id: str = "",
        apply: bool = False,
    ) -> MaintenanceApplyResult:
        """Repair stale pre-controller rows without touching submitted work.

        Args:
            batch_id: Batch namespace to repair.
            output_dir: Root batch output directory.
            older_than_seconds: Minimum age for stale in-flight rows.
            case_id: Optional single case id.
            apply: Whether to write safe stale-row resets.

        Returns:
            Dry-run or applied stale repair result.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        plan = self._planner.repair_stale(
            batch_id=batch_id,
            batch_dir=batch_dir,
            store=store,
            older_than_seconds=older_than_seconds,
            case_id=case_id,
        )
        return await self._executor(store).execute(plan=plan, apply=apply)

    async def repair_pre_resume_stale(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        controller_db: Path,
        older_than_seconds: float,
        case_id: str = "",
        apply: bool = False,
        include_active_leases: bool = False,
    ) -> MaintenanceApplyResult:
        """DB-backed stale active repair used immediately before resume.

        This avoids opening controller websockets while the controller may
        already be under pressure. Any matching controller DB snapshot is
        persisted locally first; stale active rows without a snapshot are moved
        out of active statuses before the scheduler starts claiming work.

        Args:
            batch_id: Batch namespace to repair.
            output_dir: Root batch output directory.
            controller_db: Local controller SQLite DB to read as authority.
            older_than_seconds: Minimum stale age for active local rows.
            case_id: Optional single case id.
            apply: Whether to write safe stale-row resets/reconciliations.
            include_active_leases: Also include unexpired local leases. This is
                reserved for hard-barrier cleanup after the old runner has
                stopped; normal maintenance remains lease-conservative.

        Returns:
            Dry-run or applied pre-resume stale repair result.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        manifest = self._manifest(batch_id=batch_id, output_dir=output_dir)
        states = store.pre_resume_states(case_id=case_id)
        query_ids = _pre_resume_controller_query_ids(batch_id=batch_id, states=states, manifest=manifest)
        query_ids = _merge_query_ids(
            query_ids,
            _state_json_controller_query_ids(batch_db=store.path, task_ids={state.task_id for state in states}),
        )
        snapshots = LocalControllerDBStatusReader(controller_db).maintenance_statuses(query_ids)
        if snapshots is None:
            snapshots = await self._controller.statuses(
                controller_url="",
                token="",
                task_ids=query_ids,
                controller_db_path=controller_db,
            )
        snapshots_by_id = {snapshot.task_id: snapshot for snapshot in snapshots}
        plan = self._planner.pre_resume_stale_repair(
            batch_id=batch_id,
            batch_dir=batch_dir,
            store=store,
            snapshots=list(snapshots_by_id.values()),
            older_than_seconds=older_than_seconds,
            case_id=case_id,
            include_active_leases=include_active_leases,
        )
        return await self._executor(store).execute(plan=plan, apply=apply)

    async def cancel_case(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        case_id: str,
        controller_url: str = "",
        token: str = "",
        apply: bool = False,
    ) -> MaintenanceApplyResult:
        """Cancel one case locally and, when possible, at the controller.

        Args:
            batch_id: Batch namespace to modify.
            output_dir: Root batch output directory.
            case_id: Case/task id to cancel.
            controller_url: Optional controller URL override.
            token: Optional controller token override.
            apply: Whether to send cancel and mark the case canceled.

        Returns:
            Dry-run or applied cancel result.

        Raises:
            ValueError: If the case id is not present in SQLite.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        state = store.get_state(case_id)
        if state is None:
            raise ValueError(f"unknown batch case: {case_id}")
        manifest = self._manifest(batch_id=batch_id, output_dir=output_dir)
        resolved_url, resolved_token = self._controller_runtime(
            manifest=manifest,
            resume_config=self._resume_config(batch_id=batch_id, output_dir=output_dir),
            controller_url=controller_url,
            token=token,
        )
        plan = self._planner.cancel_case(batch_id=batch_id, batch_dir=batch_dir, store=store, state=state)
        return await self._executor(store).execute(
            plan=plan,
            apply=apply,
            controller_url=resolved_url,
            token=resolved_token,
        )

    async def terminate_sandboxes(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        provider: str,
        case_id: str = "",
        include_connect_existing: bool = False,
        prepared_only: bool = False,
        force_active: bool = False,
        terminate_concurrency: int = 8,
        apply: bool = False,
    ) -> MaintenanceApplyResult:
        """Terminate provider sandboxes explicitly owned by a batch.

        Args:
            batch_id: Batch namespace whose sandboxes should be considered.
            output_dir: Root batch output directory.
            provider: Sandbox provider id.
            case_id: Optional single case id scope.
            include_connect_existing: Whether to include matching inventory
                rows without task-attempt metadata.
            prepared_only: Whether to restrict termination to prepared-sandbox
                inventory rows.
            force_active: Whether to include sandboxes whose SQLite case row
                still has an active lease.
            terminate_concurrency: Maximum concurrent provider terminate calls.
            apply: Whether to terminate matching sandboxes.

        Returns:
            Dry-run or applied sandbox termination result.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        try:
            inventory = await self._inventory(
                batch_dir=batch_dir,
                provider_id=provider,
                batch_id=batch_id,
                case_id=case_id,
            )
        except Exception as exc:
            logger.warning(
                "batch maintenance sandbox inventory failed before termination",
                extra={"batch_id": batch_id, "provider": provider, "case_id": case_id},
                exc_info=True,
            )
            plan = MaintenancePlan(
                batch_id=batch_id,
                batch_dir=batch_dir,
                command="terminate-sandboxes",
                warnings=[f"sandbox inventory failed: {type(exc).__name__}: {exc}"],
                items=[
                    MaintenancePlanItem(
                        action_id="maint-inventory-error",
                        kind=MaintenanceActionKind.DIAGNOSE,
                        provider=provider,
                        reason="sandbox inventory failed; no sandbox was terminated",
                        apply_supported=False,
                        details={"error": f"{type(exc).__name__}: {exc}", "case_id": case_id},
                    )
                ],
            )
            result = await self._executor(store, sandbox_terminate_concurrency=terminate_concurrency).execute(
                plan=plan,
                apply=False,
            )
            return result.model_copy(update={"errors": [*result.errors, *plan.warnings]})
        if prepared_only:
            inventory = _merge_db_known_prepared_sandboxes(
                store=store,
                inventory=inventory,
                batch_id=batch_id,
                provider=provider,
            )
        plan = self._planner.terminate_sandboxes(
            batch_id=batch_id,
            batch_dir=batch_dir,
            store=store,
            items=inventory,
            case_id=case_id,
            include_connect_existing=include_connect_existing,
            prepared_only=prepared_only,
            force_active=force_active,
        )
        return await self._executor(store, sandbox_terminate_concurrency=terminate_concurrency).execute(
            plan=plan,
            apply=apply,
        )

    async def sweep_provider_orphans(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        provider: str,
        case_id: str = "",
        include_connect_existing: bool = False,
        terminate_concurrency: int = 8,
        apply: bool = False,
    ) -> MaintenanceApplyResult:
        """Terminate provider-visible batch sandboxes not protected by SQLite state.

        This is the provider-inventory complement to DB/controller cleanup. It is
        intentionally attempt-aware so a live current attempt can coexist with an
        old attempt orphan for the same task while only the old attempt is cleaned.
        """

        batch_dir = output_dir / batch_id
        store = self._store(batch_id=batch_id, output_dir=output_dir)
        try:
            inventory = await self._inventory(
                batch_dir=batch_dir,
                provider_id=provider,
                batch_id=batch_id,
                case_id=case_id,
            )
        except Exception as exc:
            logger.warning(
                "batch maintenance sandbox inventory failed before provider orphan sweep",
                extra={"batch_id": batch_id, "provider": provider, "case_id": case_id},
                exc_info=True,
            )
            plan = MaintenancePlan(
                batch_id=batch_id,
                batch_dir=batch_dir,
                command="sweep-provider-orphans",
                warnings=[f"sandbox inventory failed: {type(exc).__name__}: {exc}"],
                metadata={
                    "inventory_count": 0,
                    "protected_count": 0,
                    "skipped_count": 0,
                    "candidate_count": 0,
                },
                items=[
                    MaintenancePlanItem(
                        action_id="maint-provider-orphan-inventory-error",
                        kind=MaintenanceActionKind.DIAGNOSE,
                        provider=provider,
                        reason="sandbox inventory failed; provider orphan sweep skipped",
                        apply_supported=False,
                        details={"error": f"{type(exc).__name__}: {exc}", "case_id": case_id},
                    )
                ],
            )
            result = await self._executor(store, sandbox_terminate_concurrency=terminate_concurrency).execute(
                plan=plan,
                apply=False,
            )
            return result.model_copy(update={"errors": [*result.errors, *plan.warnings]})
        plan = self._planner.sweep_provider_orphans(
            batch_id=batch_id,
            batch_dir=batch_dir,
            store=store,
            items=inventory,
            case_id=case_id,
            include_connect_existing=include_connect_existing,
        )
        return await self._executor(store, sandbox_terminate_concurrency=terminate_concurrency).execute(
            plan=plan,
            apply=apply,
        )

    async def cleanup_controller_residue(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        batch_db: Path | None = None,
        controller_db: Path | None = None,
        output_report_dir: Path | None = None,
        protect_prepared: bool = True,
        include_current_active: bool = False,
        include_prepared: bool = False,
        provider: str = "e2b",
        skip_provider: bool = False,
        apply: bool = False,
    ) -> dict[str, object]:
        """Clean stale controller/session/device residue for a batch.

        Args:
            batch_id: Batch namespace.
            output_dir: Root batch output directory.
            batch_db: Optional explicit batch SQLite path.
            controller_db: Optional explicit controller SQLite path.
            output_report_dir: Optional directory for summary/TSV outputs.
            protect_prepared: Whether unassigned prepared rows should protect
                their sandbox/device ids.
            include_current_active: Whether current active batch rows should be
                interrupted. Intended for explicit stop cleanup only.
            include_prepared: Whether unassigned prepared rows should be
                terminated. Intended for explicit stop cleanup only.
            provider: Sandbox provider id used for optional inventory.
            skip_provider: Whether to skip provider inventory/termination.
            apply: Whether to mutate controller state and terminate candidates.

        Returns:
            JSON-safe cleanup summary.
        """

        resolved_batch_db = batch_db or output_dir / batch_id / "batch_state.sqlite"
        resolved_controller_db = controller_db or _default_controller_db(output_dir=output_dir, batch_id=batch_id)
        return await cleanup_controller_residue(
            batch_db=resolved_batch_db,
            controller_db=resolved_controller_db,
            batch_id=batch_id,
            output_dir=output_report_dir,
            protect_prepared=protect_prepared,
            include_current_active=include_current_active,
            include_prepared=include_prepared,
            apply=apply,
            provider=provider,
            skip_provider=skip_provider,
            sandbox_client=self._sandboxes,
        )

    def _executor(
        self,
        store: SQLiteMaintenanceStore,
        *,
        sandbox_terminate_concurrency: int = 8,
    ) -> MaintenanceExecutor:
        return MaintenanceExecutor(
            store=store,
            controller=self._controller,
            sandboxes=self._sandboxes,
            sandbox_terminate_concurrency=sandbox_terminate_concurrency,
        )

    @staticmethod
    def _store(*, batch_id: str, output_dir: Path) -> SQLiteMaintenanceStore:
        return SQLiteMaintenanceStore(output_dir / batch_id / "batch_state.sqlite")

    @staticmethod
    def _manifest(*, batch_id: str, output_dir: Path) -> BatchManifest | None:
        path = output_dir / batch_id / "manifest.json"
        if not path.exists():
            return None
        return BatchManifest.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _resume_config(*, batch_id: str, output_dir: Path) -> BatchRunResumeConfig:
        path = output_dir / batch_id / "run_config.json"
        if not path.exists():
            return BatchRunResumeConfig()
        return BatchRunResumeConfig.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _controller_runtime(
        *,
        manifest: BatchManifest | None,
        resume_config: BatchRunResumeConfig,
        controller_url: str,
        token: str,
        controller_db: Path | None = None,
    ) -> tuple[str, str]:
        defaults = manifest.defaults if manifest is not None else None
        resolved_url = controller_url or resume_config.controller_url or (defaults.controller_url if defaults else "")
        resolved_token = token or resume_config.token or (defaults.token if defaults else "")
        if not resolved_url and controller_db is None:
            raise ValueError("controller URL is required for this maintenance command")
        if not resolved_url and not LocalControllerDBStatusReader(controller_db).available():
            raise ValueError(f"readable controller DB is required when controller URL is omitted: {controller_db}")
        return resolved_url, resolved_token


def _default_controller_db(*, output_dir: Path, batch_id: str) -> Path:
    case_run_controller = output_dir.parent / "controller.sqlite"
    if case_run_controller.exists():
        return case_run_controller
    return output_dir / batch_id / "controller.sqlite"


def _resolved_controller_db(
    *,
    output_dir: Path,
    batch_id: str,
    resume_config: BatchRunResumeConfig,
    controller_db: Path | None,
) -> Path | None:
    if controller_db is not None:
        return controller_db
    if resume_config.telemetry_controller_db_path is not None:
        return resume_config.telemetry_controller_db_path
    default = _default_controller_db(output_dir=output_dir, batch_id=batch_id)
    return default if default.exists() else None


def _filter_pre_resume_states(states: list[BatchTaskState], *, case_id: str = "") -> list[BatchTaskState]:
    statuses = {
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
    return [state for state in states if state.status in statuses and (not case_id or state.task_id == case_id)]


def _pre_resume_controller_query_ids(
    *,
    batch_id: str,
    states: list[BatchTaskState],
    manifest: BatchManifest | None,
) -> list[str]:
    del manifest
    query_ids: list[str] = []
    seen: set[str] = set()
    for state in states:
        candidates: list[str] = []
        if state.controller_task_id:
            candidates.append(state.controller_task_id)
        for attempt in _pre_resume_candidate_attempts(state):
            controller_task_id, _ = BatchTaskPlanner._deterministic_ids(
                batch_id=batch_id,
                task_id=state.task_id,
                attempt=attempt,
            )
            candidates.append(controller_task_id)
        for controller_task_id in candidates:
            if controller_task_id and controller_task_id not in seen:
                seen.add(controller_task_id)
                query_ids.append(controller_task_id)
    return query_ids


def _merge_query_ids(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value and value not in seen:
                seen.add(value)
                merged.append(value)
    return merged


def _state_json_controller_query_ids(*, batch_db: Path, task_ids: set[str]) -> list[str]:
    if not task_ids:
        return []
    query_ids: list[str] = []
    seen: set[str] = set()
    with sqlite3.connect(str(batch_db)) as connection:
        connection.row_factory = sqlite3.Row
        for chunk in _chunks(sorted(task_ids), 500):
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT task_id, state_json FROM batch_tasks WHERE task_id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["state_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                controller_task_id = str(payload.get("controller_task_id") or "")
                if controller_task_id and controller_task_id not in seen:
                    seen.add(controller_task_id)
                    query_ids.append(controller_task_id)
    return query_ids


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _pre_resume_candidate_attempts(state: BatchTaskState) -> list[int]:
    current = max(1, int(state.attempts or 1))
    attempts = [current]
    if state.status == BatchTaskStatus.PENDING:
        attempts = [current] if state.controller_task_id or int(state.attempts or 0) > 0 else []
    elif state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT}:
        if int(state.retry_budget_remaining or 0) > 0:
            attempts.append(current + 1)
    return sorted(set(attempts))


def _merge_db_known_prepared_sandboxes(
    *,
    store: SQLiteMaintenanceStore,
    inventory: list[SandboxInventoryItem],
    batch_id: str,
    provider: str,
) -> list[SandboxInventoryItem]:
    """Append DB-known prepared resources that provider inventory omitted.

    Provider inventory can be empty immediately after provider-side cleanup or
    API churn. These synthetic items still carry NorthCUA batch ownership
    metadata and are rechecked by the normal SQLite row-safety rules before any
    terminate call is attempted.
    """

    merged = list(inventory)
    known_ids = {item.sandbox_id for item in merged}
    for row in store.prepared_resource_sandbox_rows():
        sandbox_id = str(row.get("sandbox_id") or "")
        task_id = str(row.get("task_id") or "")
        if not sandbox_id or sandbox_id in known_ids or not task_id:
            continue
        attempt = _json_int(row.get("attempt"), default=0)
        if attempt <= 0:
            continue
        metadata: JsonObject = {
            "northcua": True,
            "batch_id": batch_id,
            "task_id": task_id,
            "attempt": str(attempt),
            "prepared_sandbox": "true",
            "prepared_status": str(row.get("status") or ""),
            "source": "batch_sqlite_prepared_sandboxes",
        }
        merged.append(
            SandboxInventoryItem(
                sandbox_id=sandbox_id,
                provider=str(row.get("provider") or provider),
                template=str(row.get("template") or "osworld_desktop"),
                state=SandboxInventoryState.RUNNING,
                metadata=metadata,
            )
        )
        known_ids.add(sandbox_id)
    return merged


def _json_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default
