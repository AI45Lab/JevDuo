"""Clean stale controller/session/device residue before resuming a batch."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from northcua.batch.planner import BatchTaskPlanner
from northcua.maintenance.cleanup_controller_models import CleanupCandidate, CleanupPaths, CleanupPlan, JsonDict
from northcua.maintenance.cleanup_controller_residue_support import (
    _backup_controller_rows,
    _blocked,
    _chunks,
    _connect,
    _ensure_controller_cleanup_indexes,
    _first_non_empty,
    _infer_batch_id,
    _int_value,
    _loads,
    _paths,
    _table_exists,
    _utc_now,
    _write_tsv,
)
from northcua.maintenance.cleanup_controller_sandboxes import (
    ACTIVE_DEVICE_STATUSES,
    terminate_candidate_sandboxes,
    terminate_live_db_known_sandboxes,
)
from northcua.sandboxes.capacity_contracts import SandboxInventoryItem

if TYPE_CHECKING:
    from northcua.batch.maintenance.sandbox_ops import SandboxMaintenanceClient

logger = logging.getLogger(__name__)

_ACTIVE_BATCH_STATUSES = {
    "starting_sandbox",
    "bootstrapping_device",
    "waiting_device",
    "submitted",
    "running",
}
_TERMINAL_BATCH_STATUSES = {"succeeded", "evaluation_pending", "failed", "timed_out", "canceled"}
_PROTECTED_PREPARED_STATUSES = {"reserved", "preparing", "prepared"}
_CLEANABLE_PREPARED_STATUSES = {"cleanup_failed", "expired", "failed", "canceled"}
_ACTIVE_TASK_STATUSES = {"submitted", "accepted", "scheduled", "running"}
_TERMINAL_TASK_STATUSES = {"finished", "failed", "interrupted"}
_ACTIVE_SESSION_STATUSES = {"pending", "leased", "running"}
_ACTIVE_COMMAND_STATUSES = {"queued", "sent", "running"}
_MAX_DETERMINISTIC_LOOKUP_ATTEMPTS = 32
PROVIDER_INVENTORY_TIMEOUT_SECONDS = 60.0


async def cleanup_controller_residue(
    *,
    batch_db: Path,
    controller_db: Path,
    batch_id: str = "",
    output_dir: Path | None = None,
    protect_prepared: bool = True,
    include_current_active: bool = False,
    include_prepared: bool = False,
    apply: bool = False,
    provider: str = "e2b",
    skip_provider: bool = False,
    sandbox_client: "SandboxMaintenanceClient | None" = None,
) -> JsonDict:
    """Dry-run or apply cleanup for stale controller/session/device rows.

    Args:
        batch_db: Batch SQLite database path.
        controller_db: Controller SQLite database path.
        batch_id: Optional explicit batch id.
        output_dir: Optional output directory for cleanup reports.
        protect_prepared: Whether active prepared sandboxes block cleanup.
        include_current_active: Whether currently active rows may be cleaned.
        include_prepared: Whether prepared sandbox rows may be cleaned.
        apply: Whether to mutate controller/provider state.
        provider: Sandbox provider id used for inventory cleanup.
        skip_provider: Whether to skip provider inventory and termination.
        sandbox_client: Optional injected sandbox maintenance client for tests.

    Returns:
        JSON-safe cleanup summary.

    The cleanup is intentionally conservative: current active batch rows and
    unassigned prepared buffer rows are protected before any controller row is
    considered. Rows are updated in-place; nothing is deleted.
    """

    batch_db = batch_db.expanduser().resolve()
    controller_db = controller_db.expanduser().resolve()
    batch_id = batch_id or _infer_batch_id(batch_db)
    paths = _paths(batch_db=batch_db, output_dir=output_dir, apply=apply)
    with _connect(batch_db) as batch_conn, _connect(controller_db) as controller_conn:
        if apply:
            _ensure_controller_cleanup_indexes(controller_conn)
        plan = _plan_cleanup(
            batch_conn=batch_conn,
            controller_conn=controller_conn,
            batch_id=batch_id,
            batch_db=batch_db,
            controller_db=controller_db,
            paths=paths,
            protect_prepared=protect_prepared,
            include_current_active=include_current_active,
            include_prepared=include_prepared,
            apply=apply,
        )
    inventory_by_sandbox: dict[str, SandboxInventoryItem] = {}
    needs_provider_cleanup = _has_provider_sandbox_candidates(plan)
    if provider and not skip_provider and needs_provider_cleanup:
        try:
            inventory_client = sandbox_client or _sandbox_maintenance_client()
            inventory = await asyncio.wait_for(
                inventory_client.inventory(
                    provider_id=provider,
                    batch_id=plan.batch_id,
                ),
                timeout=PROVIDER_INVENTORY_TIMEOUT_SECONDS,
            )
            inventory_by_sandbox = {item.sandbox_id: item for item in inventory}
        except TimeoutError:
            logger.warning("provider inventory timed out during controller residue cleanup", exc_info=True)
            plan.warnings.append(
                f"provider inventory failed: TimeoutError: timed out after {PROVIDER_INVENTORY_TIMEOUT_SECONDS:.1f}s"
            )
        except Exception as exc:  # noqa: BLE001 - provider probes must not hide DB cleanup facts.
            logger.warning("provider inventory failed during controller residue cleanup", exc_info=True)
            plan.warnings.append(f"provider inventory failed: {type(exc).__name__}: {exc}")
    if inventory_by_sandbox:
        _annotate_provider_matches(plan, inventory_by_sandbox)
    _write_outputs(plan)
    if apply:
        if provider and not skip_provider and needs_provider_cleanup:
            cleanup_client = sandbox_client or _sandbox_maintenance_client()
            if inventory_by_sandbox:
                await terminate_candidate_sandboxes(
                    plan=plan,
                    inventory_by_sandbox=inventory_by_sandbox,
                    sandbox_client=cleanup_client,
                )
            await terminate_live_db_known_sandboxes(
                plan=plan,
                sandbox_client=cleanup_client,
                provider=provider,
            )
            _write_outputs(plan)
            if _has_provider_terminate_failed(plan):
                return _summary(plan)
        with _connect(controller_db) as controller_conn:
            _backup_controller_rows(controller_conn, plan, paths.backup)
            _apply_controller_updates(controller_conn, plan)
        with _connect(controller_db) as controller_conn:
            plan.protected["residue_online_after"] = _count_candidate_online_devices(controller_conn, plan.candidates)
            _append_controller_event(controller_conn, plan, _summary(plan))
        _write_outputs(plan)
    return _summary(plan)


def build_plan(
    *,
    batch_db: Path,
    controller_db: Path,
    batch_id: str = "",
    output_dir: Path | None = None,
    protect_prepared: bool = True,
    include_current_active: bool = False,
    include_prepared: bool = False,
    apply: bool = False,
) -> CleanupPlan:
    """Build a controller residue cleanup plan without provider inventory.

    Args:
        batch_db: Batch SQLite database path.
        controller_db: Controller SQLite database path.
        batch_id: Optional explicit batch id.
        output_dir: Optional report output directory.
        protect_prepared: Whether prepared rows should protect resources.
        include_current_active: Whether current active rows may be candidates.
        include_prepared: Whether prepared rows may be candidates.
        apply: Whether the generated plan is intended for mutation.

    Returns:
        Conservative cleanup plan derived from SQLite state.
    """

    batch_db = batch_db.expanduser().resolve()
    controller_db = controller_db.expanduser().resolve()
    with _connect(batch_db) as batch_conn, _connect(controller_db) as controller_conn:
        return _plan_cleanup(
            batch_conn=batch_conn,
            controller_conn=controller_conn,
            batch_id=batch_id or _infer_batch_id(batch_db),
            batch_db=batch_db,
            controller_db=controller_db,
            paths=_paths(batch_db=batch_db, output_dir=output_dir, apply=apply),
            protect_prepared=protect_prepared,
            include_current_active=include_current_active,
            include_prepared=include_prepared,
            apply=apply,
        )


def _plan_cleanup(
    *,
    batch_conn: sqlite3.Connection,
    controller_conn: sqlite3.Connection,
    batch_id: str,
    batch_db: Path,
    controller_db: Path,
    paths: CleanupPaths,
    protect_prepared: bool,
    include_current_active: bool,
    include_prepared: bool,
    apply: bool,
) -> CleanupPlan:
    batch_rows = _batch_rows(batch_conn)
    protected = _protected_sets(
        batch_conn,
        batch_rows=batch_rows,
        protect_prepared=protect_prepared,
        protect_active_batch=not include_current_active,
    )
    current_controller_by_case = {
        str(row["task_id"]): str(row["controller_task_id"] or "")
        for row in batch_rows
        if str(row["controller_task_id"] or "")
    }
    current_task_ids = {str(row["task_id"]) for row in batch_rows}
    lookup_rows = _controller_lookup_batch_rows(batch_rows)
    lookup_task_ids = _controller_lookup_task_ids(batch_rows=lookup_rows, batch_id=batch_id)
    active_residue_task_ids, active_residue_stats = _controller_active_residue_task_ids(controller_conn)
    lookup_task_ids.update(active_residue_task_ids)
    warnings = _deterministic_lookup_cap_warnings(lookup_rows)
    protected["controller_task_lookup_id_set"] = lookup_task_ids
    protected["controller_task_lookup_row_count"] = len(lookup_rows)
    protected["controller_task_lookup_rows_skipped"] = max(len(batch_rows) - len(lookup_rows), 0)
    protected.update(active_residue_stats)
    candidates: list[CleanupCandidate] = []
    blocked: list[JsonDict] = []
    controller_rows, controller_row_stats = _controller_task_rows(
        controller_conn,
        protected_task_ids=protected["controller_task_ids"],
        lookup_task_ids=lookup_task_ids,
    )
    protected.update(controller_row_stats)
    for row in controller_rows:
        task_id = str(row["task_id"])
        metadata = _task_metadata(row)
        if metadata.get("batch_id") != batch_id and task_id not in protected["controller_task_ids"]:
            continue
        case_id = str(metadata.get("batch_task_id") or metadata.get("task_id") or "")
        if task_id in protected["controller_task_ids"]:
            blocked.append(_blocked("current_active_batch_row", task_id=task_id, case_id=case_id, metadata=metadata))
            continue
        if not case_id and metadata.get("batch_id") == batch_id:
            blocked.append(_blocked("missing_batch_task_id_metadata", task_id=task_id, metadata=metadata))
            continue
        expected_current = current_controller_by_case.get(case_id, "")
        batch_status = _batch_status(batch_rows, case_id)
        current_active_for_stop = (
            include_current_active
            and bool(expected_current)
            and expected_current == task_id
            and batch_status in _ACTIVE_BATCH_STATUSES
        )
        if expected_current and expected_current == task_id:
            if batch_status in _ACTIVE_BATCH_STATUSES and not current_active_for_stop:
                blocked.append(_blocked("current_batch_controller_task", task_id=task_id, case_id=case_id))
                continue
        stale_for_current_case = bool(case_id and case_id in current_task_ids and expected_current != task_id)
        terminal_current_case = bool(
            case_id and case_id in current_task_ids and batch_status in _TERMINAL_BATCH_STATUSES
        )
        if not stale_for_current_case and not terminal_current_case and not current_active_for_stop:
            if metadata.get("batch_id") == batch_id:
                blocked.append(_blocked("ambiguous_batch_owned_controller_task", task_id=task_id, case_id=case_id))
            continue
        session = _session_for_task(controller_conn, task_id)
        lease = _lease_for_session(controller_conn, str(session["session_id"])) if session is not None else None
        device_id = _first_non_empty(
            str(session["device_id"] or "") if session is not None else "",
            str(lease["device_id"] or "") if lease is not None else "",
            str(metadata.get("target_device_id") or ""),
        )
        device = _device(controller_conn, device_id) if device_id else None
        sandbox_id = str(metadata.get("sandbox_id") or "")
        session_id = str(session["session_id"] or "") if session is not None else ""
        lease_id = str(lease["lease_id"] or "") if lease is not None else ""
        session_status = str(session["status"] or "") if session is not None else ""
        lease_status = str(lease["status"] or "") if lease is not None else ""
        device_status = str(device["status"] or "") if device is not None else ""
        protected_reason = _protected_reason(
            protected=protected,
            task_id=task_id,
            session_id=session_id,
            lease_id=lease_id,
            device_id=device_id,
            sandbox_id=sandbox_id,
        )
        if protected_reason:
            blocked.append(
                _blocked(
                    protected_reason,
                    task_id=task_id,
                    case_id=case_id,
                    session_id=session_id,
                    lease_id=lease_id,
                    device_id=device_id,
                    sandbox_id=sandbox_id,
                )
            )
            continue
        has_live_residue = (
            str(row["status"] or "") in _ACTIVE_TASK_STATUSES
            or session_status in _ACTIVE_SESSION_STATUSES
            or lease_status in _ACTIVE_SESSION_STATUSES
            or device_status in ACTIVE_DEVICE_STATUSES
            or _session_has_active_command(controller_conn, session_id)
        )
        if not current_active_for_stop and not has_live_residue:
            continue
        if current_active_for_stop:
            reason = "current_active_stop_cleanup"
        else:
            reason = "old_controller_task_id_mismatch" if stale_for_current_case else "terminal_batch_row_residue"
        candidates.append(
            CleanupCandidate(
                controller_task_id=task_id,
                case_id=case_id,
                reason=reason,
                task_status=str(row["status"] or ""),
                trace_id=str(row["trace_id"] or ""),
                session_id=session_id,
                session_status=session_status,
                lease_id=lease_id,
                lease_status=lease_status,
                device_id=device_id,
                device_status=device_status,
                sandbox_id=sandbox_id,
                metadata=metadata,
            )
        )
    candidates.extend(
        _cleanable_prepared_residue_candidates(
            batch_conn,
            controller_conn,
            protected=protected,
            existing_sandbox_ids={item.sandbox_id for item in candidates if item.sandbox_id},
            existing_device_ids={item.device_id for item in candidates if item.device_id},
        )
    )
    orphan_candidates, orphan_blocked = _orphan_online_device_candidates(
        controller_conn,
        batch_id=batch_id,
        protected=protected,
        existing_device_ids={item.device_id for item in candidates if item.device_id},
    )
    candidates.extend(orphan_candidates)
    blocked.extend(orphan_blocked)
    if include_prepared:
        candidates.extend(
            _prepared_stop_candidates(
                batch_conn, controller_conn, existing_sandbox_ids={item.sandbox_id for item in candidates}
            )
        )
    residue_online_before = _count_candidate_online_devices(controller_conn, candidates)
    return CleanupPlan(
        batch_id=batch_id,
        apply=apply,
        batch_db=batch_db,
        controller_db=controller_db,
        paths=paths,
        protected={**protected, "residue_online_before": residue_online_before},
        candidates=candidates,
        blocked=blocked,
        warnings=warnings,
    )


def _apply_controller_updates(connection: sqlite3.Connection, plan: CleanupPlan) -> None:
    now = _utc_now()
    payload = json.dumps({"error": "controller residue cleanup interrupted stale task"}, ensure_ascii=False)
    counts = {"tasks": 0, "sessions": 0, "leases": 0, "devices": 0, "commands": 0}
    connection.execute("BEGIN IMMEDIATE")
    try:
        for candidate in plan.candidates:
            if candidate.task_status in _ACTIVE_TASK_STATUSES:
                cursor = connection.execute(
                    "UPDATE tasks SET status=?, result_json=?, finished_at=? WHERE task_id=? AND status=?",
                    ("interrupted", payload, now, candidate.controller_task_id, candidate.task_status),
                )
                counts["tasks"] += cursor.rowcount
            if candidate.session_id and candidate.session_status in _ACTIVE_SESSION_STATUSES:
                target = "interrupted" if candidate.task_status in _ACTIVE_TASK_STATUSES else "released"
                cursor = connection.execute(
                    "UPDATE sessions SET status=?, updated_at=? WHERE session_id=? AND status=?",
                    (target, now, candidate.session_id, candidate.session_status),
                )
                counts["sessions"] += cursor.rowcount
            if candidate.lease_id and candidate.lease_status in _ACTIVE_SESSION_STATUSES:
                target = "interrupted" if candidate.task_status in _ACTIVE_TASK_STATUSES else "released"
                cursor = connection.execute(
                    "UPDATE leases SET status=? WHERE lease_id=? AND status=?",
                    (target, candidate.lease_id, candidate.lease_status),
                )
                counts["leases"] += cursor.rowcount
            if candidate.device_id and candidate.device_status in ACTIVE_DEVICE_STATUSES:
                cursor = connection.execute(
                    "UPDATE devices SET status=?, last_heartbeat_at=? WHERE device_id=? AND status=?",
                    ("offline", now, candidate.device_id, candidate.device_status),
                )
                counts["devices"] += cursor.rowcount
            if candidate.session_id:
                cursor = connection.execute(
                    f"""
                    UPDATE commands SET status=?, updated_at=?
                    WHERE session_id=? AND status IN ({",".join("?" for _ in _ACTIVE_COMMAND_STATUSES)})
                    """,
                    ("cancelled", now, candidate.session_id, *sorted(_ACTIVE_COMMAND_STATUSES)),
                )
                counts["commands"] += cursor.rowcount
        connection.commit()
        plan.applied_counts = counts
    except Exception:
        logger.exception("controller residue cleanup failed; rolling back")
        connection.rollback()
        raise


def _append_controller_event(connection: sqlite3.Connection, plan: CleanupPlan, summary: JsonDict) -> None:
    if not _table_exists(connection, "events"):
        return
    connection.execute(
        """
        INSERT INTO events(trace_id, entity_type, entity_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "",
            "maintenance",
            plan.batch_id,
            "controller_residue_cleanup",
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            _utc_now(),
        ),
    )
    connection.commit()


