"""Low-memory one-shot monitor for large NorthCUA batch runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

JsonDict = dict[str, object]

_ACTIVE_STATUSES = {"starting_sandbox", "bootstrapping_device", "waiting_device", "submitted", "running", "active"}
_TERMINAL_STATUSES = {"succeeded", "evaluation_pending", "failed", "timed_out", "canceled"}
_ERROR_MESSAGE_NEEDLES = (
    "failed",
    "timeout",
    "disconnect",
    "traceback",
    "null",
    "fallback",
    "device_ready",
    "auth",
)
_SUBMIT_MESSAGES = (
    "controller_submit_started",
    "controller_submit_open_started",
    "controller_submit_request_sent",
    "controller_submit_accepted",
    "controller_submit_failed_before_terminal",
)


def generate_lightweight_batch_monitor_report(
    *,
    batch_db: Path,
    controller_db: Path | None = None,
    run_dir: Path | None = None,
    attempt_events: Path | None = None,
    prepared_events: Path | None = None,
    runtime_jsonl: Iterable[Path] = (),
    launch_time: str = "",
    tail_mb: int = 32,
    windows_minutes: Iterable[int] = (5, 10, 20, 30, 60),
    include_processes: bool = True,
    slow_controller_counts: bool = False,
) -> JsonDict:
    """Return a bounded-memory operator snapshot for a large batch run.

    This monitor intentionally avoids the heavier health report paths. It uses
    SQL aggregation, parses only a bounded tail of JSONL event files, and never
    recursively scans agent artifacts unless explicit runtime JSONL files are
    passed in.

    Args:
        batch_db: Batch state SQLite database to aggregate.
        controller_db: Optional controller SQLite database for lightweight
            session/job counts.
        run_dir: Batch run directory containing event JSONL files.
        attempt_events: Optional explicit attempt event JSONL path.
        prepared_events: Optional explicit prepared-sandbox event JSONL path.
        runtime_jsonl: Optional runtime event files for LLM statistics.
        launch_time: ISO timestamp used to filter event tails to this resume.
        tail_mb: Maximum MiB read from each JSONL tail.
        windows_minutes: Throughput windows to calculate.
        include_processes: Whether to sample ps/tmux/key-tail process state.
        slow_controller_counts: Whether to run slower controller table counts.

    Returns:
        JSON-serializable report dictionary.
    """

    batch_db = batch_db.expanduser()
    controller_db = controller_db.expanduser() if controller_db is not None else None
    run_dir = run_dir.expanduser() if run_dir is not None else batch_db.parent
    attempt_events = (
        attempt_events.expanduser() if attempt_events is not None else run_dir / "batch_attempt_events.jsonl"
    )
    prepared_events = (
        prepared_events.expanduser() if prepared_events is not None else run_dir / "prepared_sandbox_events.jsonl"
    )
    launch_ts = _parse_time_to_epoch(launch_time)
    now = time.time()
    report: JsonDict = {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "launch_time": launch_time,
        "elapsed_seconds": round(now - launch_ts, 3) if launch_ts > 0 else None,
        "paths": {
            "batch_db": str(batch_db),
            "controller_db": str(controller_db or ""),
            "run_dir": str(run_dir),
            "attempt_events": str(attempt_events),
            "prepared_events": str(prepared_events),
        },
        "batch": _batch_db_snapshot(batch_db),
        "prepared_sandboxes": _prepared_db_snapshot(batch_db),
        "controller": _controller_db_snapshot(controller_db, slow_counts=slow_controller_counts),
        "attempt_events": _attempt_event_tail_snapshot(
            attempt_events,
            launch_ts=launch_ts,
            now=now,
            tail_bytes=max(int(tail_mb), 1) * 1024 * 1024,
            windows_minutes=tuple(windows_minutes),
        ),
        "prepared_events": _prepared_event_tail_snapshot(
            prepared_events,
            launch_ts=launch_ts,
            tail_bytes=max(min(int(tail_mb), 120), 1) * 1024 * 1024,
        ),
        "runtime_jsonl": _runtime_jsonl_snapshot(
            _runtime_jsonl_paths(run_dir=run_dir, explicit_paths=runtime_jsonl),
            launch_ts=launch_ts,
            tail_bytes=min(max(int(tail_mb), 1), 8) * 1024 * 1024,
        ),
        "runtime": _runtime_snapshot(include_processes=include_processes),
    }
    report["summary"] = _summary(report)
    report["markdown"] = render_lightweight_markdown(report)
    return report


def render_lightweight_markdown(report: JsonDict) -> str:
    """Render the lightweight monitor report as compact Markdown.

    Args:
        report: Structured snapshot returned by
            ``generate_lightweight_batch_monitor_report``.

    Returns:
        Operator-facing Markdown summary.
    """

    summary = _dict(report.get("summary"))
    batch = _dict(report.get("batch"))
    attempt = _dict(report.get("attempt_events"))
    runtime = _dict(report.get("runtime"))
    memory = _dict(runtime.get("memory"))
    lines = [
        f"Generated: {report.get('generated_at')}",
        f"Elapsed: {_format_duration(report.get('elapsed_seconds'))}",
        f"Needs intervention: {summary.get('needs_intervention')}",
        (
            "Batch: "
            f"total={batch.get('total')} succeeded={batch.get('succeeded')} failed={batch.get('failed')} "
            f"timed_out={batch.get('timed_out')} canceled={batch.get('canceled')} submitted={batch.get('submitted')} "
            f"active={batch.get('active_total')} pending={batch.get('pending')}"
        ),
        (
            "Rates: "
            f"completion={summary.get('completion_rate')} success={summary.get('success_rate')} "
            f"eta={summary.get('eta')}"
        ),
        f"Throughput: {attempt.get('throughput_windows')}",
        f"Controller: {_dict(report.get('controller')).get('tables')}",
        f"Worker RSS: {_dict(_dict(report.get('controller')).get('worker_heartbeat')).get('rss')}",
        f"Controller timings: {_dict(_dict(report.get('controller')).get('runtime')).get('operation_timings')}",
        f"Prepared: {_dict(report.get('prepared_sandboxes')).get('status_counts')}",
        f"LLM wall-clock: {_dict(report.get('runtime_jsonl')).get('llm_call_wall_seconds')}",
        f"LLM calls: {_dict(report.get('runtime_jsonl')).get('llm_calls')}",
        f"LLM fallback: {_dict(report.get('runtime_jsonl')).get('fallback')}",
        (
            "Runtime: "
            f"memory={memory.get('current_gib')}/{memory.get('limit_gib')}GiB "
            f"oom_kill={_dict(memory.get('events')).get('oom_kill')} "
            f"cpu={_dict(runtime.get('cpu')).get('stat')}"
        ),
        f"Top issues: {summary.get('issues')}",
    ]
    return "\n".join(lines)


def _batch_db_snapshot(batch_db: Path) -> JsonDict:
    warnings: list[str] = []
    if not batch_db.exists():
        return {"warnings": [f"batch DB not found: {batch_db}"]}
    with _connect(batch_db) as conn:
        if not _table_exists(conn, "batch_tasks"):
            return {"warnings": ["batch_tasks table not found"]}
        status_counts = _count_by(conn, "batch_tasks", "status")
        total = sum(status_counts.values())
        terminal_total = sum(status_counts.get(status, 0) for status in _TERMINAL_STATUSES)
        active_total = sum(status_counts.get(status, 0) for status in _ACTIVE_STATUSES)
        failure_categories: list[JsonDict] = []
        retry_budget: list[JsonDict] = []
        if _has_columns(conn, "batch_tasks", {"status", "last_failure_category", "retry_budget_remaining"}):
            failure_categories = _rows(
                conn,
                """
                SELECT status, last_failure_category, retry_budget_remaining, COUNT(*) AS count
                FROM batch_tasks
                WHERE status IN ('failed', 'timed_out', 'canceled')
                GROUP BY status, last_failure_category, retry_budget_remaining
                ORDER BY count DESC
                LIMIT 20
                """,
            )
            retry_budget = _rows(
                conn,
                """
                SELECT status, retry_budget_remaining, COUNT(*) AS count
                FROM batch_tasks
                WHERE status IN ('failed', 'timed_out', 'canceled')
                GROUP BY status, retry_budget_remaining
                ORDER BY status, retry_budget_remaining
                """,
            )
        active_owners = _active_owner_counts(conn)
    return {
        "total": total,
        "status_counts": status_counts,
        "terminal_total": terminal_total,
        "active_total": active_total,
        "completion_rate": _ratio(terminal_total, total),
        "success_rate": _ratio(status_counts.get("succeeded", 0), terminal_total),
        "succeeded": status_counts.get("succeeded", 0),
        "failed": status_counts.get("failed", 0),
        "timed_out": status_counts.get("timed_out", 0),
        "canceled": status_counts.get("canceled", 0),
        "submitted": status_counts.get("submitted", 0),
        "pending": status_counts.get("pending", 0),
        "failure_categories": failure_categories,
        "retry_budget": retry_budget,
        "active_owners": active_owners,
        "warnings": warnings,
    }


def _prepared_db_snapshot(batch_db: Path) -> JsonDict:
    if not batch_db.exists():
        return {}
    with _connect(batch_db) as conn:
        if not _table_exists(conn, "prepared_sandboxes"):
            return {}
        counts = _count_by(conn, "prepared_sandboxes", "status")
        rows: JsonDict = {"status_counts": counts}
        if _has_columns(conn, "prepared_sandboxes", {"status", "expires_at"}):
            now = time.time()
            rows["active_expired_total"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM prepared_sandboxes
                    WHERE status IN ('reserved', 'preparing', 'prepared', 'assigned')
                      AND expires_at > 0 AND expires_at < ?
                    """,
                    (now,),
                ).fetchone()[0]
            )
        return rows


