"""Controller event metric readers used by batch health reports."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict

JsonDict = dict[str, object]


def runtime_event_metrics(
    connection: sqlite3.Connection,
    *,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    """Summarize controller runtime telemetry events.

    Args:
        connection: Controller SQLite connection.
        restart_ts: Optional restart cutoff epoch; ``0`` includes all rows.
        recent_window_minutes: Window used for recent latency projections.

    Returns:
        JSON-safe runtime telemetry summary.
    """

    if not _table_exists(connection, "events"):
        return {}
    recent_cutoff = time.time() - recent_window_minutes * 60
    rows = _query_rows_since_restart(
        connection,
        """
            SELECT payload_json, created_at
            FROM events
            WHERE event_type='controller_runtime_telemetry'
            """,
        restart_ts=restart_ts,
    )
    event_loop_lag_ms: list[float] = []
    recent_event_loop_lag_ms: list[float] = []
    recv_q_max: list[float] = []
    recent_recv_q_max: list[float] = []
    recv_q_p95: list[float] = []
    socket_count: list[float] = []
    latest: JsonDict = {}
    for row in rows:
        payload = _loads(str(row["payload_json"] or "{}"))
        latest = payload
        created_at = _parse_time_to_epoch(str(row["created_at"] or ""))
        lag = _float(payload.get("event_loop_lag_ms"))
        event_loop_lag_ms.append(lag)
        recv_max = _float(payload.get("recv_q_max"))
        recv_p95 = _float(payload.get("recv_q_p95"))
        sockets = _float(payload.get("socket_count"))
        recv_q_max.append(recv_max)
        recv_q_p95.append(recv_p95)
        socket_count.append(sockets)
        if created_at >= recent_cutoff:
            recent_event_loop_lag_ms.append(lag)
            recent_recv_q_max.append(recv_max)
    return {
        "sample_count": len(rows),
        "event_loop_lag_ms": _value_distribution(event_loop_lag_ms),
        "recent_event_loop_lag_ms": _value_distribution(recent_event_loop_lag_ms),
        "recv_q_max": _value_distribution(recv_q_max),
        "recent_recv_q_max": _value_distribution(recent_recv_q_max),
        "recv_q_p95": _value_distribution(recv_q_p95),
        "socket_count": _value_distribution(socket_count),
        "latest": latest,
    }


def websocket_handshake_metrics(
    connection: sqlite3.Connection,
    *,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    """Summarize controller websocket handshake timing events.

    Args:
        connection: Controller SQLite connection.
        restart_ts: Optional restart cutoff epoch; ``0`` includes all rows.
        recent_window_minutes: Window used for recent latency projections.

    Returns:
        JSON-safe accepted/rejected websocket timing summary.
    """

    if not _table_exists(connection, "events"):
        return {}
    recent_cutoff = time.time() - recent_window_minutes * 60
    accepted_rows = _query_rows_since_restart(
        connection,
        """
            SELECT entity_id, payload_json, created_at
            FROM events
            WHERE event_type='websocket_handler_started'
            """,
        restart_ts=restart_ts,
    )
    first_envelope_rows = _query_rows_since_restart(
        connection,
        """
            SELECT entity_id, payload_json, created_at
            FROM events
            WHERE event_type='websocket_first_envelope_received'
            """,
        restart_ts=restart_ts,
    )
    rejected_rows = _query_rows_since_restart(
        connection,
        """
            SELECT entity_id, payload_json, created_at
            FROM events
            WHERE event_type='websocket_handshake_rejected'
            """,
        restart_ts=restart_ts,
    )
    open_ms: list[float] = []
    handler_start_delay_ms: list[float] = []
    recent_open_ms: list[float] = []
    recent_handler_start_delay_ms: list[float] = []
    status_counts: Counter[str] = Counter()
    slow_samples: list[tuple[float, JsonDict]] = []
    for row in accepted_rows:
        payload = _loads(str(row["payload_json"] or "{}"))
        created_at = _parse_time_to_epoch(str(row["created_at"] or ""))
        open_value = _float(payload.get("websocket_open_handshake_ms"))
        delay_value = _float(payload.get("websocket_handler_start_delay_ms"))
        if open_value >= 0:
            open_ms.append(open_value)
            slow_samples.append((open_value, _websocket_handshake_sample(row, payload, open_value)))
        if delay_value >= 0:
            handler_start_delay_ms.append(delay_value)
        if created_at >= recent_cutoff:
            if open_value >= 0:
                recent_open_ms.append(open_value)
            if delay_value >= 0:
                recent_handler_start_delay_ms.append(delay_value)
        status_counts[str(int(_float(payload.get("websocket_response_status_code"))))] += 1
    first_envelope_ms: list[float] = []
    recent_first_envelope_ms: list[float] = []
    first_envelope_type_counts: Counter[str] = Counter()
    for row in first_envelope_rows:
        payload = _loads(str(row["payload_json"] or "{}"))
        created_at = _parse_time_to_epoch(str(row["created_at"] or ""))
        value = _float(payload.get("first_envelope_ms"))
        if value >= 0:
            first_envelope_ms.append(value)
            if created_at >= recent_cutoff:
                recent_first_envelope_ms.append(value)
        message_type = str(payload.get("message_type") or "")
        if message_type:
            first_envelope_type_counts[message_type] += 1
    rejected_status_counts: Counter[str] = Counter()
    for row in rejected_rows:
        payload = _loads(str(row["payload_json"] or "{}"))
        rejected_status_counts[str(int(_float(payload.get("websocket_response_status_code"))))] += 1
    slow_samples.sort(key=lambda item: item[0], reverse=True)
    return {
        "accepted_total": len(accepted_rows),
        "accepted_status_counts": dict(status_counts),
        "open_handshake_ms": _value_distribution(open_ms),
        "recent_open_handshake_ms": _value_distribution(recent_open_ms),
        "handler_start_delay_ms": _value_distribution(handler_start_delay_ms),
        "recent_handler_start_delay_ms": _value_distribution(recent_handler_start_delay_ms),
        "first_envelope_ms": _value_distribution(first_envelope_ms),
        "recent_first_envelope_ms": _value_distribution(recent_first_envelope_ms),
        "first_envelope_type_counts": dict(first_envelope_type_counts.most_common(20)),
        "rejected_total": len(rejected_rows),
        "rejected_status_counts": dict(rejected_status_counts),
        "top_slow_accepted": [sample for _, sample in slow_samples[:8]],
    }


def process_executor_metrics(
    connection: sqlite3.Connection,
    *,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    """Summarize process-executor metric events.

    Args:
        connection: Controller SQLite connection.
        restart_ts: Optional restart cutoff epoch; ``0`` includes all rows.
        recent_window_minutes: Window used for recent latency projections.

    Returns:
        JSON-safe worker startup and IPC roundtrip summary.
    """

    if not _table_exists(connection, "events"):
        return {}
    recent_cutoff = time.time() - recent_window_minutes * 60
    rows = _query_rows_since_restart(
        connection,
        """
            SELECT payload_json, created_at
            FROM events
            WHERE event_type='process_executor_metric'
            """,
        restart_ts=restart_ts,
    )
    values_by_name: defaultdict[str, list[float]] = defaultdict(list)
    recent_values_by_name: defaultdict[str, list[float]] = defaultdict(list)
    counts: Counter[str] = Counter()
    logical_counts: Counter[str] = Counter()
    for row in rows:
        payload = _loads(str(row["payload_json"] or "{}"))
        metric_name = str(payload.get("metric_name") or "")
        if not metric_name:
            continue
        counts[metric_name] += 1
        value = _float(payload.get("value_ms"))
        if value < 0:
            continue
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        logical_count = max(1, _int(metadata.get("count")))
        logical_counts[metric_name] += logical_count
        values_by_name[metric_name].append(value)
        created_at = _parse_time_to_epoch(str(row["created_at"] or ""))
        if created_at >= recent_cutoff:
            recent_values_by_name[metric_name].append(value)
    command_values = values_by_name.get("worker_command_roundtrip", [])
    if not command_values:
        command_values = values_by_name.get("ipc_command_roundtrip", [])
    recent_command_values = recent_values_by_name.get("worker_command_roundtrip", [])
    if not recent_command_values:
        recent_command_values = recent_values_by_name.get("ipc_command_roundtrip", [])
    worker_connect_values = values_by_name.get("worker_connect", []) or values_by_name.get("worker_startup", [])
    recent_worker_connect_values = recent_values_by_name.get("worker_connect", []) or recent_values_by_name.get(
        "worker_startup", []
    )
    worker_ready_values = values_by_name.get("worker_ready", [])
    recent_worker_ready_values = recent_values_by_name.get("worker_ready", [])
    return {
        "sample_count": len(rows),
        "metric_counts": dict(counts),
        "logical_metric_counts": dict(logical_counts),
        "worker_command_roundtrip_event_count": counts.get("worker_command_roundtrip", 0)
        or counts.get("ipc_command_roundtrip", 0),
        "worker_command_roundtrip_command_count": logical_counts.get("worker_command_roundtrip", 0)
        or counts.get("ipc_command_roundtrip", 0),
        "worker_connect_ms": _value_distribution(worker_connect_values),
        "recent_worker_connect_ms": _value_distribution(recent_worker_connect_values),
        "worker_ready_ms": _value_distribution(worker_ready_values),
        "recent_worker_ready_ms": _value_distribution(recent_worker_ready_values),
        "worker_startup_ms": _value_distribution(worker_connect_values),
        "recent_worker_startup_ms": _value_distribution(recent_worker_connect_values),
        "worker_command_roundtrip_ms": _value_distribution(command_values),
        "recent_worker_command_roundtrip_ms": _value_distribution(recent_command_values),
        "ipc_command_roundtrip_ms": _value_distribution(command_values),
        "recent_ipc_command_roundtrip_ms": _value_distribution(recent_command_values),
    }


def readiness_stage_metrics(
    connection: sqlite3.Connection,
    *,
    restart_ts: float,
    recent_window_minutes: int,
) -> JsonDict:
    """Summarize prepared/device readiness stage timing telemetry.

    Args:
        connection: Controller SQLite connection.
        restart_ts: Controller process restart timestamp; older rows are ignored.
        recent_window_minutes: Window used for recent metric distributions.

    Returns:
        Readiness stage timing summary keyed by stage.
    """

    recent_cutoff = time.time() - recent_window_minutes * 60
    samples: list[tuple[float, str, JsonDict]] = []
    by_stage: defaultdict[str, list[JsonDict]] = defaultdict(list)
    recent_by_stage: defaultdict[str, list[JsonDict]] = defaultdict(list)
    if _table_exists(connection, "events"):
        for row in _query_rows_since_restart(
            connection,
            """
                SELECT entity_id, event_type, payload_json, created_at
                FROM events
                WHERE event_type='device_readiness_stage'
                """,
            restart_ts=restart_ts,
        ):
            payload = _loads(str(row["payload_json"] or "{}"))
            payload["entity_id"] = str(row["entity_id"] or "")
            payload["source_table"] = "events"
            _collect_readiness_stage_sample(
                row,
                payload,
                by_stage=by_stage,
                recent_by_stage=recent_by_stage,
                samples=samples,
                recent_cutoff=recent_cutoff,
            )
    if _table_exists(connection, "telemetry_events"):
        for row in _query_rows_since_restart(
            connection,
            """
                SELECT case_id, entity_id, payload_json, created_at
                FROM telemetry_events
                """,
            restart_ts=restart_ts,
        ):
            raw_payload = _loads(str(row["payload_json"] or "{}"))
            message = str(raw_payload.get("message") or "")
            if message not in {"sandbox_stage_timing", "prepared_sandbox_stage_timing"}:
                continue
            raw_details = raw_payload.get("details")
            if not isinstance(raw_details, dict):
                continue
            payload = dict(raw_details)
            payload["case_id"] = str(row["case_id"] or "")
            payload["entity_id"] = str(row["entity_id"] or "")
            payload["message"] = message
            payload["source_table"] = "telemetry_events"
            _collect_readiness_stage_sample(
                row,
                payload,
                by_stage=by_stage,
                recent_by_stage=recent_by_stage,
                samples=samples,
                recent_cutoff=recent_cutoff,
            )
    samples.sort(key=lambda item: item[0], reverse=True)
    timeout_classifications = _readiness_timeout_classifications(by_stage)
    recent_timeout_classifications = _readiness_timeout_classifications(
        by_stage,
        wait_rows=recent_by_stage.get("device_readiness_wait", []),
    )
    return {
        "stage_counts": {stage: len(values) for stage, values in sorted(by_stage.items())},
        "by_stage": {stage: _stage_bucket(values) for stage, values in sorted(by_stage.items())},
        "recent_by_stage": {stage: _stage_bucket(values) for stage, values in sorted(recent_by_stage.items())},
        "timeout_classification_counts": timeout_classifications["counts"],
        "recent_timeout_classification_counts": recent_timeout_classifications["counts"],
        "timeout_classification_samples": timeout_classifications["samples"][:12],
        "top_slow": [sample for _, _, sample in samples[:12]],
    }


def _collect_readiness_stage_sample(
    row: sqlite3.Row,
    payload: JsonDict,
    *,
    by_stage: defaultdict[str, list[JsonDict]],
    recent_by_stage: defaultdict[str, list[JsonDict]],
    samples: list[tuple[float, str, JsonDict]],
    recent_cutoff: float,
) -> None:
    stage = str(payload.get("stage") or "")
    if not stage:
        return
    created_at_raw = str(row["created_at"] or "")
    created_at = _parse_time_to_epoch(created_at_raw)
    payload["created_at"] = created_at_raw
    by_stage[stage].append(payload)
    if created_at >= recent_cutoff:
        recent_by_stage[stage].append(payload)
    score = max((_float(payload.get(field)) for field in _READINESS_STAGE_NUMERIC_FIELDS), default=-1.0)
    if score >= 0:
        samples.append((score, stage, _stage_sample(stage, payload, score)))


_READINESS_STAGE_NUMERIC_FIELDS = (
    "duration_ms",
    "db_commit_ms",
    "heartbeat_db_commit_ms",
    "websocket_open_handshake_ms",
    "websocket_handler_start_delay_ms",
    "first_envelope_ms",
)


def _stage_bucket(values: list[JsonDict]) -> JsonDict:
    status_counts = Counter(str(value.get("status") or "unknown") for value in values)
    result: JsonDict = {"count": len(values), "status_counts": dict(status_counts)}
    for field in _READINESS_STAGE_NUMERIC_FIELDS:
        field_values = [_float(value.get(field)) for value in values if _float(value.get(field)) >= 0]
        if field_values:
            result[field] = _value_distribution(field_values)
    return result


def _stage_sample(stage: str, payload: JsonDict, score: float) -> JsonDict:
    return {
        "stage": stage,
        "score_ms": round(score, 3),
        "created_at": str(payload.get("created_at") or ""),
        "case_id": str(payload.get("case_id") or ""),
        "entity_id": str(payload.get("entity_id") or ""),
        "device_id": str(payload.get("device_id") or ""),
        "sandbox_id": str(payload.get("sandbox_id") or ""),
        "status": str(payload.get("status") or ""),
        "message": str(payload.get("message") or ""),
        "duration_ms": _int(payload.get("duration_ms")),
        "db_commit_ms": _int(payload.get("db_commit_ms")),
        "heartbeat_db_commit_ms": _int(payload.get("heartbeat_db_commit_ms")),
        "websocket_open_handshake_ms": _int(payload.get("websocket_open_handshake_ms")),
        "first_envelope_ms": _int(payload.get("first_envelope_ms")),
        "source_table": str(payload.get("source_table") or ""),
    }


def _readiness_timeout_classifications(
    by_stage: dict[str, list[JsonDict]],
    *,
    wait_rows: list[JsonDict] | None = None,
) -> JsonDict:
    source_wait_rows = by_stage.get("device_readiness_wait", []) if wait_rows is None else wait_rows
    failed_wait_rows = [payload for payload in source_wait_rows if _is_failed_readiness_wait(payload)]
    stage_times_by_device: defaultdict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for stage in ("websocket_open", "first_heartbeat", "capability_ready"):
        for payload in by_stage.get(stage, []):
            device_id = str(payload.get("device_id") or payload.get("entity_id") or "")
            if not device_id:
                continue
            stage_times_by_device[device_id][stage].append(_parse_time_to_epoch(str(payload.get("created_at") or "")))
    counts: Counter[str] = Counter()
    samples: list[JsonDict] = []
    for payload in failed_wait_rows:
        device_id = str(payload.get("device_id") or payload.get("entity_id") or "")
        wait_at = _parse_time_to_epoch(str(payload.get("created_at") or ""))
        stages = stage_times_by_device.get(device_id, {})
        classification = _classify_readiness_timeout(stages=stages, wait_at=wait_at)
        counts[classification] += 1
        samples.append(
            {
                "classification": classification,
                "created_at": str(payload.get("created_at") or ""),
                "case_id": str(payload.get("case_id") or ""),
                "entity_id": str(payload.get("entity_id") or ""),
                "device_id": device_id,
                "sandbox_id": str(payload.get("sandbox_id") or ""),
                "readiness": str(payload.get("readiness") or ""),
                "last_probe_status": str(payload.get("last_probe_status") or ""),
                "last_probe_error": str(payload.get("last_probe_error") or ""),
                "duration_ms": _int(payload.get("duration_ms")),
            }
        )
    return {"counts": dict(counts.most_common()), "samples": samples}


def _is_failed_readiness_wait(payload: JsonDict) -> bool:
    readiness = str(payload.get("readiness") or "").lower()
    status = str(payload.get("status") or "").lower()
    return status == "failed" or readiness in {"timed_out", "failed", "timeout"}


def _classify_readiness_timeout(*, stages: dict[str, list[float]], wait_at: float) -> str:
    if not _stage_observed_before_wait(stages, "websocket_open", wait_at=wait_at):
        return "opening_handshake_timeout"
    if not _stage_observed_before_wait(stages, "first_heartbeat", wait_at=wait_at):
        return "first_heartbeat_timeout"
    if not _stage_observed_before_wait(stages, "capability_ready", wait_at=wait_at):
        return "capability_ready_timeout"
    return "device_ready_timeout_after_capability_ready"


def _stage_observed_before_wait(stages: dict[str, list[float]], stage: str, *, wait_at: float) -> bool:
    values = stages.get(stage) or []
    if wait_at <= 0:
        return bool(values)
    return any(value <= wait_at + 5.0 for value in values if value > 0)


def _query_rows_since_restart(
    connection: sqlite3.Connection,
    query: str,
    *,
    restart_ts: float,
) -> list[sqlite3.Row]:
    stripped_query = query.rstrip()
    if restart_ts > 0 and "created_at" in stripped_query:
        connector = " AND " if " WHERE " in stripped_query.upper() else " WHERE "
        # Controller DBs in tests and older tools may store ``created_at`` as an
        # epoch string, while production rows use ISO timestamps. Push both
        # predicates into SQLite so the event_type/created_at index can trim the
        # scan, then keep the Python parser as a correctness guard.
        rows = list(
            connection.execute(
                f"{stripped_query}{connector}(created_at >= ? OR CAST(created_at AS REAL) >= ?)",
                (_epoch_to_sqlite_ts(restart_ts), restart_ts),
            ).fetchall()
        )
        return [row for row in rows if _parse_time_to_epoch(str(row["created_at"] or "")) >= restart_ts]
    rows = list(connection.execute(query).fetchall())
    if restart_ts <= 0:
        return rows
    return [row for row in rows if _parse_time_to_epoch(str(row["created_at"] or "")) >= restart_ts]


def _epoch_to_sqlite_ts(value: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _websocket_handshake_sample(row: sqlite3.Row, payload: JsonDict, duration_ms: float) -> JsonDict:
    return {
        "connection_id": str(row["entity_id"] or ""),
        "created_at": row["created_at"] or "",
        "duration_ms": round(duration_ms, 3),
        "handler_start_delay_ms": _int(payload.get("websocket_handler_start_delay_ms")),
        "response_status_code": _int(payload.get("websocket_response_status_code")),
        "remote_address": _shorten(str(payload.get("remote_address") or ""), 120),
        "path": _shorten(str(payload.get("path") or ""), 120),
    }


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _loads(raw: str) -> JsonDict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_time_to_epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _value_distribution(values: list[float]) -> JsonDict:
    filtered = sorted(value for value in values if value >= 0)
    if not filtered:
        return {"count": 0}
    return {
        "count": len(filtered),
        "min": round(filtered[0], 3),
        "p50": round(_percentile(filtered, 50), 3),
        "p95": round(_percentile(filtered, 95), 3),
        "max": round(filtered[-1], 3),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = int(round((percentile / 100.0) * (len(values) - 1)))
    return values[min(max(index, 0), len(values) - 1)]


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return -1.0


def _int(value: object) -> int:
    numeric = _float(value)
    return int(numeric) if numeric >= 0 else 0


def _shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