def _write_outputs(plan: CleanupPlan) -> None:
    plan.paths.output_dir.mkdir(parents=True, exist_ok=True)
    _write_tsv(
        plan.paths.devices_tsv,
        [
            "action",
            "device_id",
            "before_status",
            "after_status",
            "reason",
            "case_id",
            "controller_task_id",
            "sandbox_id",
        ],
        [
            {
                "action": "offline_device",
                "device_id": item.device_id,
                "before_status": item.device_status,
                "after_status": "offline" if item.device_status in ACTIVE_DEVICE_STATUSES else item.device_status,
                "reason": item.reason,
                "case_id": item.case_id,
                "controller_task_id": item.controller_task_id,
                "sandbox_id": item.sandbox_id,
            }
            for item in plan.candidates
            if item.device_id
        ],
    )
    _write_tsv(
        plan.paths.sessions_tsv,
        ["action", "session_id", "before_status", "after_status", "reason", "case_id", "controller_task_id"],
        [
            {
                "action": "interrupt_or_release_session",
                "session_id": item.session_id,
                "before_status": item.session_status,
                "after_status": "interrupted" if item.task_status in _ACTIVE_TASK_STATUSES else "released",
                "reason": item.reason,
                "case_id": item.case_id,
                "controller_task_id": item.controller_task_id,
            }
            for item in plan.candidates
            if item.session_id
        ],
    )
    _write_tsv(
        plan.paths.leases_tsv,
        ["action", "lease_id", "before_status", "after_status", "reason", "case_id", "controller_task_id"],
        [
            {
                "action": "interrupt_or_release_lease",
                "lease_id": item.lease_id,
                "before_status": item.lease_status,
                "after_status": "interrupted" if item.task_status in _ACTIVE_TASK_STATUSES else "released",
                "reason": item.reason,
                "case_id": item.case_id,
                "controller_task_id": item.controller_task_id,
            }
            for item in plan.candidates
            if item.lease_id
        ],
    )
    _write_tsv(
        plan.paths.blocked_tsv,
        ["reason", "task_id", "case_id", "session_id", "lease_id", "device_id", "sandbox_id"],
        plan.blocked,
    )
    plan.paths.summary.write_text(
        json.dumps(_summary(plan), indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _summary(plan: CleanupPlan) -> JsonDict:
    provider_status_counts: dict[str, int] = {}
    for candidate in plan.candidates:
        if candidate.provider_status:
            provider_status_counts[candidate.provider_status] = (
                provider_status_counts.get(candidate.provider_status, 0) + 1
            )
    blocked_by_reason: dict[str, int] = {}
    for item in plan.blocked:
        reason = str(item.get("reason") or "unknown")
        blocked_by_reason[reason] = blocked_by_reason.get(reason, 0) + 1
    return {
        "batch_id": plan.batch_id,
        "apply": plan.apply,
        "batch_db": str(plan.batch_db),
        "controller_db": str(plan.controller_db),
        "output_dir": str(plan.paths.output_dir),
        "backup": str(plan.paths.backup or ""),
        "backup_kind": "controller_delta_json" if plan.paths.backup else "",
        "planned": {
            "tasks": len({item.controller_task_id for item in plan.candidates if item.controller_task_id}),
            "sessions": len({item.session_id for item in plan.candidates if item.session_id}),
            "leases": len({item.lease_id for item in plan.candidates if item.lease_id}),
            "devices": len({item.device_id for item in plan.candidates if item.device_id}),
            "sandboxes": len({item.sandbox_id for item in plan.candidates if item.sandbox_id}),
            "blocked": len(plan.blocked),
        },
        "provider_status": provider_status_counts,
        "provider_missing": provider_status_counts.get("provider_missing", 0),
        "blocked_by_reason": blocked_by_reason,
        "blocked_sample": plan.blocked[:10],
        "residue_online_before": plan.protected.get("residue_online_before", 0),
        "residue_online_after": plan.protected.get(
            "residue_online_after",
            plan.protected.get("residue_online_before", 0),
        ),
        "cleaned": {
            "tasks": int(plan.applied_counts.get("tasks", 0)),
            "sessions": int(plan.applied_counts.get("sessions", 0)),
            "leases": int(plan.applied_counts.get("leases", 0)),
            "devices": int(plan.applied_counts.get("devices", 0)),
            "commands": int(plan.applied_counts.get("commands", 0)),
            "sandboxes": provider_status_counts.get("terminated", 0)
            + provider_status_counts.get("provider_missing", 0),
        },
        "protected_counts": {
            key: len(value) if isinstance(value, set) else value
            for key, value in plan.protected.items()
            if key != "residue_online_before"
        },
        "warnings": list(plan.warnings),
        "outputs": {
            "summary": str(plan.paths.summary),
            "cleanup_devices_tsv": str(plan.paths.devices_tsv),
            "cleanup_sessions_tsv": str(plan.paths.sessions_tsv),
            "cleanup_leases_tsv": str(plan.paths.leases_tsv),
            "blocked_tsv": str(plan.paths.blocked_tsv),
        },
    }


def _has_provider_terminate_failed(plan: CleanupPlan) -> bool:
    return any(candidate.provider_status == "terminate_failed" for candidate in plan.candidates)


def _has_provider_sandbox_candidates(plan: CleanupPlan) -> bool:
    return any(candidate.sandbox_id for candidate in plan.candidates)


def _protected_sets(
    connection: sqlite3.Connection,
    *,
    batch_rows: list[sqlite3.Row],
    protect_prepared: bool,
    protect_active_batch: bool = True,
) -> JsonDict:
    protected = {
        "controller_task_ids": set(),
        "session_ids": set(),
        "lease_ids": set(),
        "device_ids": set(),
        "sandbox_ids": set(),
        "prepared_device_ids": set(),
        "prepared_sandbox_ids": set(),
        "active_batch_task_ids": set(),
    }
    for row in batch_rows:
        if not protect_active_batch:
            continue
        if str(row["status"]) not in _ACTIVE_BATCH_STATUSES:
            continue
        protected["active_batch_task_ids"].add(str(row["task_id"]))
        for target, column in (
            ("controller_task_ids", "controller_task_id"),
            ("session_ids", "session_id"),
            ("device_ids", "device_id"),
            ("sandbox_ids", "sandbox_id"),
            ("lease_ids", "lease_token"),
        ):
            value = str(row[column] or "") if column in row.keys() else ""
            if value:
                protected[target].add(value)
    if protect_prepared and _table_exists(connection, "prepared_sandboxes"):
        rows = connection.execute(
            """
            SELECT sandbox_id, device_id, lease_id
            FROM prepared_sandboxes
            WHERE status IN ({placeholders}) AND ({unassigned})
            """.format(
                placeholders=",".join("?" for _ in _PROTECTED_PREPARED_STATUSES),
                unassigned=_unassigned_prepared_sql(),
            ),
            tuple(sorted(_PROTECTED_PREPARED_STATUSES)),
        ).fetchall()
        for row in rows:
            if row["device_id"]:
                protected["device_ids"].add(str(row["device_id"]))
                protected["prepared_device_ids"].add(str(row["device_id"]))
            if row["sandbox_id"]:
                protected["sandbox_ids"].add(str(row["sandbox_id"]))
                protected["prepared_sandbox_ids"].add(str(row["sandbox_id"]))
            if row["lease_id"]:
                protected["lease_ids"].add(str(row["lease_id"]))
    return protected


def _prepared_stop_candidates(
    batch_conn: sqlite3.Connection,
    controller_conn: sqlite3.Connection,
    *,
    existing_sandbox_ids: set[str],
) -> list[CleanupCandidate]:
    if not _table_exists(batch_conn, "prepared_sandboxes"):
        return []
    rows = batch_conn.execute(
        """
        SELECT task_id, sandbox_id, device_id, lease_id, status
        FROM prepared_sandboxes
        WHERE status IN ({placeholders}) AND ({unassigned})
        """.format(
            placeholders=",".join("?" for _ in _PROTECTED_PREPARED_STATUSES),
            unassigned=_unassigned_prepared_sql(),
        ),
        tuple(sorted(_PROTECTED_PREPARED_STATUSES)),
    ).fetchall()
    candidates: list[CleanupCandidate] = []
    for row in rows:
        sandbox_id = str(row["sandbox_id"] or "")
        device_id = str(row["device_id"] or "")
        if sandbox_id and sandbox_id in existing_sandbox_ids:
            continue
        device = _device(controller_conn, device_id) if device_id else None
        candidates.append(
            CleanupCandidate(
                controller_task_id="",
                case_id=str(row["task_id"] or ""),
                reason="prepared_buffer_stop_cleanup",
                task_status="",
                lease_id=str(row["lease_id"] or ""),
                lease_status="",
                device_id=device_id,
                device_status=str(device["status"] or "") if device is not None else "",
                sandbox_id=sandbox_id,
                metadata={"prepared_status": str(row["status"] or "")},
            )
        )
    return candidates


def _cleanable_prepared_residue_candidates(
    batch_conn: sqlite3.Connection,
    controller_conn: sqlite3.Connection,
    *,
    protected: JsonDict,
    existing_sandbox_ids: set[str],
    existing_device_ids: set[str],
) -> list[CleanupCandidate]:
    if not _table_exists(batch_conn, "prepared_sandboxes"):
        return []
    rows = batch_conn.execute(
        """
        SELECT task_id, attempt, provider, template, sandbox_id, device_id, lease_id, status, error
        FROM prepared_sandboxes
        WHERE status IN ({placeholders})
          AND (sandbox_id<>'' OR device_id<>'' OR lease_id<>'')
        """.format(placeholders=",".join("?" for _ in _CLEANABLE_PREPARED_STATUSES)),
        tuple(sorted(_CLEANABLE_PREPARED_STATUSES)),
    ).fetchall()
    candidates: list[CleanupCandidate] = []
    for row in rows:
        sandbox_id = str(row["sandbox_id"] or "")
        device_id = str(row["device_id"] or "")
        lease_id = str(row["lease_id"] or "")
        if (sandbox_id and sandbox_id in existing_sandbox_ids) or (device_id and device_id in existing_device_ids):
            continue
        protected_reason = _protected_reason(
            protected=protected,
            task_id="",
            session_id="",
            lease_id=lease_id,
            device_id=device_id,
            sandbox_id=sandbox_id,
        )
        if protected_reason:
            continue
        device = _device(controller_conn, device_id) if device_id else None
        candidates.append(
            CleanupCandidate(
                controller_task_id="",
                case_id=str(row["task_id"] or ""),
                reason=f"prepared_{row['status']}_residue",
                task_status="",
                lease_id=lease_id,
                lease_status="",
                device_id=device_id,
                device_status=str(device["status"] or "") if device is not None else "",
                sandbox_id=sandbox_id,
                metadata={
                    "prepared_status": str(row["status"] or ""),
                    "prepared_error": str(row["error"] or ""),
                    "provider": str(row["provider"] or ""),
                    "template": str(row["template"] or ""),
                    "attempt": str(row["attempt"] or ""),
                },
            )
        )
    return candidates


def _orphan_online_device_candidates(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    protected: JsonDict,
    existing_device_ids: set[str],
) -> tuple[list[CleanupCandidate], list[JsonDict]]:
    if not _table_exists(connection, "devices"):
        return [], []
    candidates: list[CleanupCandidate] = []
    blocked: list[JsonDict] = []
    for row in connection.execute(
        "SELECT * FROM devices WHERE status IN ({})".format(",".join("?" for _ in ACTIVE_DEVICE_STATUSES)),
        tuple(sorted(ACTIVE_DEVICE_STATUSES)),
    ):
        device_id = str(row["device_id"] or "")
        if not device_id or device_id in existing_device_ids:
            continue
        if _device_has_active_session(connection, device_id) or _device_has_active_command(connection, device_id):
            continue
        protected_reason = _protected_reason(
            protected=protected,
            task_id="",
            session_id="",
            lease_id="",
            device_id=device_id,
            sandbox_id="",
        )
        if protected_reason:
            blocked.append(_blocked(protected_reason, device_id=device_id))
            continue
        owner_batch_id = _device_owner_batch_id(row)
        if not owner_batch_id:
            blocked.append(_blocked("ambiguous_online_orphan_device", device_id=device_id))
            continue
        if owner_batch_id != batch_id:
            blocked.append(
                _blocked("foreign_batch_online_orphan_device", device_id=device_id, owner_batch_id=owner_batch_id)
            )
            continue
        candidates.append(
            CleanupCandidate(
                controller_task_id="",
                case_id="",
                reason="batch_orphan_online_device_residue",
                task_status="",
                device_id=device_id,
                device_status=str(row["status"] or ""),
                metadata={"owner_batch_id": owner_batch_id},
            )
        )
    return candidates, blocked


def _protected_reason(
    *,
    protected: JsonDict,
    task_id: str,
    session_id: str,
    lease_id: str,
    device_id: str,
    sandbox_id: str,
) -> str:
    checks = [
        ("protected_active_controller_task", "controller_task_ids", task_id),
        ("protected_active_session", "session_ids", session_id),
        ("protected_active_lease", "lease_ids", lease_id),
        ("protected_active_or_prepared_device", "device_ids", device_id),
        ("protected_active_or_prepared_sandbox", "sandbox_ids", sandbox_id),
    ]
    for reason, key, value in checks:
        if value and value in protected[key]:
            return reason
    return ""


def _batch_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(connection, "batch_tasks"):
        raise RuntimeError("batch DB has no batch_tasks table")
    return list(connection.execute("SELECT * FROM batch_tasks ORDER BY order_index ASC"))


def _controller_lookup_batch_rows(batch_rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in batch_rows if _row_needs_controller_lookup(row)]


def _row_needs_controller_lookup(row: sqlite3.Row) -> bool:
    status = str(row["status"] if "status" in row.keys() else "")
    if status in _ACTIVE_BATCH_STATUSES:
        return True
    if status in {"failed", "timed_out", "canceled"}:
        return True
    if status == "pending":
        return bool(_explicit_controller_task_id(row) or _row_has_attempt_history(row))
    if status == "succeeded":
        return _succeeded_row_needs_controller_lookup(row)
    return _row_has_runtime_resource_reference(row)


def _explicit_controller_task_id(row: sqlite3.Row) -> str:
    return str(row["controller_task_id"] or "") if "controller_task_id" in row.keys() else ""


def _row_has_attempt_history(row: sqlite3.Row) -> bool:
    return _int_value(row["attempts"] if "attempts" in row.keys() else 0, default=0) > 0


def _row_has_runtime_resource_reference(row: sqlite3.Row) -> bool:
    for column in ("session_id", "sandbox_id", "device_id", "lease_token"):
        if column in row.keys() and str(row[column] or ""):
            return True
    return False


def _succeeded_row_needs_controller_lookup(row: sqlite3.Row) -> bool:
    if "lease_token" in row.keys() and str(row["lease_token"] or ""):
        return True
    cleanup_status = str(row["cleanup_status"] or "") if "cleanup_status" in row.keys() else ""
    if cleanup_status == "ok":
        return False
    if cleanup_status:
        return True
    return _row_has_runtime_resource_reference(row)


def _controller_lookup_task_ids(*, batch_rows: list[sqlite3.Row], batch_id: str) -> set[str]:
    task_ids: set[str] = set()
    for row in batch_rows:
        if not _row_needs_controller_lookup(row):
            continue
        case_id = str(row["task_id"] or "")
        if not case_id:
            continue
        controller_task_id = _explicit_controller_task_id(row)
        if controller_task_id:
            task_ids.add(controller_task_id)
        for attempt in _deterministic_lookup_attempts(row):
            deterministic_id, _trace_id = BatchTaskPlanner._deterministic_ids(
                batch_id=batch_id,
                task_id=case_id,
                attempt=attempt,
            )
            task_ids.add(deterministic_id)
    return task_ids


def _deterministic_lookup_attempts(row: sqlite3.Row) -> list[int]:
    attempts = _deterministic_lookup_attempts_uncapped(row)
    return sorted(attempt for attempt in attempts if 0 < attempt <= _MAX_DETERMINISTIC_LOOKUP_ATTEMPTS)


def _deterministic_lookup_cap_warnings(batch_rows: list[sqlite3.Row]) -> list[str]:
    capped: list[str] = []
    for row in batch_rows:
        attempts = _deterministic_lookup_attempts_uncapped(row)
        if any(attempt > _MAX_DETERMINISTIC_LOOKUP_ATTEMPTS for attempt in attempts):
            capped.append(str(row["task_id"] or ""))
    if not capped:
        return []
    sample = ",".join(capped[:5])
    return [
        "deterministic lookup cap exceeded; refusing to treat controller cleanup as complete "
        f"without explicit operator handling: count={len(capped)} sample={sample}"
    ]


def _deterministic_lookup_attempts_uncapped(row: sqlite3.Row) -> set[int]:
    status = str(row["status"] if "status" in row.keys() else "")
    current = max(_int_value(row["attempts"] if "attempts" in row.keys() else 1, default=1), 1)
    max_attempts = _int_value(row["max_attempts"] if "max_attempts" in row.keys() else 0, default=0)
    retry_budget = _int_value(
        row["retry_budget_remaining"] if "retry_budget_remaining" in row.keys() else 0,
        default=0,
    )
    attempts = {1} if status == "pending" else set(range(1, current + 1))
    if status in {"failed", "timed_out"} and (retry_budget > 0 or max_attempts <= 0 or current < max_attempts):
        attempts.add(current + 1)
    return attempts


def _controller_task_rows(
    connection: sqlite3.Connection,
    *,
    protected_task_ids: set[str],
    lookup_task_ids: set[str],
) -> tuple[list[sqlite3.Row], JsonDict]:
    if not _table_exists(connection, "tasks"):
        raise RuntimeError("controller DB has no tasks table")
    task_by_id: dict[str, sqlite3.Row] = {}
    ids = {task_id for task_id in {*lookup_task_ids, *protected_task_ids} if task_id}
    for chunk in _chunks(sorted(ids), 500):
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            f"""
            SELECT task_id, trace_id, request_json, status, created_at
            FROM tasks
            WHERE task_id IN ({placeholders})
            """,
            tuple(chunk),
        ):
            task_by_id[str(row["task_id"])] = row
    rows = sorted(task_by_id.values(), key=lambda row: str(row["created_at"] or ""), reverse=True)
    return rows, {
        "controller_task_lookup_id_count": len(ids),
        "controller_task_rows_loaded": len(rows),
        "controller_task_rows_missing": max(len(ids) - len(rows), 0),
    }


def _controller_active_residue_task_ids(connection: sqlite3.Connection) -> tuple[set[str], JsonDict]:
    task_ids: set[str] = set()
    stats: JsonDict = {
        "controller_active_task_ids": 0,
        "controller_active_session_task_ids": 0,
        "controller_active_lease_task_ids": 0,
        "controller_active_command_task_ids": 0,
    }
    if _table_exists(connection, "tasks"):
        rows = connection.execute(
            "SELECT task_id FROM tasks WHERE status IN ({})".format(",".join("?" for _ in _ACTIVE_TASK_STATUSES)),
            tuple(sorted(_ACTIVE_TASK_STATUSES)),
        ).fetchall()
        values = {str(row["task_id"] or "") for row in rows if str(row["task_id"] or "")}
        task_ids.update(values)
        stats["controller_active_task_ids"] = len(values)
    if _table_exists(connection, "sessions"):
        rows = connection.execute(
            "SELECT task_id FROM sessions WHERE status IN ({}) AND task_id<>''".format(
                ",".join("?" for _ in _ACTIVE_SESSION_STATUSES)
            ),
            tuple(sorted(_ACTIVE_SESSION_STATUSES)),
        ).fetchall()
        values = {str(row["task_id"] or "") for row in rows if str(row["task_id"] or "")}
        task_ids.update(values)
        stats["controller_active_session_task_ids"] = len(values)
    if _table_exists(connection, "leases") and _table_exists(connection, "sessions"):
        rows = connection.execute(
            """
            SELECT DISTINCT sessions.task_id
            FROM leases
            JOIN sessions ON sessions.session_id=leases.session_id
            WHERE leases.status IN ({}) AND sessions.task_id<>''
            """.format(",".join("?" for _ in _ACTIVE_SESSION_STATUSES)),
            tuple(sorted(_ACTIVE_SESSION_STATUSES)),
        ).fetchall()
        values = {str(row["task_id"] or "") for row in rows if str(row["task_id"] or "")}
        task_ids.update(values)
        stats["controller_active_lease_task_ids"] = len(values)
    if _table_exists(connection, "commands") and _table_exists(connection, "sessions"):
        rows = connection.execute(
            """
            SELECT DISTINCT sessions.task_id
            FROM commands
            JOIN sessions ON sessions.session_id=commands.session_id
            WHERE commands.status IN ({}) AND sessions.task_id<>''
            """.format(",".join("?" for _ in _ACTIVE_COMMAND_STATUSES)),
            tuple(sorted(_ACTIVE_COMMAND_STATUSES)),
        ).fetchall()
        values = {str(row["task_id"] or "") for row in rows if str(row["task_id"] or "")}
        task_ids.update(values)
        stats["controller_active_command_task_ids"] = len(values)
    stats["controller_active_residue_task_id_count"] = len(task_ids)
    return task_ids, stats


def _session_for_task(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    if not _table_exists(connection, "sessions"):
        return None
    return connection.execute(
        "SELECT * FROM sessions WHERE task_id=? ORDER BY updated_at DESC LIMIT 1", (task_id,)
    ).fetchone()


def _lease_for_session(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    if not session_id or not _table_exists(connection, "leases"):
        return None
    return connection.execute(
        "SELECT * FROM leases WHERE session_id=? ORDER BY generation DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _device(connection: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    if not device_id or not _table_exists(connection, "devices"):
        return None
    return connection.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()


def _device_has_active_session(connection: sqlite3.Connection, device_id: str) -> bool:
    if not device_id or not _table_exists(connection, "sessions"):
        return False
    return (
        connection.execute(
            f"""
            SELECT 1 FROM sessions
            WHERE device_id=? AND status IN ({",".join("?" for _ in _ACTIVE_SESSION_STATUSES)})
            LIMIT 1
            """,
            (device_id, *sorted(_ACTIVE_SESSION_STATUSES)),
        ).fetchone()
        is not None
    )


def _device_has_active_command(connection: sqlite3.Connection, device_id: str) -> bool:
    if not device_id or not _table_exists(connection, "commands"):
        return False
    return (
        connection.execute(
            f"""
            SELECT 1 FROM commands
            WHERE device_id=? AND status IN ({",".join("?" for _ in _ACTIVE_COMMAND_STATUSES)})
            LIMIT 1
            """,
            (device_id, *sorted(_ACTIVE_COMMAND_STATUSES)),
        ).fetchone()
        is not None
    )


def _session_has_active_command(connection: sqlite3.Connection, session_id: str) -> bool:
    if not session_id or not _table_exists(connection, "commands"):
        return False
    return (
        connection.execute(
            f"""
            SELECT 1 FROM commands
            WHERE session_id=? AND status IN ({",".join("?" for _ in _ACTIVE_COMMAND_STATUSES)})
            LIMIT 1
            """,
            (session_id, *sorted(_ACTIVE_COMMAND_STATUSES)),
        ).fetchone()
        is not None
    )


def _unassigned_prepared_sql() -> str:
    return "assigned_at IS NULL OR CAST(assigned_at AS TEXT)='' OR CAST(assigned_at AS REAL)<=0"


def _batch_status(rows: list[sqlite3.Row], task_id: str) -> str:
    for row in rows:
        if str(row["task_id"]) == task_id:
            return str(row["status"])
    return ""


def _task_metadata(row: sqlite3.Row) -> JsonDict:
    request = _loads(str(row["request_json"] or "{}"))
    metadata = request.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    payload = request.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
        return dict(payload["metadata"])
    return {}


def _device_owner_batch_id(row: sqlite3.Row) -> str:
    descriptor = _loads(str(row["descriptor_json"] or "{}"))
    metadata = descriptor.get("metadata")
    if isinstance(metadata, dict):
        batch_id = str(metadata.get("batch_id") or "")
        if batch_id:
            return batch_id
    display_name = str(descriptor.get("display_name") or "")
    if ":" in display_name:
        prefix = display_name.split(":", 1)[0].strip()
        if prefix:
            return prefix
    device_id = str(row["device_id"] or "")
    if device_id.startswith("batch-") and "-sc-" in device_id:
        return device_id[len("batch-") : device_id.index("-sc-")]
    return ""


def _annotate_provider_matches(plan: CleanupPlan, inventory_by_sandbox: dict[str, SandboxInventoryItem]) -> None:
    for candidate in plan.candidates:
        if candidate.sandbox_id in inventory_by_sandbox:
            candidate.provider_status = "planned"


def _count_candidate_online_devices(connection: sqlite3.Connection, candidates: list[CleanupCandidate]) -> int:
    if not _table_exists(connection, "devices"):
        return 0
    device_ids = sorted({candidate.device_id for candidate in candidates if candidate.device_id})
    if not device_ids:
        return 0
    count = 0
    for chunk in _chunks(device_ids, 500):
        placeholders = ",".join("?" for _ in chunk)
        row = connection.execute(
            f"""
            SELECT COUNT(*) FROM devices
            WHERE device_id IN ({placeholders}) AND status='online'
            """,
            tuple(chunk),
        ).fetchone()
        count += int(row[0] if row is not None else 0)
    return count


def _sandbox_maintenance_client() -> "SandboxMaintenanceClient":
    from northcua.batch.maintenance.sandbox_ops import SandboxMaintenanceClient

    return SandboxMaintenanceClient()


def main() -> None:
    """Run the controller-residue cleanup CLI entrypoint."""

    from northcua.maintenance.cleanup_controller_cli import main as cli_main

    cli_main()


if __name__ == "__main__":  # pragma: no cover
    main()