def _controller_db_snapshot(controller_db: Path | None, *, slow_counts: bool) -> JsonDict:
    if controller_db is None:
        return {}
    if not controller_db.exists():
        return {"warnings": [f"controller DB not found: {controller_db}"]}
    table_counts: dict[str, dict[str, int]] = {}
    skipped_tables: list[str] = []
    with _connect(controller_db) as conn:
        for table in ("tasks", "sessions", "worker_jobs", "devices", "leases", "commands"):
            if _table_exists(conn, table) and _has_columns(conn, table, {"status"}):
                if slow_counts or table in {"sessions", "worker_jobs"}:
                    table_counts[table] = _count_by(conn, table, "status")
                else:
                    skipped_tables.append(table)
        runtime = _controller_runtime_tail(conn)
        worker_heartbeat = _worker_heartbeat_snapshot(conn)
    return {
        "tables": table_counts,
        "runtime": runtime,
        "worker_heartbeat": worker_heartbeat,
        "skipped_slow_tables": skipped_tables,
    }


def _worker_heartbeat_snapshot(conn: sqlite3.Connection) -> JsonDict:
    """Summarize active worker heartbeat memory without scanning large tables."""

    required = {"status", "heartbeat_payload_json"}
    if not _table_exists(conn, "worker_jobs") or not _has_columns(conn, "worker_jobs", required):
        return {}
    order_clause = "ORDER BY heartbeat_at DESC" if _has_columns(conn, "worker_jobs", {"heartbeat_at"}) else ""
    rows = conn.execute(f"""
        SELECT status, heartbeat_payload_json
        FROM worker_jobs
        WHERE status IN ('queued', 'starting', 'running', 'finishing')
        {order_clause}
        LIMIT 512
        """).fetchall()
    phases: Counter[str] = Counter()
    legacy_rss_mib: list[float] = []
    peak_rss_mib: list[float] = []
    current_rss_mib: list[float] = []
    known_step_count = 0
    unknown_step_count = 0
    no_heartbeat_payload_count = 0
    active_command_count = 0
    for row in rows:
        raw_payload = str(row["heartbeat_payload_json"] or "").strip()
        if not raw_payload or raw_payload == "{}":
            no_heartbeat_payload_count += 1
            continue
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            no_heartbeat_payload_count += 1
            continue
        if not isinstance(payload, dict):
            no_heartbeat_payload_count += 1
            continue
        phase = str(payload.get("phase") or "")
        if phase:
            phases[phase] += 1
        step_index = payload.get("step_index")
        if isinstance(step_index, int) and not isinstance(step_index, bool) and step_index > 0:
            known_step_count += 1
        else:
            unknown_step_count += 1
        if str(payload.get("active_command_id") or ""):
            active_command_count += 1
        for key, values in (
            ("rss_bytes", legacy_rss_mib),
            ("peak_rss_bytes", peak_rss_mib),
            ("current_rss_bytes", current_rss_mib),
        ):
            value = payload.get(key)
            if isinstance(value, int | float) and value >= 0:
                values.append(float(value) / 1024 / 1024)
    return {
        "sampled_workers": len(rows),
        "phases": dict(phases),
        "step_index": {
            "known": known_step_count,
            "unknown": unknown_step_count,
            "no_heartbeat_payload": no_heartbeat_payload_count,
        },
        "active_command_workers": active_command_count,
        "rss": {
            "legacy_peak_mib": _percentiles(legacy_rss_mib),
            "peak_mib": _percentiles(peak_rss_mib),
            "current_mib": _percentiles(current_rss_mib),
        },
    }


