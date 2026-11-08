"""Summarize device command timing from a completed NorthCUA batch output."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


_RESULT_TELEMETRY_KEYS = {
    "action_result_total_ms",
    "backend_action_ms",
    "post_action_wait_ms",
    "post_action_screenshot_total_ms",
    "requested_wait_ms",
    "screenshot_backend_capture_ms",
    "screenshot_save_ms",
    "device_telemetry",
    "tool_stage_timings",
}
_TRANSPORT_FAILURE_EVENTS = {
    "command_result_send_failed",
    "device_ordered_queue_full",
    "device_telemetry_dropped",
}
_CONNECTION_EVENTS = {"connected", "connection_failed", "disconnected"}
_INGRESS_NEGATIVE_CLOCK_SKEW_TOLERANCE_MS = 1_000.0
_REQUIRED_DEVICE_STAGES = (
    "device_command_received",
    "device_command_dequeued",
    "device_command_prepare_started",
    "device_command_prepare_finished",
    "device_driver_call_started",
    "device_driver_call_finished",
    "device_result_json_serialize_started",
    "device_result_json_serialize_finished",
    "device_websocket_send_started",
    "device_websocket_send_finished",
    "command_result_sent",
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def _stats(values: list[float]) -> dict[str, int | float]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "p50": round(_percentile(values, 0.50), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def _open_readonly(path: Path) -> sqlite3.Connection:
    # Do not use immutable=1: completed controller databases can retain a WAL
    # containing committed command and event rows.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _contains_telemetry_key(value: object) -> bool:
    if isinstance(value, dict):
        return bool(_RESULT_TELEMETRY_KEYS.intersection(value)) or any(
            _contains_telemetry_key(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_telemetry_key(item) for item in value)
    return False


def _failure_class(tool_id: str, result: dict[str, Any]) -> str:
    status = str(result.get("status", ""))
    error = str(result.get("error", "")).lower()
    nested = result.get("result")
    returncode = nested.get("returncode") if isinstance(nested, dict) else None
    if status == "timed_out" or "timed out" in error or "timeout" in error:
        return "timeout"
    if "unknown tool" in error or "unknown tool_id" in error:
        return "unknown_tool"
    if tool_id == "shell.exec_readonly" and isinstance(returncode, int):
        return f"shell_nonzero_rc_{returncode}"
    if "denied" in error or "not allowed" in error or "permission" in error:
        return "policy_or_permission"
    return "other_failure"


def summarize(root: Path) -> dict[str, Any]:
    command_statuses: Counter[str] = Counter()
    failed_tools: Counter[str] = Counter()
    failure_classes: Counter[str] = Counter()
    failure_events: Counter[str] = Counter()
    connection_events: Counter[str] = Counter()
    stage_timings: dict[str, list[float]] = defaultdict(list)
    sends: list[dict[str, Any]] = []
    ingress_ms: list[float] = []
    queue_wait_ms: list[float] = []
    queue_depths: list[float] = []
    matched_sends = 0
    unmatched_sends = 0
    unmatched_receives = 0
    duplicate_send_ids = 0
    duplicate_receive_ids = 0
    controller_receives = 0
    telemetry_leaks = 0
    agent_trace_telemetry_leaks = 0
    malformed_runtime_lines = 0
    malformed_agent_trace_lines = 0
    agent_trace_files = 0
    duplicate_command_ids = 0
    missing_controller_results = 0
    unexpected_controller_results = 0
    lifecycle_totals: dict[str, Counter[str]] = {stage: Counter() for stage in _REQUIRED_DEVICE_STAGES}

    databases = sorted(root.rglob("controller.sqlite"))
    for database in databases:
        with _open_readonly(database) as connection:
            received_rows = list(
                connection.execute(
                    "SELECT entity_id, created_at FROM events WHERE event_type = 'command_result_envelope_received'"
                )
            )
            received_counts = Counter(command_id for command_id, _ in received_rows)
            duplicate_receive_ids += sum(count - 1 for count in received_counts.values() if count > 1)
            controller_receives += len(received_rows)
            received = {command_id: datetime.fromisoformat(created_at) for command_id, created_at in received_rows}
            command_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(commands)")}
            protocol_column = "protocol_command_id" if "protocol_command_id" in command_columns else "''"
            expected_command_counts: Counter[str] = Counter()
            for status, protocol_command_id, request_json, result_json in connection.execute(
                f"SELECT status, {protocol_column}, request_json, result_json FROM commands"
            ):
                command_statuses[str(status)] += 1
                request = json.loads(request_json)
                result = json.loads(result_json) if result_json else {}
                protocol_id = str(protocol_command_id or request.get("command_id", ""))
                if protocol_id:
                    expected_command_counts[protocol_id] += 1
                if status != "succeeded":
                    tool_id = str(request.get("tool_id", ""))
                    failed_tools[tool_id] += 1
                    failure_classes[_failure_class(tool_id, result)] += 1
                if _contains_telemetry_key(result):
                    telemetry_leaks += 1
            duplicate_command_ids += sum(count - 1 for count in expected_command_counts.values() if count > 1)
            missing_controller_results += sum(
                max(0, count - received_counts.get(command_id, 0))
                for command_id, count in expected_command_counts.items()
            )
            unexpected_controller_results += sum(
                max(0, count - expected_command_counts.get(command_id, 0))
                for command_id, count in received_counts.items()
            )

        send_counts: Counter[str] = Counter()
        stage_counts: dict[str, Counter[str]] = {stage: Counter() for stage in _REQUIRED_DEVICE_STAGES}
        for runtime_log in database.parent.rglob("*device_runtime.jsonl"):
            with runtime_log.open(encoding="utf-8") as lines:
                for line in lines:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_runtime_lines += 1
                        continue
                    event_name = str(event.get("event", ""))
                    command_id = str(event.get("command_id", ""))
                    if event_name in stage_counts and command_id:
                        stage_counts[event_name][command_id] += 1
                    if event_name in _TRANSPORT_FAILURE_EVENTS:
                        failure_events[event_name] += 1
                    if event_name in _CONNECTION_EVENTS:
                        connection_events[event_name] += 1
                    if event_name == "device_command_received":
                        queue_depths.append(float(event.get("queue_depth_before", 0)))
                    if event_name == "device_command_dequeued":
                        queue_wait_ms.append(float(event.get("queue_wait_ms", 0)))
                    if event_name == "command_result_sent":
                        sends.append(event)
                        send_counts[command_id] += 1
                        if command_id in received:
                            sent_at = datetime.fromisoformat(str(event["timestamp"]))
                            ingress_ms.append((received[command_id] - sent_at).total_seconds() * 1000)
                            matched_sends += 1
                        else:
                            unmatched_sends += 1
                    if event_name == "device_driver_call_finished":
                        for key, value in event.get("tool_stage_timings", {}).items():
                            if key.endswith("_ms") and isinstance(value, int | float):
                                stage_timings[key].append(float(value))
        duplicate_send_ids += sum(count - 1 for count in send_counts.values() if count > 1)
        unmatched_receives += sum(
            max(0, count - send_counts.get(command_id, 0)) for command_id, count in received_counts.items()
        )
        for stage, counts in stage_counts.items():
            totals = lifecycle_totals[stage]
            totals["events"] += sum(counts.values())
            totals["matched_ids"] += sum(
                min(count, expected_command_counts.get(command_id, 0)) for command_id, count in counts.items()
            )
            totals["missing"] += sum(
                max(0, count - counts.get(command_id, 0)) for command_id, count in expected_command_counts.items()
            )
            totals["unexpected"] += sum(
                max(0, count - expected_command_counts.get(command_id, 0)) for command_id, count in counts.items()
            )
            totals["duplicates"] += sum(count - 1 for count in counts.values() if count > 1)

    for trace_path in sorted(path for pattern in ("steps.jsonl", "llm_trace.jsonl") for path in root.rglob(pattern)):
        agent_trace_files += 1
        with trace_path.open(encoding="utf-8") as lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed_agent_trace_lines += 1
                    continue
                if _contains_telemetry_key(event):
                    agent_trace_telemetry_leaks += 1

    return {
        "root": str(root),
        "controller_databases": len(databases),
        "commands": dict(sorted(command_statuses.items())),
        "failed_tools": dict(sorted(failed_tools.items())),
        "failure_classes": dict(sorted(failure_classes.items())),
        "transport_failure_events": dict(sorted(failure_events.items())),
        "connection_events": dict(sorted(connection_events.items())),
        "result_telemetry_leaks": telemetry_leaks,
        "agent_trace_telemetry_leaks": agent_trace_telemetry_leaks,
        "agent_trace_files": agent_trace_files,
        "malformed_runtime_lines": malformed_runtime_lines,
        "malformed_agent_trace_lines": malformed_agent_trace_lines,
        "command_table_coverage": {
            "protocol_commands": sum(command_statuses.values()),
            "duplicate_command_ids": duplicate_command_ids,
            "missing_controller_results": missing_controller_results,
            "unexpected_controller_results": unexpected_controller_results,
        },
        "device_lifecycle_coverage": {
            stage: dict(sorted(counts.items())) for stage, counts in lifecycle_totals.items()
        },
        "result_delivery_coverage": {
            "device_sends": len(sends),
            "controller_receives": controller_receives,
            "matched_sends": matched_sends,
            "unmatched_sends": unmatched_sends,
            "unmatched_receives": unmatched_receives,
            "duplicate_send_ids": duplicate_send_ids,
            "duplicate_receive_ids": duplicate_receive_ids,
        },
        "queue_depth_before": _stats(queue_depths),
        "queue_wait_ms": _stats(queue_wait_ms),
        "send_elapsed_ms": _stats([float(event.get("send_elapsed_ms", 0)) for event in sends]),
        "envelope_bytes": _stats([float(event.get("envelope_bytes", 0)) for event in sends]),
        "send_to_controller_ingress_ms": _stats(ingress_ms),
        "stage_timings": {key: _stats(values) for key, values in sorted(stage_timings.items())},
    }


def strict_violations(payload: dict[str, Any], *, expected_databases: int = 0) -> list[str]:
    violations: list[str] = []
    database_count = int(payload.get("controller_databases", 0))
    if expected_databases and database_count != expected_databases:
        violations.append(f"controller_databases={database_count}, expected={expected_databases}")
    commands = payload.get("commands", {})
    if not isinstance(commands, dict) or sum(int(value) for value in commands.values()) == 0:
        violations.append("no commands found")
    pending = {
        str(status): int(count)
        for status, count in commands.items()
        if status not in {"succeeded", "failed"} and int(count)
    }
    if pending:
        violations.append(f"nonterminal command statuses: {pending}")
    failure_classes = payload.get("failure_classes", {})
    for failure_class in ("timeout", "unknown_tool"):
        if int(failure_classes.get(failure_class, 0)):
            violations.append(f"{failure_class}={failure_classes[failure_class]}")
    for event_name, count in payload.get("transport_failure_events", {}).items():
        if int(count):
            violations.append(f"{event_name}={count}")
    for key in (
        "result_telemetry_leaks",
        "agent_trace_telemetry_leaks",
        "malformed_runtime_lines",
        "malformed_agent_trace_lines",
    ):
        if int(payload.get(key, 0)):
            violations.append(f"{key}={payload[key]}")
    if int(payload.get("agent_trace_files", 0)) == 0:
        violations.append("agent_trace_files=0")
    for key, count in payload.get("command_table_coverage", {}).items():
        if key != "protocol_commands" and int(count):
            violations.append(f"command_table_coverage.{key}={count}")
    for stage, coverage in payload.get("device_lifecycle_coverage", {}).items():
        for key in ("missing", "unexpected", "duplicates"):
            if int(coverage.get(key, 0)):
                violations.append(f"device_lifecycle_coverage.{stage}.{key}={coverage[key]}")
    for key, count in payload.get("result_delivery_coverage", {}).items():
        if key.startswith(("unmatched_", "duplicate_")) and int(count):
            violations.append(f"result_delivery_coverage.{key}={count}")
    queue_max = payload.get("queue_depth_before", {}).get("max", 0)
    if float(queue_max) > 0:
        violations.append(f"queue_depth_before.max={queue_max}")
    ingress_min = payload.get("send_to_controller_ingress_ms", {}).get("min", 0)
    if float(ingress_min) < -_INGRESS_NEGATIVE_CLOCK_SKEW_TOLERANCE_MS:
        violations.append(
            "send_to_controller_ingress_ms.min="
            f"{ingress_min}, tolerance_ms={_INGRESS_NEGATIVE_CLOCK_SKEW_TOLERANCE_MS:g}"
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_output", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--expected-databases", type=int, default=0)
    args = parser.parse_args()
    payload = summarize(args.batch_output.expanduser().resolve())
    violations = strict_violations(payload, expected_databases=args.expected_databases)
    payload["strict_violations"] = violations
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.expanduser().write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if args.strict and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
