"""One-shot health report for NorthCUA batch monitoring."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from northcua.maintenance.batch_health_markdown import render_markdown
from northcua.maintenance.batch_health_runtime_probes import _artifact_metrics, _cloudflared_metrics, _socket_metrics
from northcua.maintenance.batch_health_time import epoch as _epoch
from northcua.maintenance.batch_health_time import parse_time_to_epoch as _parse_time_to_epoch
from northcua.maintenance.batch_health_values import report_value as _report_value
from northcua.maintenance.batch_health_values import shorten as _shorten
from northcua.maintenance.batch_health_values import to_float as _float
from northcua.maintenance.batch_health_values import to_int as _int
from northcua.maintenance.batch_health_worker_logs import worker_log_metrics as _worker_log_metrics
from northcua.maintenance.controller_event_metrics import (
    process_executor_metrics,
    readiness_stage_metrics,
    runtime_event_metrics,
    websocket_handshake_metrics,
)

JsonDict = dict[str, object]

_TERMINAL_BATCH_STATUSES = {"succeeded", "evaluation_pending", "failed", "timed_out", "canceled"}
_ACTIVE_BATCH_STATUSES = {"starting_sandbox", "bootstrapping_device", "waiting_device", "submitted", "running"}
_DISABLED_WINDOW_TOOLS = {"get_app_window_info", "get_app_window_controls_target_info"}
_PREPARED_ACTIVE_UNASSIGNED_STATUSES = {"reserved", "preparing", "prepared"}
_PREPARED_ACTIVE_STATUSES = {*_PREPARED_ACTIVE_UNASSIGNED_STATUSES, "assigned"}
_COMMAND_PROBLEM_STATUSES = {"failed", "timed_out"}
_COMMAND_DURATION_BUCKETS = (
    ("<1s", 1.0),
    ("1-2s", 2.0),
    ("2-5s", 5.0),
    ("5-10s", 10.0),
    ("10-30s", 30.0),
    ("30-90s", 90.0),
    ("90-300s", 300.0),
)


def generate_batch_health_report(
    *,
    batch_db: Path,
    controller_db: Path | None = None,
    run_dir: Path | None = None,
    restart_time: str = "",
    cloudflared_metrics_url: str = "",
    socket_port: int = 9192,
    recent_window_minutes: int = 20,
) -> JsonDict:
    """Return a JSON-safe operator health report for one batch.

    Args:
        batch_db: Batch SQLite database path.
        controller_db: Optional controller SQLite database path.
        run_dir: Batch run directory containing JSONL and artifacts.
        restart_time: Optional restart time used to bound recent controller rows.
        cloudflared_metrics_url: Optional cloudflared Prometheus metrics URL.
        socket_port: Controller socket port for local queue probes.
        recent_window_minutes: Window used for recent distributions.

    Returns:
        JSON-safe report plus rendered Markdown.
    """

    batch_db = batch_db.expanduser().resolve()
    controller_db = controller_db.expanduser().resolve() if controller_db is not None else None
    run_dir = run_dir.expanduser().resolve() if run_dir is not None else batch_db.parent
    restart_ts = _parse_time_to_epoch(restart_time)
    warnings: list[str] = []
    with _connect(batch_db) as batch_conn:
        batch = _batch_metrics(batch_conn, restart_ts=restart_ts, recent_window_minutes=recent_window_minutes)
        prepared = _prepared_metrics(batch_conn, batch=batch, recent_window_minutes=recent_window_minutes)
    controller: JsonDict = {}
    if controller_db is not None and controller_db.exists():
        with _connect(controller_db) as controller_conn:
            controller = _controller_metrics(
                controller_conn,
                batch=batch,
                restart_ts=restart_ts,
                recent_window_minutes=recent_window_minutes,
            )
            _augment_batch_with_evaluator_acceptance(batch, controller_conn)
    elif controller_db is not None:
        warnings.append(f"controller DB not found: {controller_db}")
    attempts = _attempt_event_metrics(run_dir)
    artifacts = _artifact_metrics(run_dir, restart_ts=restart_ts)
    worker_logs = _worker_log_metrics(
        batch_db=batch_db,
        run_dir=run_dir,
        restart_ts=restart_ts,
        recent_window_minutes=recent_window_minutes,
        warnings=warnings,
    )
    cloudflared = _cloudflared_metrics(cloudflared_metrics_url, warnings=warnings) if cloudflared_metrics_url else {}
    sockets = _socket_metrics(socket_port, warnings=warnings)
    report: JsonDict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch_db": str(batch_db),
        "controller_db": str(controller_db or ""),
        "run_dir": str(run_dir),
        "restart_time": restart_time,
        "recent_window_minutes": recent_window_minutes,
        "batch": batch,
        "prepared_sandboxes": prepared,
        "controller": controller,
        "attempt_events": attempts,
        "artifacts": artifacts,
        "worker_logs": worker_logs,
        "socket_queues": sockets,
        "cloudflared": cloudflared,
        "warnings": warnings,
    }
    report["markdown"] = render_markdown(report)
    return report


def _batch_metrics(
    connection: sqlite3.Connection,
    *,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    rows = list(connection.execute("SELECT * FROM batch_tasks"))
    status_counts = Counter(str(row["status"]) for row in rows)
    accepted_infeasible_cases = {
        str(row["task_id"]) for row in rows if str(row["last_failure_category"] or "") == "accepted_infeasible"
    }
    terminal_total = sum(status_counts.get(status, 0) for status in _TERMINAL_BATCH_STATUSES)
    active_total = sum(status_counts.get(status, 0) for status in _ACTIVE_BATCH_STATUSES)
    current_controller_by_case = {
        str(row["task_id"]): str(row["controller_task_id"] or "")
        for row in rows
        if str(row["controller_task_id"] or "")
    }
    status_by_case = {str(row["task_id"]): str(row["status"]) for row in rows}
    active_controller_task_ids = {
        str(row["controller_task_id"])
        for row in rows
        if str(row["status"]) in _ACTIVE_BATCH_STATUSES and str(row["controller_task_id"] or "")
    }
    now = time.time()
    recent_cutoff = now - recent_window_minutes * 60
    since_restart = [row for row in rows if restart_ts > 0 and _float(row["updated_at"]) >= restart_ts]
    recent = [row for row in rows if _float(row["updated_at"]) >= recent_cutoff]
    root_causes = Counter(_root_cause_from_row(row) for row in rows if str(row["status"]) != "succeeded")
    throughput_since_restart = _throughput(since_restart, restart_ts=restart_ts)
    recent_terminal_delta = sum(1 for row in recent if str(row["status"]) in _TERMINAL_BATCH_STATUSES)
    recent_success_delta = sum(1 for row in recent if str(row["status"]) == "succeeded")
    return {
        "total": len(rows),
        "status_counts": dict(status_counts),
        "active_total": active_total,
        "terminal_total": terminal_total,
        "success_total": status_counts.get("succeeded", 0),
        "task_success_total": max(status_counts.get("succeeded", 0) - len(accepted_infeasible_cases), 0),
        "accepted_infeasible_total": len(accepted_infeasible_cases),
        "accepted_infeasible_cases": sorted(accepted_infeasible_cases),
        "actionable_failed_total": status_counts.get("failed", 0),
        "active_submitted_running_waiting_device": sum(
            status_counts.get(status, 0) for status in ("submitted", "running", "waiting_device")
        ),
        "current_controller_by_case": current_controller_by_case,
        "status_by_case": status_by_case,
        "active_controller_task_ids": sorted(active_controller_task_ids),
        "throughput_since_restart": throughput_since_restart,
        "recent_window": {
            "terminal_delta": recent_terminal_delta,
            "success_delta": recent_success_delta,
            "accepted_infeasible_delta": sum(
                1 for row in recent if str(row["last_failure_category"] or "") == "accepted_infeasible"
            ),
        },
        "eta": _eta_metrics(
            total=len(rows),
            terminal_total=terminal_total,
            recent_terminal_delta=recent_terminal_delta,
            recent_window_minutes=recent_window_minutes,
            throughput_since_restart=throughput_since_restart,
            restart_ts=restart_ts,
            now=now,
        ),
        "failure_root_causes": dict(root_causes.most_common(20)),
    }


def _augment_batch_with_evaluator_acceptance(batch: JsonDict, controller_conn: sqlite3.Connection) -> None:
    current_controller_by_case = batch.get("current_controller_by_case")
    if not isinstance(current_controller_by_case, dict):
        return
    existing_cases = set(batch.get("accepted_infeasible_cases") or [])
    raw_status_by_case = batch.get("status_by_case")
    status_by_case: dict[str, object] = raw_status_by_case if isinstance(raw_status_by_case, dict) else {}
    if not current_controller_by_case:
        return
    placeholders = ",".join("?" for _ in current_controller_by_case)
    query = f"SELECT task_id, result_json FROM tasks WHERE task_id IN ({placeholders})"
    controller_rows = {
        str(row["task_id"]): row["result_json"]
        for row in controller_conn.execute(query, tuple(current_controller_by_case.values())).fetchall()
    }
    accepted_cases = set(existing_cases)
    raw_failed_accepted_cases = set()
    for case_id, controller_task_id in current_controller_by_case.items():
        if not _controller_result_json_accepts_uncontrollable(controller_rows.get(str(controller_task_id))):
            continue
        accepted_cases.add(str(case_id))
        if str(status_by_case.get(str(case_id)) or "") == "failed":
            raw_failed_accepted_cases.add(str(case_id))
    failed_count = int((batch.get("status_counts") or {}).get("failed", 0) or 0)
    succeeded_count = int((batch.get("status_counts") or {}).get("succeeded", 0) or 0)
    accepted_succeeded_count = sum(
        1 for case_id in accepted_cases if str(status_by_case.get(case_id) or "") == "succeeded"
    )
    batch["accepted_infeasible_cases"] = sorted(accepted_cases)
    batch["accepted_infeasible_total"] = len(accepted_cases)
    batch["accepted_infeasible_raw_failed_total"] = len(raw_failed_accepted_cases)
    batch["actionable_failed_total"] = max(failed_count - len(raw_failed_accepted_cases), 0)
    batch["task_success_total"] = max(succeeded_count - accepted_succeeded_count, 0)


def _prepared_metrics(
    connection: sqlite3.Connection,
    *,
    batch: JsonDict,
    recent_window_minutes: int,
) -> JsonDict:
    if not _table_exists(connection, "prepared_sandboxes"):
        return {
            "status_counts": {},
            "active_unassigned_total": 0,
            "active_unassigned_counts": {},
            "assigned_to_current_active_total": 0,
            "stale_assigned_total": 0,
            "expired_but_active_total": 0,
            "prepare_seconds_distribution": {},
            "age_seconds_by_status": {},
            "recent_window_status_updates": {},
            "error_samples": [],
        }
    rows = list(connection.execute("SELECT * FROM prepared_sandboxes"))
    now = time.time()
    recent_cutoff = now - recent_window_minutes * 60
    status_counts = Counter(str(row["status"]) for row in rows)
    status_by_case = {str(case_id): str(status) for case_id, status in dict(batch.get("status_by_case", {})).items()}
    active_case_ids = {case_id for case_id, status in status_by_case.items() if status in _ACTIVE_BATCH_STATUSES}
    active_unassigned = [
        row
        for row in rows
        if str(row["status"]) in _PREPARED_ACTIVE_UNASSIGNED_STATUSES
        and _epoch(_row_value(row, "assigned_at")) <= 0
        and _is_unexpired(row, now=now)
    ]
    assigned = [row for row in rows if str(row["status"]) == "assigned"]
    assigned_to_current_active = [row for row in assigned if str(_row_value(row, "task_id", "")) in active_case_ids]
    stale_assigned = [row for row in assigned if str(_row_value(row, "task_id", "")) not in active_case_ids]
    expired_but_active = [
        row
        for row in rows
        if str(row["status"]) in _PREPARED_ACTIVE_UNASSIGNED_STATUSES
        and _epoch(_row_value(row, "expires_at")) > 0
        and _epoch(_row_value(row, "expires_at")) <= now
    ]
    ages_by_status: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        status = str(row["status"])
        if status not in _PREPARED_ACTIVE_STATUSES:
            continue
        started_at = _prepared_age_anchor(row, status)
        if started_at > 0 and now >= started_at:
            ages_by_status[status].append(now - started_at)
    recent_rows = [row for row in rows if _epoch(_row_value(row, "updated_at")) >= recent_cutoff]
    error_rows = sorted(
        [row for row in rows if str(_row_value(row, "error", "") or "")],
        key=lambda row: _epoch(_row_value(row, "updated_at")),
        reverse=True,
    )
    return {
        "status_counts": dict(status_counts),
        "active_unassigned_total": len(active_unassigned),
        "active_unassigned_counts": dict(Counter(str(row["status"]) for row in active_unassigned)),
        "assigned_to_current_active_total": len(assigned_to_current_active),
        "stale_assigned_total": len(stale_assigned),
        "expired_but_active_total": len(expired_but_active),
        "prepare_seconds_distribution": _duration_distribution(
            [
                _float(_row_value(row, "prepare_seconds"))
                for row in rows
                if _float(_row_value(row, "prepare_seconds")) > 0
            ]
        ),
        "age_seconds_by_status": {
            status: _duration_distribution(values) for status, values in sorted(ages_by_status.items()) if values
        },
        "recent_window_status_updates": dict(Counter(str(row["status"]) for row in recent_rows)),
        "error_samples": [
            {
                "task_id": str(_row_value(row, "task_id", "")),
                "attempt": _int(_row_value(row, "attempt")),
                "status": str(_row_value(row, "status", "")),
                "updated_at": _row_value(row, "updated_at", ""),
                "error": _shorten(str(_row_value(row, "error", "")), 240),
            }
            for row in error_rows[:8]
        ],
    }


def _controller_metrics(
    connection: sqlite3.Connection,
    *,
    batch: JsonDict,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    task_rows = _rows(connection, "tasks")
    session_rows = _rows(connection, "sessions")
    lease_rows = _rows(connection, "leases")
    device_rows = _rows(connection, "devices")
    command_rows = _rows(connection, "commands")
    worker_job_rows = _rows(connection, "worker_jobs")
    latest_runtime = _latest_event_payload(connection, "controller_runtime_telemetry")
    command_latencies = _command_latency(command_rows, restart_ts=restart_ts)
    command_metrics = _command_metrics(
        command_rows,
        restart_ts=restart_ts,
        recent_window_minutes=recent_window_minutes,
    )
    runtime_metrics = runtime_event_metrics(
        connection,
        restart_ts=restart_ts,
        recent_window_minutes=recent_window_minutes,
    )
    handshake_metrics = websocket_handshake_metrics(
        connection,
        restart_ts=restart_ts,
        recent_window_minutes=recent_window_minutes,
    )
    process_metrics = process_executor_metrics(
        connection,
        restart_ts=restart_ts,
        recent_window_minutes=recent_window_minutes,
    )
    readiness_metrics = readiness_stage_metrics(
        connection,
        restart_ts=restart_ts,
        recent_window_minutes=recent_window_minutes,
    )
    return {
        "task_status_counts": dict(Counter(str(row["status"]) for row in task_rows)),
        "session_status_counts": dict(Counter(str(row["status"]) for row in session_rows)),
        "lease_status_counts": dict(Counter(str(row["status"]) for row in lease_rows)),
        "device_status_counts": dict(Counter(str(row["status"]) for row in device_rows)),
        "command_status_counts": dict(Counter(str(row["status"]) for row in command_rows)),
        "live_membership": _live_membership(task_rows, session_rows, batch=batch),
        "runtime_telemetry_latest": latest_runtime,
        "runtime_event_metrics": runtime_metrics,
        "websocket_handshake_metrics": handshake_metrics,
        "readiness_stage_metrics": readiness_metrics,
        "process_executor_metrics": process_metrics,
        "worker_job_health": _worker_job_health(worker_job_rows),
        "command_latency_seconds": command_latencies,
        "command_metrics": command_metrics,
        "command_tool_counts_since_restart": _command_tool_counts(command_rows, restart_ts=restart_ts),
        "disabled_window_tool_count_since_restart": sum(
            1
            for row in command_rows
            if _command_is_since(row, restart_ts) and _command_tool(row) in _DISABLED_WINDOW_TOOLS
        ),
        "telemetry_event_health_since_restart": _telemetry_event_health(connection, restart_ts=restart_ts),
    }


def _worker_job_health(rows: list[sqlite3.Row]) -> JsonDict:
    if not rows:
        return {"total": 0, "status_counts": {}}
    now = time.time()
    heartbeat_ages: list[float] = []
    rss_values: list[float] = []
    peak_rss_values: list[float] = []
    current_rss_values: list[float] = []
    cpu_values: list[float] = []
    stale_jobs: list[JsonDict] = []
    active_statuses = {"queued", "starting", "running", "finishing"}
    for row in rows:
        heartbeat_at = _parse_time_to_epoch(str(row["heartbeat_at"] or ""))
        if heartbeat_at > 0:
            age = max(0.0, now - heartbeat_at)
            heartbeat_ages.append(age * 1000)
            if str(row["status"]) in active_statuses and age > 60:
                stale_jobs.append(
                    {
                        "job_id": str(row["job_id"]),
                        "task_id": str(row["task_id"]),
                        "status": str(row["status"]),
                        "heartbeat_age_seconds": round(age, 3),
                    }
                )
        payload = _loads(str(row["heartbeat_payload_json"] or "{}"))
        rss = _float(payload.get("rss_bytes"))
        peak_rss = _float(payload.get("peak_rss_bytes"))
        current_rss = _float(payload.get("current_rss_bytes"))
        cpu = _float(payload.get("cpu_time_seconds"))
        if rss >= 0:
            rss_values.append(rss)
        if peak_rss >= 0:
            peak_rss_values.append(peak_rss)
        if current_rss >= 0:
            current_rss_values.append(current_rss)
        if cpu >= 0:
            cpu_values.append(cpu)
    return {
        "total": len(rows),
        "status_counts": dict(Counter(str(row["status"]) for row in rows)),
        "failure_kind_counts": dict(Counter(str(row["failure_kind"]) for row in rows if str(row["failure_kind"]))),
        "heartbeat_age_ms": _value_distribution(heartbeat_ages),
        "rss_bytes": _value_distribution(rss_values),
        "peak_rss_bytes": _value_distribution(peak_rss_values),
        "current_rss_bytes": _value_distribution(current_rss_values),
        "cpu_time_seconds": _value_distribution(cpu_values),
        "top_stale_jobs": stale_jobs[:8],
    }


def _attempt_event_metrics(run_dir: Path) -> JsonDict:
    path = run_dir / "batch_attempt_events.jsonl"
    rows = _jsonl_rows(path)
    windows = {}
    now = time.time()
    for minutes in (5, 10, 20, 40, 60):
        cutoff = now - minutes * 60
        windows[f"{minutes}m"] = sum(
            1
            for row in rows
            if _parse_time_to_epoch(str(row.get("timestamp", ""))) >= cutoff
            and "did not become ready" in json.dumps(row, ensure_ascii=False).lower()
        )
    return {
        "total": len(rows),
        "message_counts": dict(Counter(str(row.get("message", "")) for row in rows).most_common(20)),
        "device_ready_failure_windows": windows,
    }


def _live_membership(task_rows: list[sqlite3.Row], session_rows: list[sqlite3.Row], *, batch: JsonDict) -> JsonDict:
    active_controller_ids = {str(value) for value in batch.get("active_controller_task_ids", [])}
    current_controller_by_case = {
        str(case_id): str(controller_id)
        for case_id, controller_id in dict(batch.get("current_controller_by_case", {})).items()
    }
    status_by_case = {str(case_id): str(status) for case_id, status in dict(batch.get("status_by_case", {})).items()}
    session_by_task = {str(row["task_id"]): row for row in session_rows}
    counts: Counter[str] = Counter()
    for row in task_rows:
        metadata = _task_metadata(row)
        if not metadata:
            continue
        case_id = str(metadata.get("batch_task_id") or "")
        if not case_id:
            continue
        controller_task_id = str(row["task_id"])
        if case_id not in status_by_case:
            counts["batch_owned_unknown_case"] += 1
            continue
        session = session_by_task.get(str(row["task_id"]))
        session_status = str(session["status"]) if session is not None else ""
        task_is_live = str(row["status"]) in {"running", "scheduled", "accepted"} and session_status in {
            "running",
            "leased",
        }
        expected_current = current_controller_by_case.get(case_id, "")
        if controller_task_id == expected_current:
            counts[
                "current_active" if task_is_live and controller_task_id in active_controller_ids else "current_nonlive"
            ] += 1
        elif expected_current:
            counts[
                (
                    "orphan_controller_task_id_mismatch_live"
                    if task_is_live
                    else "orphan_controller_task_id_mismatch_nonlive"
                )
            ] += 1
        else:
            counts["batch_owned_without_current_live" if task_is_live else "batch_owned_without_current_nonlive"] += 1
    return dict(counts)


def _throughput(rows: list[sqlite3.Row], *, restart_ts: float) -> JsonDict:
    terminal_delta = sum(1 for row in rows if str(row["status"]) in _TERMINAL_BATCH_STATUSES)
    success_delta = sum(1 for row in rows if str(row["status"]) == "succeeded")
    elapsed_hours = max((time.time() - restart_ts) / 3600.0, 0.0) if restart_ts > 0 else 0.0
    return {
        "terminal_delta": terminal_delta,
        "success_delta": success_delta,
        "elapsed_hours": round(elapsed_hours, 3),
        "terminal_per_hour": round(terminal_delta / elapsed_hours, 3) if elapsed_hours > 0 else 0,
        "success_per_hour": round(success_delta / elapsed_hours, 3) if elapsed_hours > 0 else 0,
    }


def _eta_metrics(
    *,
    total: int,
    terminal_total: int,
    recent_terminal_delta: int,
    recent_window_minutes: int,
    throughput_since_restart: JsonDict,
    restart_ts: float,
    now: float,
) -> JsonDict:
    remaining = max(total - terminal_total, 0)
    elapsed_since_restart_hours = max((now - restart_ts) / 3600.0, 0.0) if restart_ts > 0 else 0.0
    recent_elapsed_hours = max(float(recent_window_minutes) / 60.0, 0.0)
    if elapsed_since_restart_hours > 0:
        recent_elapsed_hours = min(recent_elapsed_hours, elapsed_since_restart_hours)
    recent_rate = recent_terminal_delta / recent_elapsed_hours if recent_elapsed_hours > 0 else 0.0
    since_restart_rate = _float(throughput_since_restart.get("terminal_per_hour"))
    if remaining <= 0:
        basis = "complete"
        chosen_rate = 0.0
    elif recent_rate > 0:
        basis = "recent_window"
        chosen_rate = recent_rate
    elif since_restart_rate > 0:
        basis = "since_restart"
        chosen_rate = since_restart_rate
    else:
        basis = "unavailable"
        chosen_rate = 0.0
    eta_seconds = _eta_seconds(remaining, chosen_rate)
    recent_eta_seconds = _eta_seconds(remaining, recent_rate)
    since_restart_eta_seconds = _eta_seconds(remaining, since_restart_rate)
    return {
        "remaining_total": remaining,
        "basis": basis,
        "recent_window_minutes": recent_window_minutes,
        "recent_terminal_delta": recent_terminal_delta,
        "recent_terminal_per_hour": round(recent_rate, 3) if recent_rate > 0 else 0,
        "since_restart_terminal_per_hour": round(since_restart_rate, 3) if since_restart_rate > 0 else 0,
        "eta_seconds": eta_seconds,
        "eta_hours": round(eta_seconds / 3600.0, 3) if eta_seconds is not None else None,
        "eta_at": _eta_at(now, eta_seconds),
        "recent_eta_seconds": recent_eta_seconds,
        "recent_eta_hours": round(recent_eta_seconds / 3600.0, 3) if recent_eta_seconds is not None else None,
        "recent_eta_at": _eta_at(now, recent_eta_seconds),
        "since_restart_eta_seconds": since_restart_eta_seconds,
        "since_restart_eta_hours": (
            round(since_restart_eta_seconds / 3600.0, 3) if since_restart_eta_seconds is not None else None
        ),
        "since_restart_eta_at": _eta_at(now, since_restart_eta_seconds),
    }


def _eta_seconds(remaining: int, terminal_per_hour: float) -> float | None:
    if remaining <= 0:
        return 0.0
    if terminal_per_hour <= 0:
        return None
    return round((remaining / terminal_per_hour) * 3600.0, 3)


def _eta_at(now: float, eta_seconds: float | None) -> str:
    if eta_seconds is None:
        return ""
    return datetime.fromtimestamp(now + eta_seconds, timezone.utc).isoformat()


def _command_latency(rows: list[sqlite3.Row], *, restart_ts: float) -> JsonDict:
    latencies_by_status: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if not _command_is_since(row, restart_ts):
            continue
        duration = _command_duration_seconds(row)
        if duration >= 0:
            latencies_by_status[str(row["status"])].append(duration)
    return {status: _percentiles(values) for status, values in latencies_by_status.items()}


def _command_metrics(
    rows: list[sqlite3.Row],
    *,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    now = time.time()
    recent_cutoff = now - recent_window_minutes * 60
    since_restart = [row for row in rows if _command_is_since(row, restart_ts)]
    recent = [row for row in rows if _epoch(_row_value(row, "updated_at")) >= recent_cutoff]
    by_tool_status: dict[str, list[float]] = defaultdict(list)
    recent_by_tool_status: dict[str, list[float]] = defaultdict(list)
    counts_by_tool_status: Counter[str] = Counter()
    recent_counts_by_tool_status: Counter[str] = Counter()
    slow_commands: list[tuple[float, sqlite3.Row]] = []
    problem_commands: list[sqlite3.Row] = []
    non_evaluator_shell_exec: list[sqlite3.Row] = []
    for row in since_restart:
        key = _command_tool_status_key(row)
        counts_by_tool_status[key] += 1
        duration = _command_duration_seconds(row)
        if duration >= 0:
            by_tool_status[key].append(duration)
            slow_commands.append((duration, row))
        if _command_is_problem(row):
            problem_commands.append(row)
        if _command_tool(row) == "shell.exec" and not _command_is_evaluator_shell_exec(row):
            non_evaluator_shell_exec.append(row)
    for row in recent:
        key = _command_tool_status_key(row)
        recent_counts_by_tool_status[key] += 1
        duration = _command_duration_seconds(row)
        if duration >= 0:
            recent_by_tool_status[key].append(duration)
    slow_commands.sort(key=lambda item: item[0], reverse=True)
    problem_commands.sort(key=lambda row: _epoch(_row_value(row, "updated_at")), reverse=True)
    non_evaluator_shell_exec.sort(key=lambda row: _epoch(_row_value(row, "updated_at")), reverse=True)
    return {
        "by_tool_status": {
            key: _duration_distribution(values) for key, values in sorted(by_tool_status.items()) if values
        },
        "recent_window_by_tool_status": {
            key: _duration_distribution(values) for key, values in sorted(recent_by_tool_status.items()) if values
        },
        "counts_by_tool_status": dict(counts_by_tool_status.most_common(80)),
        "recent_counts_by_tool_status": dict(recent_counts_by_tool_status.most_common(80)),
        "top_slow_commands": [_command_sample(row, duration=duration) for duration, row in slow_commands[:8]],
        "problem_commands": [
            _command_sample(row, duration=_command_duration_seconds(row)) for row in problem_commands[:20]
        ],
        "non_evaluator_shell_exec_total": len(non_evaluator_shell_exec),
        "non_evaluator_shell_exec_samples": [
            _command_sample(row, duration=_command_duration_seconds(row)) for row in non_evaluator_shell_exec[:8]
        ],
    }


def _command_tool_counts(rows: list[sqlite3.Row], *, restart_ts: float) -> JsonDict:
    counts = Counter(_command_tool(row) for row in rows if _command_is_since(row, restart_ts))
    counts.pop("", None)
    return dict(counts.most_common(40))


def _duration_distribution(values: list[float]) -> JsonDict:
    if not values:
        return {}
    return {
        **_percentiles(values),
        "buckets": _duration_buckets(values),
    }


def _value_distribution(values: list[float]) -> JsonDict:
    values = [float(value) for value in values]
    if not values:
        return {}
    return _percentiles(values)


def _duration_buckets(values: list[float]) -> JsonDict:
    counts: Counter[str] = Counter()
    for raw_value in values:
        value = max(float(raw_value), 0.0)
        for label, upper_bound in _COMMAND_DURATION_BUCKETS:
            if value < upper_bound:
                counts[label] += 1
                break
        else:
            counts[">300s"] += 1
    for label, _upper_bound in _COMMAND_DURATION_BUCKETS:
        counts.setdefault(label, 0)
    counts.setdefault(">300s", 0)
    return dict(counts)


def _percentiles(values: list[float]) -> JsonDict:
    if not values:
        return {}
    values = sorted(values)
    return {
        "count": len(values),
        "p50": round(_pct(values, 0.50), 3),
        "p90": round(_pct(values, 0.90), 3),
        "p95": round(_pct(values, 0.95), 3),
        "max": round(values[-1], 3),
    }


def _pct(values: list[float], fraction: float) -> float:
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * fraction))))
    return values[index]


def _root_cause_from_row(row: sqlite3.Row) -> str:
    error = str(row["error"] or "")
    if not error:
        payload = _loads(str(row["state_json"] or "{}"))
        error = str(payload.get("error", ""))
    lowered = error.lower()
    if _is_case_level_timeout_text(lowered):
        return "case_level_timeout"
    if "did not become ready" in lowered or "device-ready" in lowered or "device ready" in lowered:
        return "device_ready_timeout"
    if "window readiness" in lowered:
        return "window_readiness"
    if "llm" in lowered or "model" in lowered:
        return "llm"
    if "command" in lowered:
        return "command"
    return "unknown" if not error else error[:120]


def _is_case_level_timeout_text(text: str) -> bool:
    normalized = " ".join(text.lower().replace("-", " ").replace("_", " ").split())
    if not normalized:
        return False
    if "timeout" not in normalized and "timed out" not in normalized:
        return False
    if "case level" in normalized:
        return True
    if "case timeout" in normalized or "case timed out" in normalized:
        return True
    return "controller timeout" in normalized and any(token in normalized for token in ("case", "task"))


def _latest_event_payload(connection: sqlite3.Connection, event_type: str) -> JsonDict:
    if not _table_exists(connection, "events"):
        return {}
    row = connection.execute(
        "SELECT payload_json FROM events WHERE event_type=? ORDER BY event_id DESC LIMIT 1",
        (event_type,),
    ).fetchone()
    return {} if row is None else _loads(str(row["payload_json"] or "{}"))


def _query_rows_since_restart(
    connection: sqlite3.Connection,
    query: str,
    *,
    restart_ts: float,
) -> list[sqlite3.Row]:
    rows = list(connection.execute(query).fetchall())
    if restart_ts <= 0:
        return rows
    return [row for row in rows if _parse_time_to_epoch(str(row["created_at"] or "")) >= restart_ts]


def _telemetry_event_health(connection: sqlite3.Connection, *, restart_ts: float) -> JsonDict:
    if not _table_exists(connection, "telemetry_events"):
        return {}
    rows = _query_rows_since_restart(
        connection,
        """
            SELECT event_type, phase, status, payload_json, created_at
            FROM telemetry_events
            """,
        restart_ts=restart_ts,
    )
    event_type_counts = Counter(str(row["event_type"]) for row in rows)
    status_counts = Counter(str(row["status"]) for row in rows if str(row["status"]))
    health = {
        "total": len(rows),
        "llm_error": 0,
        "llm_fallback": 0,
        "device_ready_timeout": 0,
        "window_readiness": 0,
        "case_level_timeout": 0,
    }
    for row in rows:
        blob = " ".join(
            (
                str(row["event_type"] or ""),
                str(row["phase"] or ""),
                str(row["status"] or ""),
                str(row["payload_json"] or ""),
            )
        ).lower()
        if ("llm" in blob or "model" in blob) and any(token in blob for token in ("error", "timeout", "failed")):
            health["llm_error"] += 1
        if "fallback" in blob and ("llm" in blob or "model" in blob):
            health["llm_fallback"] += 1
        if "did not become ready" in blob or "device-ready" in blob or "device ready" in blob:
            health["device_ready_timeout"] += 1
        if "window readiness" in blob:
            health["window_readiness"] += 1
        if _is_case_level_timeout_text(blob):
            health["case_level_timeout"] += 1
    return {
        **health,
        "event_type_counts": dict(event_type_counts.most_common(40)),
        "status_counts": dict(status_counts.most_common(20)),
    }


def _rows(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not _table_exists(connection, table):
        return []
    return list(connection.execute(f"SELECT * FROM {table}"))


def _command_is_since(row: sqlite3.Row, restart_ts: float) -> bool:
    return restart_ts <= 0 or _epoch(_row_value(row, "created_at")) >= restart_ts


def _command_duration_seconds(row: sqlite3.Row) -> float:
    created_at = _epoch(_row_value(row, "created_at"))
    updated_at = _epoch(_row_value(row, "updated_at"))
    if created_at <= 0 or updated_at <= 0:
        return -1.0
    return round(updated_at - created_at, 3)


def _command_tool_status_key(row: sqlite3.Row) -> str:
    tool = _command_tool(row) or "unknown"
    status = str(_row_value(row, "status", "") or "unknown")
    return f"{tool}:{status}"


def _command_is_problem(row: sqlite3.Row) -> bool:
    status = str(_row_value(row, "status", "") or "")
    error = _command_error(row)
    return status in _COMMAND_PROBLEM_STATUSES or "COMMAND_TIMED_OUT" in error


def _command_sample(row: sqlite3.Row, *, duration: float) -> JsonDict:
    return {
        "command_id": str(_row_value(row, "command_id", "")),
        "tool": _command_tool(row) or "unknown",
        "status": str(_row_value(row, "status", "")),
        "duration_seconds": round(duration, 3) if duration >= 0 else None,
        "session_id": str(_row_value(row, "session_id", "")),
        "device_id": str(_row_value(row, "device_id", "")),
        "created_at": _row_value(row, "created_at", ""),
        "updated_at": _row_value(row, "updated_at", ""),
        "error": _shorten(_command_error(row), 240),
    }


def _command_error(row: sqlite3.Row) -> str:
    result = _loads(str(_row_value(row, "result_json", "") or "{}"))
    for key in ("error", "message", "detail", "reason", "stderr"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("error", "message", "detail", "reason"):
        value = _find_string(result, key)
        if value:
            return value
    raw = str(_row_value(row, "result_json", "") or "")
    return raw[:500]


def _command_is_evaluator_shell_exec(row: sqlite3.Row) -> bool:
    haystack = " ".join(
        (
            str(_row_value(row, "command_id", "")),
            str(_row_value(row, "session_id", "")),
            str(_row_value(row, "request_json", "")),
        )
    )
    return "evaluator-client_bash_readonly" in haystack


def _command_tool(row: sqlite3.Row) -> str:
    request = _loads(str(_row_value(row, "request_json", "") or "{}"))
    for key in ("tool_id", "tool_name", "name", "command", "action"):
        value = request.get(key)
        if isinstance(value, str):
            return value
    payload = request.get("payload")
    if isinstance(payload, dict):
        value = payload.get("tool_id") or payload.get("tool_name") or payload.get("name")
        if isinstance(value, str):
            return value
    parameters = request.get("parameters")
    if isinstance(parameters, dict):
        value = parameters.get("tool_id") or parameters.get("tool_name") or parameters.get("name")
        if isinstance(value, str):
            return value
    return ""


def _prepared_age_anchor(row: sqlite3.Row, status: str) -> float:
    if status == "preparing":
        return _epoch(_row_value(row, "prepare_started_at"))
    if status == "prepared":
        return _epoch(_row_value(row, "prepared_at"))
    if status == "assigned":
        return _epoch(_row_value(row, "assigned_at"))
    if status == "reserved":
        return _epoch(_row_value(row, "reserved_at"))
    return _epoch(_row_value(row, "updated_at"))


def _is_unexpired(row: sqlite3.Row, *, now: float) -> bool:
    expires_at = _epoch(_row_value(row, "expires_at"))
    return expires_at <= 0 or expires_at > now


def _row_value(row: sqlite3.Row, key: str, default: object = None) -> object:
    try:
        if key not in row.keys():
            return default
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _task_metadata(row: sqlite3.Row) -> JsonDict:
    request = _loads(str(row["request_json"] or "{}"))
    metadata = request.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _jsonl_rows(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _find_string(value: object, key: str) -> str:
    if isinstance(value, dict):
        item = value.get(key)
        if isinstance(item, str):
            return item
        for nested in value.values():
            found = _find_string(nested, key)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_string(nested, key)
            if found:
                return found
    return ""


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _loads(raw: str) -> JsonDict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _controller_result_json_accepts_uncontrollable(result_json: object) -> bool:
    report = _evaluator_report_from_controller_result_json(result_json)
    return (
        _report_value(report, "overall_verdict") == "success"
        and _report_value(report, "controllability") == "uncontrollable"
    )


def _evaluator_report_from_controller_result_json(result_json: object) -> JsonDict | None:
    if not result_json:
        return None
    if isinstance(result_json, dict):
        return _evaluator_report_from_result(result_json)
    try:
        payload = json.loads(str(result_json))
    except (TypeError, ValueError):
        return None
    return _evaluator_report_from_result(payload)


def _evaluator_report_from_result(result: object) -> JsonDict | None:
    if not isinstance(result, dict):
        return None
    for key in ("evaluation_report", "round_evaluation", "final_report", "evaluation", "record"):
        report = result.get(key)
        if isinstance(report, dict):
            return {str(item_key): item for item_key, item in report.items()}
    nested = result.get("result")
    if isinstance(nested, dict):
        return _evaluator_report_from_result(nested)
    return None


def main() -> None:
    """Run the health-report CLI entrypoint."""

    from northcua.maintenance.batch_health_report_cli import main as cli_main

    cli_main()


if __name__ == "__main__":  # pragma: no cover
    main()