def _attempt_event_tail_snapshot(
    path: Path,
    *,
    launch_ts: float,
    now: float,
    tail_bytes: int,
    windows_minutes: tuple[int, ...],
) -> JsonDict:
    if not path.exists():
        return {"warnings": [f"attempt events not found: {path}"]}
    messages: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    terminal_events: list[tuple[float, str]] = []
    by_second: dict[str, Counter[str]] = {message: Counter() for message in _SUBMIT_MESSAGES}
    preflight: list[dict[str, object]] = []
    traceprune_total = 0
    ttl_related_total = 0
    no_hot_lease_total = 0
    db_timeout_total = 0
    websocket_fallback_true_total = 0
    for event in _iter_jsonl_tail(path, tail_bytes=tail_bytes):
        timestamp = _event_timestamp(event)
        if timestamp is None or (launch_ts > 0 and timestamp < launch_ts):
            continue
        message = str(event.get("message") or "")
        details = _dict(event.get("details"))
        messages[message] += 1
        second = _event_second(event)
        if message in by_second and second:
            by_second[message][second] += 1
        if message == "attempt_cleanup_finished":
            terminal = str(details.get("terminal_status") or details.get("status") or "unknown")
            terminal_events.append((timestamp, terminal))
        if message == "controller_submit_preflight_finished":
            preflight.append(details)
        message_lower = message.lower()
        if any(needle in message_lower for needle in _ERROR_MESSAGE_NEEDLES):
            errors[message] += 1
        if "tracer_spans_pruned" in message:
            traceprune_total += 1
        if "ttl" in message_lower or "lease" in message_lower:
            ttl_related_total += 1
        if details.get("lease_extension_skipped") == "provider_timeout_set_at_prepare_create":
            no_hot_lease_total += 1
        if "db" in message_lower and "timeout" in message_lower:
            db_timeout_total += 1
        if details.get("websocket_fallback") is True:
            websocket_fallback_true_total += 1
    terminal_counts = Counter(status for _, status in terminal_events)
    return {
        "messages_top": _counter_items(messages, limit=40),
        "errors_top": _counter_items(errors, limit=30),
        "terminal_total": len(terminal_events),
        "terminal_counts": dict(terminal_counts),
        "throughput_windows": _throughput_windows(terminal_events, now=now, windows_minutes=windows_minutes),
        "submit_by_second": {message: _by_second_summary(counter) for message, counter in by_second.items()},
        "preflight": _preflight_summary(preflight),
        "traceprune_total": traceprune_total,
        "ttl_related_total": ttl_related_total,
        "no_hot_lease_total": no_hot_lease_total,
        "db_timeout_total": db_timeout_total,
        "websocket_fallback_true_total": websocket_fallback_true_total,
        "tail_bytes": tail_bytes,
    }


