"""Operator-facing controller CLI projections and printers."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from northcua_protocol.state import TaskState
from northcua_runtime_shared.redaction import redact_for_log as _shared_redact_for_log

from .shared_args import resolve_app_config_path

if TYPE_CHECKING:
    from northcua.controller.api.server import ControllerServer
    from northcua.controller.config import ControllerConfig
    from northcua.controller.execution import ControllerRunnerRegistrationContext, RunnerRegistry

logger = logging.getLogger(__name__)

_TERMINAL_TASK_STATUSES = {
    TaskState.FINISHED.value,
    TaskState.FAILED.value,
    TaskState.INTERRUPTED.value,
}


def print_controller_rows(*, server: ControllerServer, args: argparse.Namespace) -> int:
    """Print devices, sessions, or tasks in table or JSON form.

    Args:
        server: Controller server whose store provides row data.
        args: Parsed CLI namespace for the selected list command.

    Returns:
        Process-style exit code.
    """

    if args.controller_command == "devices":
        rows = server.store.device_rows()
        if args.json:
            print(json.dumps([_device_row_json(dict(row)) for row in rows], ensure_ascii=False))
            return 0
        for row in rows:
            print(f"{row['device_id']}\t{row['status']}\tactive_sessions={row['active_sessions']}")
        return 0
    if args.controller_command == "sessions":
        rows = server.store.session_rows()
        if args.json:
            print(json.dumps([_session_row_json(dict(row)) for row in rows], ensure_ascii=False))
            return 0
        for row in rows:
            print(f"{row['session_id']}\t{row['status']}\tdevice={row['device_id'] or ''}")
        return 0

    rows = server.store.task_rows()
    if args.json:
        print(json.dumps([_task_row_json(dict(row)) for row in rows], ensure_ascii=False))
        return 0
    for row in rows:
        print(f"{row['task_id']}\t{row['status']}\ttrace={row['trace_id']}")
    return 0


async def print_controller_task_detail(*, server: ControllerServer, args: argparse.Namespace) -> int:
    """Print one task detail, optionally watching until terminal state.

    Args:
        server: Controller server whose store provides task detail.
        args: Parsed CLI namespace containing task id and watch options.

    Returns:
        Process-style exit code.
    """

    detail = await _load_task_detail(
        server,
        args.task_id,
        watch=args.watch,
        poll_interval=args.poll_interval,
        emit_status=not args.json,
    )
    if detail is None:
        print(f"task not found: {args.task_id}")
        return 1
    if args.json:
        print(json.dumps(detail, ensure_ascii=False))
    else:
        _print_task_detail(detail)
    return 0


def print_controller_events(*, server: ControllerServer, args: argparse.Namespace) -> int:
    """Print controller events in table or JSON form.

    Args:
        server: Controller server whose store provides event rows.
        args: Parsed CLI namespace containing filters and output mode.

    Returns:
        Process-style exit code.
    """

    rows = [row for row in server.store.event_rows() if not args.trace_id or row["trace_id"] == args.trace_id]
    if args.json:
        print(json.dumps([_event_row_json(dict(row)) for row in rows], ensure_ascii=False))
        return 0
    for row in rows:
        print(f"{row['event_id']}\t{row['trace_id']}\t{row['entity_type']}:{row['entity_id']}\t{row['event_type']}")
    return 0


def print_controller_telemetry(*, server: ControllerServer, args: argparse.Namespace) -> int:
    """Print controller telemetry events and case snapshots.

    Args:
        server: Controller server whose store provides telemetry rows.
        args: Parsed CLI namespace containing telemetry filters.

    Returns:
        Process-style exit code.
    """

    payload = {
        "events": [
            _telemetry_event_row_json(dict(row))
            for row in server.store.telemetry_event_rows(
                experiment_id=args.experiment_id,
                batch_id=args.batch_id,
                case_id=args.case_id,
                since_event_id=args.since_event_id,
            )
        ],
        "case_snapshots": [
            _telemetry_snapshot_row_json(dict(row))
            for row in server.store.case_snapshot_rows(
                experiment_id=args.experiment_id,
                batch_id=args.batch_id,
            )
            if not args.case_id or row["case_id"] == args.case_id
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    for snapshot in payload["case_snapshots"]:
        print(f"{snapshot['case_id']}\t{snapshot['status']}\t{snapshot['phase']}\tupdated={snapshot['updated_at']}")
    print(f"events={len(payload['events'])}")
    return 0


def print_controller_artifacts(*, server: ControllerServer, args: argparse.Namespace) -> int:
    """Print controller artifact catalog rows.

    Args:
        server: Controller server whose store provides artifact rows.
        args: Parsed CLI namespace containing artifact filters.

    Returns:
        Process-style exit code.
    """

    rows = server.store.artifact_rows(trace_id=args.trace_id, task_id=args.task_id)
    if args.json:
        print(json.dumps([_artifact_row_json(dict(row)) for row in rows], ensure_ascii=False))
        return 0
    for row in rows:
        print(
            f"{row['artifact_id']}\t{row['task_id'] or '-'}\t{row['command_id'] or '-'}\t"
            f"{row['kind']}\t{row['storage_kind']}\t{row['size_bytes']}\t{row['sha256']}"
        )
    return 0


def print_controller_health(
    *,
    args: argparse.Namespace,
    config: ControllerConfig,
    server: ControllerServer,
    runner_registry_type: type[RunnerRegistry],
    registration_context_type: type[ControllerRunnerRegistrationContext],
    populate_runner_registry: Callable[..., None],
) -> int:
    """Print controller health and runner registration details.

    Args:
        args: Parsed CLI namespace containing health command options.
        config: Resolved controller configuration.
        server: Controller server whose SDK is used for runner discovery.
        runner_registry_type: Runner registry constructor.
        registration_context_type: Registration context constructor.
        populate_runner_registry: Callable that loads runner plugins.

    Returns:
        Process-style exit code.
    """

    resolved_app_config = resolve_app_config_path(getattr(args, "app_config", None))
    runner_registry = runner_registry_type()
    populate_runner_registry(
        runner_registry=runner_registry,
        sdk=server.sdk,
        context=registration_context_type(
            app_config_path=str(resolved_app_config),
            command_timeout_seconds=config.command_timeout_seconds,
            artifact_root_dir_override=getattr(args, "artifact_root_dir", "") or None,
        ),
    )
    registered_runner_agent_kinds = list(runner_registry.registered_agent_kinds())
    payload = {
        "ok": bool(registered_runner_agent_kinds),
        "db_path": str(config.db_path),
        "blob_dir": str(config.blob_dir),
        "artifact_root_dir": getattr(args, "artifact_root_dir", "") or "",
        "registered_runner_agent_kinds": registered_runner_agent_kinds,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"ok={payload['ok']}")
        print(f"db_path={payload['db_path']}")
        print(f"blob_dir={payload['blob_dir']}")
        print(f"artifact_root_dir={payload['artifact_root_dir']}")
        print("registered_runner_agent_kinds=" + ",".join(payload["registered_runner_agent_kinds"]))
    return 0


async def _load_task_detail(
    server: ControllerServer,
    task_id: str,
    *,
    watch: bool,
    poll_interval: float,
    emit_status: bool,
) -> dict[str, object] | None:
    """Load one task detail, optionally polling until it reaches a terminal state."""

    row = server.store.task_row(task_id)
    if row is None:
        return None
    last_status = str(row["status"])
    if watch and emit_status:
        print(f"watch task={task_id} status={last_status}")
    while watch and last_status not in _TERMINAL_TASK_STATUSES:
        await asyncio.sleep(poll_interval)
        row = server.store.task_row(task_id)
        if row is None:
            return None
        current_status = str(row["status"])
        if watch and emit_status and current_status != last_status:
            print(f"watch task={task_id} status={current_status}")
        last_status = current_status
    return _task_detail_json(server, task_id)


def _task_detail_json(server: ControllerServer, task_id: str) -> dict[str, object]:
    """Build one operator-friendly controller task detail payload."""

    task_row = server.store.task_row(task_id)
    if task_row is None:
        raise ValueError(f"unknown task_id: {task_id}")
    session_row = server.store.session_row_for_task(task_id)
    return {
        "task": _task_row_json(dict(task_row)),
        "session": dict(session_row) if session_row is not None else None,
        "artifacts": [_artifact_row_json(dict(row)) for row in server.store.artifact_rows(task_id=task_id)],
    }


def _artifact_row_json(row: dict[str, object]) -> dict[str, object]:
    """Serialize one artifact row without leaking large inline payloads."""

    result = dict(row)
    artifact_json = str(result.get("artifact_json") or "")
    if artifact_json:
        parsed = json.loads(artifact_json)
        inline_data = parsed.get("inline_data")
        if isinstance(inline_data, str):
            parsed["inline_data"] = f"<base64:{len(base64.b64decode(inline_data.encode('ascii')))} bytes>"
        result["artifact"] = parsed
    result.pop("artifact_json", None)
    return result


def _event_row_json(row: dict[str, object]) -> dict[str, object]:
    sanitized = _sanitize_for_operator_json(_row_with_json_fields(row, json_fields={"payload_json"}))
    return sanitized if isinstance(sanitized, dict) else {}


def _telemetry_event_row_json(row: dict[str, object]) -> dict[str, object]:
    sanitized = _sanitize_for_operator_json(_row_with_json_fields(row, json_fields={"payload_json"}))
    return sanitized if isinstance(sanitized, dict) else {}


def _telemetry_snapshot_row_json(row: dict[str, object]) -> dict[str, object]:
    sanitized = _sanitize_for_operator_json(_row_with_json_fields(row, json_fields={"payload_json"}))
    return sanitized if isinstance(sanitized, dict) else {}


def _device_row_json(row: dict[str, object]) -> dict[str, object]:
    sanitized = _sanitize_for_operator_json(_row_with_json_fields(row, json_fields={"descriptor_json", "load_json"}))
    return sanitized if isinstance(sanitized, dict) else {}


def _session_row_json(row: dict[str, object]) -> dict[str, object]:
    sanitized = _sanitize_for_operator_json(row)
    return sanitized if isinstance(sanitized, dict) else {}


def _row_with_json_fields(row: dict[str, object], *, json_fields: set[str]) -> dict[str, object]:
    result = dict(row)
    for field_name in json_fields:
        raw = result.get(field_name)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            result[field_name.removesuffix("_json")] = json.loads(raw)
        except json.JSONDecodeError:
            continue
        result.pop(field_name, None)
    return result


def _task_row_json(row: dict[str, object]) -> dict[str, object]:
    sanitized = _sanitize_for_operator_json(_row_with_json_fields(row, json_fields={"request_json", "result_json"}))
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_for_operator_json(value: object) -> object:
    return _shared_redact_for_log(value)


def _print_task_detail(detail: dict[str, object]) -> None:
    task = cast(Mapping[str, object], detail["task"])
    session = cast(Mapping[str, object] | None, detail["session"])
    artifacts = cast(Sequence[object], detail["artifacts"])
    print(f"task={task['task_id']}\tstatus={task['status']}\tagent={task['agent_kind']}\ttrace={task['trace_id']}")
    if session is not None:
        print(
            f"session={session['session_id']}\tstatus={session['status']}\t"
            f"device={session['device_id'] or '-'}\tlease={session['lease_id'] or '-'}"
        )
    print(f"artifacts={len(artifacts)}")
