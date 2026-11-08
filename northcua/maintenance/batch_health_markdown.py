"""Markdown rendering helpers for batch health reports."""

from __future__ import annotations

JsonDict = dict[str, object]


def render_markdown(report: JsonDict) -> str:
    """Render a compact Markdown report from the JSON payload.

    Args:
        report: Report returned by ``generate_batch_health_report``.

    Returns:
        Human-readable Markdown summary for operators.
    """

    batch = report.get("batch", {})
    prepared = report.get("prepared_sandboxes", {})
    controller = report.get("controller", {})
    worker_logs = report.get("worker_logs", {})
    throughput = batch.get("throughput_since_restart", {}) if isinstance(batch, dict) else {}
    recent = batch.get("recent_window", {}) if isinstance(batch, dict) else {}
    eta = batch.get("eta", {}) if isinstance(batch, dict) else {}
    lines = [
        f"# Batch Health {report.get('generated_at', '')}",
        "",
        f"- batch counts: {_fmt_counts(batch.get('status_counts', {}) if isinstance(batch, dict) else {})}",
        (
            f"- active: {_dict_get(batch, 'active_total')}; terminal: {_dict_get(batch, 'terminal_total')}; "
            f"success: {_dict_get(batch, 'task_success_total', _dict_get(batch, 'success_total'))}; "
            f"accepted_infeasible: {_dict_get(batch, 'accepted_infeasible_total')}; "
            f"actionable_failed: {_dict_get(batch, 'actionable_failed_total', _status_failed_count(batch))}"
        ),
        f"- since restart: terminal +{_dict_get(throughput, 'terminal_delta')}, success +{_dict_get(throughput, 'success_delta')}, terminal/h {_dict_get(throughput, 'terminal_per_hour')}",
        f"- recent {report.get('recent_window_minutes', 0)}m: terminal +{_dict_get(recent, 'terminal_delta')}, success +{_dict_get(recent, 'success_delta')}",
        f"- ETA: {_fmt_eta(eta)}",
        (
            f"- prepared: {_fmt_counts(_dict_get(prepared, 'status_counts', {}))}; "
            f"active unassigned={_dict_get(prepared, 'active_unassigned_total')}"
        ),
        (
            f"- prepared detail: assigned_current={_dict_get(prepared, 'assigned_to_current_active_total')}, "
            f"stale_assigned={_dict_get(prepared, 'stale_assigned_total')}, "
            f"expired_active={_dict_get(prepared, 'expired_but_active_total')}, "
            f"prepare_seconds={_fmt_distribution(_dict_get(prepared, 'prepare_seconds_distribution', {}))}, "
            f"ages={_fmt_latency_summary(_dict_get(prepared, 'age_seconds_by_status', {}))}"
        ),
        f"- controller tasks: {_fmt_counts(_dict_get(controller, 'task_status_counts', {}))}",
        f"- controller devices: {_fmt_counts(_dict_get(controller, 'device_status_counts', {}))}",
        f"- commands: {_fmt_counts(_dict_get(controller, 'command_status_counts', {}))}",
        f"- command latency recent: {_fmt_latency_summary(_nested(controller, 'command_metrics', 'recent_window_by_tool_status'))}",
        (
            f"- command problems: failed/timed_out={len(_nested(controller, 'command_metrics', 'problem_commands', fallback=[]))}, "
            f"non-evaluator shell.exec={_nested(controller, 'command_metrics', 'non_evaluator_shell_exec_total', fallback=0)}"
        ),
        (
            f"- controller loop lag: "
            f"all={_fmt_ms_distribution(_nested(controller, 'runtime_event_metrics', 'event_loop_lag_ms'))}, "
            f"recent={_fmt_ms_distribution(_nested(controller, 'runtime_event_metrics', 'recent_event_loop_lag_ms'))}; "
            f"recv_q_max={_fmt_ms_distribution(_nested(controller, 'runtime_event_metrics', 'recent_recv_q_max'))}"
        ),
        (
            f"- websocket handshake: accepted={_nested(controller, 'websocket_handshake_metrics', 'accepted_total', fallback=0)}, "
            f"open={_fmt_ms_distribution(_nested(controller, 'websocket_handshake_metrics', 'open_handshake_ms'))}, "
            f"recent_open={_fmt_ms_distribution(_nested(controller, 'websocket_handshake_metrics', 'recent_open_handshake_ms'))}, "
            f"first_envelope={_fmt_ms_distribution(_nested(controller, 'websocket_handshake_metrics', 'recent_first_envelope_ms'))}"
        ),
        (
            f"- readiness stages: counts={_fmt_counts(_nested(controller, 'readiness_stage_metrics', 'stage_counts'))}; "
            f"recent={_fmt_stage_summary(_nested(controller, 'readiness_stage_metrics', 'recent_by_stage'))}"
        ),
        (
            f"- readiness timeout classes: "
            f"all={_fmt_counts(_nested(controller, 'readiness_stage_metrics', 'timeout_classification_counts'))}; "
            f"recent={_fmt_counts(_nested(controller, 'readiness_stage_metrics', 'recent_timeout_classification_counts'))}"
        ),
        (
            f"- worker logs: since_restart={_fmt_worker_window(_dict_get(worker_logs, 'since_restart', {}))}; "
            f"recent={_fmt_worker_window(_dict_get(worker_logs, 'recent_window', {}))}; "
            f"samples={_fmt_worker_samples(_dict_get(worker_logs, 'recent_window', {}))}"
        ),
        (
            f"- process executor: {_fmt_counts(_nested(controller, 'process_executor_metrics', 'metric_counts'))}; "
            f"logical={_fmt_counts(_nested(controller, 'process_executor_metrics', 'logical_metric_counts'))}; "
            f"command_count={_nested(controller, 'process_executor_metrics', 'worker_command_roundtrip_command_count', fallback=0)}; "
            f"connect={_fmt_ms_distribution(_nested(controller, 'process_executor_metrics', 'worker_connect_ms'))}; "
            f"ready={_fmt_ms_distribution(_nested(controller, 'process_executor_metrics', 'worker_ready_ms'))}; "
            f"command={_fmt_ms_distribution(_nested(controller, 'process_executor_metrics', 'worker_command_roundtrip_ms'))}; "
            f"workers={_fmt_counts(_nested(controller, 'worker_job_health', 'status_counts'))}"
        ),
        f"- disabled window tools since restart: {_dict_get(controller, 'disabled_window_tool_count_since_restart')}",
        f"- telemetry event health: {_fmt_counts(_dict_get(controller, 'telemetry_event_health_since_restart', {}))}",
        f"- live membership: {_fmt_counts(_dict_get(controller, 'live_membership', {}))}",
        f"- device-ready failures: {_fmt_counts(_nested(report, 'attempt_events', 'device_ready_failure_windows'))}",
    ]
    warnings = report.get("warnings") or []
    if warnings:
        lines.append(f"- warnings: {'; '.join(str(item) for item in warnings)}")
    return "\n".join(lines) + "\n"