def _prepared_event_tail_snapshot(path: Path, *, launch_ts: float, tail_bytes: int) -> JsonDict:
    if not path.exists():
        return {}
    messages: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for event in _iter_jsonl_tail(path, tail_bytes=tail_bytes):
        timestamp = _event_timestamp(event)
        if timestamp is None or (launch_ts > 0 and timestamp < launch_ts):
            continue
        messages[str(event.get("message") or "")] += 1
        details = _dict(event.get("details"))
        status = str(details.get("status") or details.get("prepared_status") or "")
        if status:
            statuses[status] += 1
    return {"messages_top": _counter_items(messages, limit=20), "status_counts": dict(statuses)}


def _runtime_jsonl_snapshot(paths: Iterable[Path], *, launch_ts: float, tail_bytes: int) -> JsonDict:
    """Summarize runtime JSONL files using model-call wall-clock events.

    The agent-facing ``llm_trace.jsonl`` stores provider response payloads.
    For Responses API, ``response.created_at`` to ``completed_at`` captures
    only server-side generation time and misses client-side retries, fallback,
    request upload, and wrapper timeouts. Runtime logs contain the outer
    ``single_model_step_llm_call_*`` lifecycle events, so this monitor treats
    those as the source of truth for LLM latency.
    """

    event_counts: Counter[str] = Counter()
    models: Counter[str] = Counter()
    api_types: Counter[str] = Counter()
    configured_max_retries: Counter[str] = Counter()
    configured_timeouts: Counter[str] = Counter()
    wrapper_timeouts: Counter[str] = Counter()
    llm_call_wall_seconds: list[float] = []
    llm_completed_seconds: list[float] = []
    llm_timeout_seconds: list[float] = []
    llm_failed_seconds: list[float] = []
    request_size_bytes: list[float] = []
    fallback_started: Counter[str] = Counter()
    fallback_selected: Counter[str] = Counter()
    fallback_failed: Counter[str] = Counter()
    fallback_error_types: Counter[str] = Counter()
    llm_calls: Counter[str] = Counter()
    files_read = 0
    files_seen = 0
    for path in paths:
        expanded = path.expanduser()
        files_seen += 1
        if not expanded.exists():
            continue
        files_read += 1
        for event in _iter_jsonl_tail(expanded, tail_bytes=tail_bytes):
            timestamp = _event_timestamp(event)
            if timestamp is not None and launch_ts > 0 and timestamp < launch_ts:
                continue
            message = str(event.get("message") or event.get("event") or event.get("type") or "")
            details = _runtime_event_details(event)
            message_lower = message.lower()
            if "llm" not in message_lower and "model" not in message_lower and "trace_capture" not in message_lower:
                continue
            event_counts[message] += 1
            model = str(details.get("model") or "")
            if model:
                models[model] += 1
            api_type = str(details.get("api_type") or "")
            if api_type:
                api_types[api_type] += 1
            _count_optional_value(configured_max_retries, details.get("configured_max_retries"))
            _count_optional_value(configured_timeouts, details.get("configured_timeout_seconds"))
            _count_optional_value(wrapper_timeouts, details.get("timeout_seconds"))
            request_size = details.get("request_size_bytes")
            if isinstance(request_size, int | float):
                request_size_bytes.append(float(request_size))
            if message == "single_model_step_llm_call_started":
                llm_calls["started"] += 1
            elif message == "single_model_step_llm_call_completed":
                llm_calls["completed"] += 1
                _append_duration(details, llm_completed_seconds, llm_call_wall_seconds)
            elif message == "single_model_step_llm_call_timed_out":
                llm_calls["timed_out"] += 1
                _append_duration(details, llm_timeout_seconds, llm_call_wall_seconds)
            elif message == "single_model_step_llm_call_failed":
                llm_calls["failed"] += 1
                _append_duration(details, llm_failed_seconds, llm_call_wall_seconds)
            elif message == "single_model_step_llm_call_cancelled":
                llm_calls["cancelled"] += 1
            elif message == "llm_fallback_attempt_started":
                fallback_started[_candidate_label(details)] += 1
            elif message == "llm_fallback_attempt_selected":
                fallback_selected[_candidate_label(details)] += 1
            elif message == "llm_fallback_attempt_failed":
                fallback_failed[_candidate_label(details)] += 1
                fallback_error_types[str(details.get("error_type") or "unknown")] += 1
    terminal_calls = (
        llm_calls.get("completed", 0)
        + llm_calls.get("timed_out", 0)
        + llm_calls.get("failed", 0)
        + llm_calls.get("cancelled", 0)
    )
    if llm_calls.get("started", 0) > terminal_calls:
        llm_calls["inflight"] = llm_calls["started"] - terminal_calls
    return {
        "files_seen": files_seen,
        "files_read": files_read,
        "events_top": _counter_items(event_counts, limit=20),
        "models": dict(models),
        "api_types": dict(api_types),
        "configured_max_retries": dict(configured_max_retries),
        "configured_timeout_seconds": dict(configured_timeouts),
        "wrapper_timeout_seconds": dict(wrapper_timeouts),
        "request_size_bytes": _percentiles(request_size_bytes),
        "llm_calls": dict(llm_calls),
        "llm_call_wall_seconds": _percentiles(llm_call_wall_seconds),
        "llm_completed_wall_seconds": _percentiles(llm_completed_seconds),
        "llm_timeout_wall_seconds": _percentiles(llm_timeout_seconds),
        "llm_failed_wall_seconds": _percentiles(llm_failed_seconds),
        "fallback": {
            "started": dict(fallback_started),
            "selected": dict(fallback_selected),
            "failed": dict(fallback_failed),
            "error_types": dict(fallback_error_types),
        },
    }