def _nested(value: object, outer: str, inner: str, *, fallback: object = {}) -> object:
    if not isinstance(value, dict):
        return fallback
    nested = value.get(outer, {})
    if not isinstance(nested, dict):
        return fallback
    return nested.get(inner, fallback)


def _dict_get(value: object, key: str, fallback: object = 0) -> object:
    return value.get(key, fallback) if isinstance(value, dict) else fallback


def _status_failed_count(value: object) -> object:
    status_counts = _dict_get(value, "status_counts", {})
    return _dict_get(status_counts, "failed") if isinstance(status_counts, dict) else 0


def _fmt_worker_window(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    files = _dict_get(value, "files")
    classes = _fmt_counts(_dict_get(value, "classification_counts", {}))
    errors = _fmt_counts(_dict_get(value, "connection_error_counts", {}))
    path_kinds = _fmt_counts(_dict_get(value, "path_kind_counts", {}))
    return f"files={files}, classes={classes}, errors={errors}, paths={path_kinds}"


def _fmt_worker_samples(value: object, *, limit: int = 3) -> str:
    if not isinstance(value, dict):
        return "{}"
    samples = value.get("samples")
    if not isinstance(samples, list) or not samples:
        return "{}"
    rendered = []
    for sample in samples[:limit]:
        if not isinstance(sample, dict):
            continue
        classification = sample.get("classification", "")
        path_kind = sample.get("path_kind", "")
        path = _truncate(str(sample.get("relative_path", "")), 80)
        error = _truncate(str(sample.get("first_error", "")), 120)
        if error:
            rendered.append(f"{classification}/{path_kind}:{path}:{error}")
        else:
            rendered.append(f"{classification}/{path_kind}:{path}")
    if len(samples) > limit:
        rendered.append(f"...+{len(samples) - limit}")
    return "; ".join(rendered) or "{}"


def _fmt_counts(value: object) -> str:
    if not isinstance(value, dict):
        return "{}"
    return ", ".join(f"{key}={item}" for key, item in sorted(value.items())) or "{}"


def _fmt_eta(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    remaining = _dict_get(value, "remaining_total")
    basis = str(_dict_get(value, "basis", "unavailable"))
    if basis == "complete":
        return f"remaining={remaining}, complete"
    eta_at = str(_dict_get(value, "eta_at", ""))
    eta_hours = _dict_get(value, "eta_hours", None)
    recent_rate = _dict_get(value, "recent_terminal_per_hour")
    since_rate = _dict_get(value, "since_restart_terminal_per_hour")
    parts = [
        f"remaining={remaining}",
        f"basis={basis}",
        f"recent_terminal/h={recent_rate}",
        f"since_restart_terminal/h={since_rate}",
    ]
    if eta_at:
        parts.append(f"eta_at={eta_at}")
        parts.append(f"eta_hours={eta_hours}")
    else:
        parts.append("eta_at=unavailable")
    recent_eta_at = str(_dict_get(value, "recent_eta_at", ""))
    since_eta_at = str(_dict_get(value, "since_restart_eta_at", ""))
    if recent_eta_at and basis != "recent_window":
        parts.append(f"recent_eta_at={recent_eta_at}")
    if since_eta_at and basis != "since_restart":
        parts.append(f"since_restart_eta_at={since_eta_at}")
    return ", ".join(parts)


def _fmt_distribution(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    count = value.get("count", 0)
    if not count:
        return "{}"
    return (
        f"n={count} p50={value.get('p50', 0)}s p90={value.get('p90', 0)}s "
        f"p95={value.get('p95', 0)}s max={value.get('max', 0)}s"
    )


def _fmt_ms_distribution(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    count = value.get("count", 0)
    if not count:
        return "{}"
    return (
        f"n={count} p50={value.get('p50', 0)}ms p90={value.get('p90', 0)}ms "
        f"p95={value.get('p95', 0)}ms max={value.get('max', 0)}ms"
    )


def _fmt_latency_summary(value: object, *, limit: int = 6) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    items: list[tuple[str, JsonDict]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            items.append((str(key), item))
    items.sort(key=lambda item: (_float(item[1].get("p95")), _float(item[1].get("max"))), reverse=True)
    rendered = [f"{key}({_fmt_distribution(item)})" for key, item in items[:limit]]
    if len(items) > limit:
        rendered.append(f"...+{len(items) - limit}")
    return "; ".join(rendered) or "{}"


def _fmt_stage_summary(value: object, *, limit: int = 8) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    items: list[tuple[str, JsonDict]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            items.append((str(key), item))
    items.sort(
        key=lambda item: (
            _float(item[1].get("recent", {}).get("p95") if isinstance(item[1].get("recent"), dict) else 0),
            _float(item[1].get("recent", {}).get("max") if isinstance(item[1].get("recent"), dict) else 0),
            _float(item[1].get("count")),
        ),
        reverse=True,
    )
    rendered = []
    for key, item in items[:limit]:
        distribution = item.get("recent") if isinstance(item.get("recent"), dict) else item.get("all")
        rendered.append(
            f"{key}({_fmt_ms_distribution(distribution) if distribution else 'n=' + str(item.get('count', 0))})"
        )
    if len(items) > limit:
        rendered.append(f"...+{len(items) - limit}")
    return "; ".join(rendered) or "{}"


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)] + "..."