def _runtime_snapshot(*, include_processes: bool) -> JsonDict:
    memory = _memory_snapshot()
    cpu = _cpu_snapshot()
    if not include_processes:
        return {"memory": memory, "cpu": cpu, "processes": {}, "tmux": {}}
    return {"memory": memory, "cpu": cpu, "processes": _process_snapshot(), "tmux": _tmux_snapshot()}


def _summary(report: JsonDict) -> JsonDict:
    batch = _dict(report.get("batch"))
    attempt = _dict(report.get("attempt_events"))
    runtime = _dict(report.get("runtime"))
    memory = _dict(runtime.get("memory"))
    status_counts = _dict(batch.get("status_counts"))
    terminal_total = int(batch.get("terminal_total") or 0)
    total = int(batch.get("total") or 0)
    pending = int(status_counts.get("pending") or 0)
    throughput = _dict(attempt.get("throughput_windows"))
    eta = _eta(pending=pending, throughput_windows=throughput)
    issues: list[str] = []
    oom_kill = int(_dict(memory.get("events")).get("oom_kill") or 0)
    if float(memory.get("current_gib") or 0.0) >= 63.0:
        issues.append("memory_current_ge_63gib")
    if int(attempt.get("websocket_fallback_true_total") or 0) > 0:
        issues.append("websocket_fallback_used")
    if attempt.get("errors_top"):
        issues.append("tail_error_events_present")
    runtime_jsonl = _dict(report.get("runtime_jsonl"))
    llm_calls = _dict(runtime_jsonl.get("llm_calls"))
    if int(llm_calls.get("timed_out") or 0) > 0:
        issues.append("llm_timeouts_present")
    return {
        "completion_rate": _ratio(terminal_total, total),
        "success_rate": batch.get("success_rate"),
        "eta": eta,
        "oom_kill": oom_kill,
        "issues": issues,
        "needs_intervention": bool(issues),
    }


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _has_columns(conn: sqlite3.Connection, table: str, columns: set[str]) -> bool:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return columns.issubset(existing)


def _count_by(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    if not _has_columns(conn, table, {column}):
        return {}
    rows = conn.execute(f"SELECT {column}, COUNT(*) AS count FROM {table} GROUP BY {column}").fetchall()
    return {str(row[0] or ""): int(row[1]) for row in rows}


def _rows(conn: sqlite3.Connection, query: str) -> list[JsonDict]:
    return [dict(row) for row in conn.execute(query).fetchall()]


def _active_owner_counts(conn: sqlite3.Connection) -> list[JsonDict]:
    if not _has_columns(conn, "batch_tasks", {"status", "state_json"}):
        return []
    owners: Counter[tuple[str, str]] = Counter()
    rows = conn.execute(
        "SELECT status, state_json FROM batch_tasks WHERE status IN (?, ?, ?, ?, ?, ?)",
        tuple(sorted(_ACTIVE_STATUSES)),
    ).fetchall()
    for row in rows:
        owner = ""
        try:
            payload = json.loads(str(row["state_json"] or "{}"))
            if isinstance(payload, dict):
                owner = str(payload.get("owner_id") or payload.get("runner_id") or payload.get("claimed_by") or "")
        except json.JSONDecodeError:
            owner = ""
        owners[(str(row["status"]), owner)] += 1
    return [{"status": status, "owner": owner, "count": count} for (status, owner), count in owners.most_common(20)]


def _controller_runtime_tail(conn: sqlite3.Connection) -> JsonDict:
    if not _table_exists(conn, "events") or not _has_columns(
        conn, "events", {"event_type", "payload_json", "created_at"}
    ):
        return {}
    rows = conn.execute("""
        SELECT payload_json, created_at
        FROM events
        WHERE event_type = 'controller_runtime_telemetry'
        ORDER BY created_at DESC
        LIMIT 200
        """).fetchall()
    lags: list[float] = []
    sockets: list[float] = []
    latest_operation_timings: JsonDict = {}
    operation_counts: Counter[str] = Counter()
    operation_p95_by_interval: defaultdict[str, list[float]] = defaultdict(list)
    operation_max_by_interval: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        lag = payload.get("event_loop_lag_ms")
        socket_count = payload.get("socket_count")
        if isinstance(lag, int | float):
            lags.append(float(lag))
        if isinstance(socket_count, int | float):
            sockets.append(float(socket_count))
        raw_timings = payload.get("controller_operation_timings")
        if isinstance(raw_timings, dict):
            if not latest_operation_timings:
                latest_operation_timings = raw_timings
            for operation, raw_stats in raw_timings.items():
                if not isinstance(operation, str) or not isinstance(raw_stats, dict):
                    continue
                operation_counts[operation] += int(raw_stats.get("count") or 0)
                p95_ms = raw_stats.get("p95_ms")
                max_ms = raw_stats.get("max_ms")
                if isinstance(p95_ms, int | float):
                    operation_p95_by_interval[operation].append(float(p95_ms))
                if isinstance(max_ms, int | float):
                    operation_max_by_interval[operation].append(float(max_ms))
    operation_timings = _operation_timing_summary(
        operation_counts,
        operation_p95_by_interval,
        operation_max_by_interval,
        latest_operation_timings,
    )
    return {
        "event_loop_lag_ms": _percentiles(lags),
        "socket_count": _percentiles(sockets),
        "operation_timings": operation_timings,
        "samples": len(rows),
    }


def _operation_timing_summary(
    counts: Counter[str],
    p95_by_interval: defaultdict[str, list[float]],
    max_by_interval: defaultdict[str, list[float]],
    latest: JsonDict,
) -> JsonDict:
    ranked = sorted(
        counts,
        key=lambda operation: (
            max(p95_by_interval.get(operation, [0.0])),
            counts[operation],
            operation,
        ),
        reverse=True,
    )
    return {
        operation: {
            "count": counts[operation],
            "interval_p95_ms": _percentiles(p95_by_interval.get(operation, [])),
            "interval_max_ms": _percentiles(max_by_interval.get(operation, [])),
            "latest": _dict(latest.get(operation)),
        }
        for operation in ranked[:20]
    }


def _iter_jsonl_tail(path: Path, *, tail_bytes: int) -> Iterable[dict[str, object]]:
    size = path.stat().st_size
    start = max(0, size - tail_bytes)
    with path.open("rb") as handle:
        handle.seek(start)
        if start:
            handle.readline()
        for raw in handle:
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _event_timestamp(event: dict[str, object]) -> float | None:
    raw = event.get("timestamp") or event.get("time") or event.get("created_at") or event.get("ts")
    if not raw:
        return None
    return _parse_time_to_epoch(str(raw))


def _event_second(event: dict[str, object]) -> str:
    raw = str(event.get("timestamp") or event.get("time") or event.get("created_at") or event.get("ts") or "")
    return raw[:19]


def _runtime_jsonl_paths(*, run_dir: Path, explicit_paths: Iterable[Path]) -> tuple[Path, ...]:
    """Return explicit runtime logs plus common agent-artifact discoveries."""

    candidates: list[Path] = [path.expanduser() for path in explicit_paths]
    roots = (run_dir, run_dir.parent, run_dir.parent.parent)
    for root in roots:
        artifact_root = root / "agent-artifacts"
        if not artifact_root.exists():
            continue
        candidates.extend(sorted(artifact_root.glob("*/runtime.jsonl")))
    deduped: dict[str, Path] = {}
    for path in candidates:
        deduped.setdefault(str(path), path)
    return tuple(deduped.values())


def _runtime_event_details(event: dict[str, object]) -> dict[str, object]:
    details = _dict(event.get("details"))
    extra = _dict(event.get("extra"))
    if details or extra:
        merged = dict(details)
        merged.update(extra)
        return merged
    return event


def _append_duration(details: dict[str, object], primary: list[float], aggregate: list[float]) -> None:
    duration = details.get("elapsed_seconds") or details.get("duration_seconds") or details.get("latency_seconds")
    if isinstance(duration, int | float):
        value = float(duration)
        primary.append(value)
        aggregate.append(value)


def _candidate_label(details: dict[str, object]) -> str:
    index = details.get("candidate_index")
    name = details.get("candidate_name") or details.get("model") or "unknown"
    primary = details.get("is_primary")
    if primary is True:
        suffix = ":primary"
    elif primary is False:
        suffix = ":fallback"
    else:
        suffix = ""
    return f"{index}:{name}{suffix}"


def _count_optional_value(counter: Counter[str], value: object) -> None:
    if value is None or value == "":
        return
    counter[str(value)] += 1


def _parse_time_to_epoch(value: str) -> float:
    raw = value.strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _throughput_windows(
    terminal_events: list[tuple[float, str]],
    *,
    now: float,
    windows_minutes: tuple[int, ...],
) -> dict[str, JsonDict]:
    windows: dict[str, JsonDict] = {}
    for minutes in windows_minutes:
        cutoff = now - minutes * 60
        statuses = [status for timestamp, status in terminal_events if timestamp >= cutoff]
        counts = Counter(statuses)
        windows[str(minutes)] = {
            "terminal_delta": len(statuses),
            "status_counts": dict(counts),
            "cases_per_hour": round(len(statuses) * 60 / minutes, 3) if minutes > 0 else 0.0,
        }
    return windows


def _by_second_summary(counter: Counter[str]) -> JsonDict:
    if not counter:
        return {"max": 0, "top": []}
    return {
        "max": max(counter.values()),
        "top": [{"second": key, "count": value} for key, value in counter.most_common(5)],
    }


def _preflight_summary(rows: list[dict[str, object]]) -> JsonDict:
    summary: JsonDict = {"count": len(rows)}
    for key in (
        "backpressure_wait_seconds",
        "upsert_task_submitted_seconds",
        "effective_submit_jitter_seconds",
        "sample_duration_seconds",
    ):
        summary[key] = _percentiles([float(row[key]) for row in rows if isinstance(row.get(key), int | float)])
    summary["bp_active"] = sum(1 for row in rows if row.get("bp_active"))
    summary["sample_timed_out"] = sum(1 for row in rows if row.get("sample_timed_out"))
    return summary


def _percentiles(values: Iterable[float]) -> JsonDict:
    sorted_values = sorted(float(value) for value in values)
    if not sorted_values:
        return {"count": 0, "p50": None, "p90": None, "p95": None, "max": None}

    def _pick(percentile: int) -> float:
        index = math.ceil((len(sorted_values) - 1) * percentile / 100)
        return round(sorted_values[index], 3)

    return {
        "count": len(sorted_values),
        "p50": _pick(50),
        "p90": _pick(90),
        "p95": _pick(95),
        "max": round(max(sorted_values), 3),
    }


def _counter_items(counter: Counter[str], *, limit: int) -> list[JsonDict]:
    return [{"name": name, "count": count} for name, count in counter.most_common(limit)]


def _counter_total(value: dict[str, object]) -> int:
    total = 0
    for item in value.values():
        if isinstance(item, int):
            total += item
    return total


def _memory_snapshot() -> JsonDict:
    current_bytes = _read_int(Path("/sys/fs/cgroup/memory.current"))
    events = _read_key_values(Path("/sys/fs/cgroup/memory.events"))
    limit_bytes = _read_int(Path("/sys/fs/cgroup/memory.max"))
    if limit_bytes <= 0:
        limit_bytes = 64 * 1024**3
    return {
        "current_bytes": current_bytes,
        "current_gib": round(current_bytes / 1024**3, 3) if current_bytes else 0.0,
        "limit_bytes": limit_bytes,
        "limit_gib": round(limit_bytes / 1024**3, 3) if limit_bytes else 0.0,
        "events": events,
    }


def _cpu_snapshot() -> JsonDict:
    return {"stat": _read_key_values(Path("/sys/fs/cgroup/cpu.stat"))}


def _process_snapshot() -> JsonDict:
    try:
        output = subprocess.check_output(["ps", "-eo", "pid,rss,pcpu,comm,args"], text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    rows = [line for line in output.splitlines() if "northcua" in line or "python" in line or "cloudflared" in line]
    top = sorted(rows[1:], key=_rss_from_ps_line, reverse=True)[:20]
    runner_pid = _runner_pid(rows)
    return {"top_rss": top, "runner_pid": runner_pid, "runner_key_env_tails": _key_env_tails(runner_pid)}


def _tmux_snapshot() -> JsonDict:
    try:
        output = subprocess.check_output(["tmux", "ls"], text=True, timeout=5, stderr=subprocess.STDOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"northcua_sessions": [line for line in output.splitlines() if "northcua_v17_office" in line]}


def _read_int(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if raw == "max":
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _read_key_values(path: Path) -> dict[str, int]:
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, int] = {}
    for line in raw:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return values


def _rss_from_ps_line(line: str) -> int:
    parts = line.split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def _runner_pid(rows: list[str]) -> str:
    for line in rows:
        if "case run" not in line or "northcua" not in line:
            continue
        parts = line.split(None, 4)
        if parts and parts[0].isdigit():
            return parts[0]
    return ""


def _key_env_tails(pid: str) -> list[JsonDict]:
    if not pid:
        return []
    try:
        raw_items = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return []
    results: list[JsonDict] = []
    for raw in raw_items:
        if b"KEY" not in raw and b"OPENAI" not in raw and b"API" not in raw:
            continue
        text = raw.decode("utf-8", errors="ignore")
        if "=" not in text:
            continue
        name, value = text.split("=", 1)
        if not value or (not value.startswith("sk-") and len(value) <= 20):
            continue
        results.append({"name": name, "tail": value[-6:], "sha8": hashlib.sha256(value.encode()).hexdigest()[:8]})
    return results


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _eta(*, pending: int, throughput_windows: dict[str, object]) -> str:
    for key in ("30", "20", "10", "60", "5"):
        window = _dict(throughput_windows.get(key))
        rate = float(window.get("cases_per_hour") or 0.0)
        if rate > 0:
            return _format_duration(pending / rate * 3600)
    return "unknown"


def _format_duration(seconds: object) -> str:
    if not isinstance(seconds, int | float) or seconds < 0:
        return "unknown"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h{minutes}m"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m{sec}s"


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for one-shot monitor snapshots.

    Returns:
        Parser with low-memory defaults suitable for heartbeat automation.
    """

    parser = argparse.ArgumentParser(description="Generate a low-memory NorthCUA batch monitor snapshot")
    parser.add_argument("--batch-db", required=True, type=Path)
    parser.add_argument("--controller-db", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--attempt-events", type=Path, default=None)
    parser.add_argument("--prepared-events", type=Path, default=None)
    parser.add_argument("--runtime-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--launch-time", default="")
    parser.add_argument("--tail-mb", type=int, default=32)
    parser.add_argument("--no-processes", action="store_true")
    parser.add_argument(
        "--slow-controller-counts",
        action="store_true",
        help="Also count large unindexed controller tables such as tasks, devices, and commands.",
    )
    parser.add_argument("--markdown", action="store_true")
    return parser


def main() -> None:
    """Run the lightweight monitor CLI and print JSON or Markdown output.

    The command keeps all reads bounded unless the caller explicitly asks for
    slower controller counts or runtime JSONL aggregation.
    """

    args = build_parser().parse_args()
    report = generate_lightweight_batch_monitor_report(
        batch_db=args.batch_db,
        controller_db=args.controller_db,
        run_dir=args.run_dir,
        attempt_events=args.attempt_events,
        prepared_events=args.prepared_events,
        runtime_jsonl=args.runtime_jsonl,
        launch_time=args.launch_time,
        tail_mb=args.tail_mb,
        include_processes=not args.no_processes,
        slow_controller_counts=args.slow_controller_counts,
    )
    if args.markdown:
        print(report["markdown"])
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
