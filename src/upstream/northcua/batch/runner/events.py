"""Batch-local state and lifecycle event writing."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import cast

from northcua.batch.contracts import BatchTaskState
from northcua.batch.preparation.contracts import PreparedSandboxEventMessage
from northcua.batch.state import BatchStateRepository, BatchTaskLease
from northcua.batch.state.async_repository import repository_call
from northcua.sandboxes.contracts import SandboxLifecycleEvent, SandboxLifecycleMessage, SandboxLifecycleState
from northcua.telemetry.background_jsonl import BackgroundJsonlWriter
from northcua.telemetry.contracts import CasePhase, CaseStatus, TelemetryEntityKind, utc_now_iso
from northcua.telemetry.recorder import CaseTelemetryRecorder
from northcua.telemetry.sinks import TelemetrySink

_SNAPSHOT_CACHE_SECONDS = 2.0
JsonSafe = str | int | float | bool | None | list["JsonSafe"] | dict[str, "JsonSafe"]
logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


_SLOW_WRITE_SECONDS = max(_env_float("NORTHCUA_BATCH_EVENT_SLOW_WRITE_SECONDS", 0.5), 0.0)
_SLOW_WRITE_LOG_INTERVAL_SECONDS = max(
    _env_float("NORTHCUA_BATCH_EVENT_SLOW_WRITE_LOG_INTERVAL_SECONDS", 30.0),
    0.0,
)


class BatchAttemptEventMessage(str, Enum):
    """Stable per-attempt diagnostic event names persisted in JSONL telemetry."""

    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_CLEANUP_FINISHED = "attempt_cleanup_finished"
    SANDBOX_PREPARE_STARTED = "sandbox_prepare_started"
    SANDBOX_READY_FOR_CONTROLLER_SUBMIT = "sandbox_ready_for_controller_submit"
    PRE_SUBMIT_COMMANDS_STARTED = "pre_submit_commands_started"
    PRE_SUBMIT_COMMANDS_FINISHED = "pre_submit_commands_finished"
    CONTROLLER_SUBMIT_PREFLIGHT_STARTED = "controller_submit_preflight_started"
    CONTROLLER_SUBMIT_PREFLIGHT_FINISHED = "controller_submit_preflight_finished"
    CONTROLLER_SUBMIT_STARTED = "controller_submit_started"
    CONTROLLER_SUBMIT_OPEN_STARTED = "controller_submit_open_started"
    CONTROLLER_SUBMIT_REQUEST_SENT = "controller_submit_request_sent"
    CONTROLLER_SUBMIT_ACCEPTED = "controller_submit_accepted"
    CONTROLLER_STATUS_DB_LOOKUP_FINISHED = "controller_status_db_lookup_finished"
    CONTROLLER_SUBMIT_EXISTING_NON_TERMINAL_RECONCILED = "controller_submit_existing_non_terminal_reconciled"
    CONTROLLER_SUBMIT_TIMED_OUT = "controller_submit_timed_out"
    CONTROLLER_SUBMIT_FAILED_BEFORE_TERMINAL = "controller_submit_failed_before_terminal"
    CONTROLLER_SUBMIT_FINISHED = "controller_submit_finished"
    POST_RUN_HOOK_STARTED = "post_run_hook_started"
    POST_RUN_HOOK_FINISHED = "post_run_hook_finished"
    INFRA_RETRY_BACKOFF_SCHEDULED = "infra_retry_backoff_scheduled"


@dataclass
class _SnapshotCache:
    captured_at: float = 0.0
    concurrency: dict[str, object] = field(default_factory=dict)
    resource: dict[str, object] = field(default_factory=dict)


@dataclass
class _SlowWriteLogState:
    last_log_at_by_operation: dict[str, float] = field(default_factory=dict)


class BatchEventWriter:
    """Write batch task snapshots and sandbox lifecycle events."""

    def __init__(
        self,
        *,
        repository: BatchStateRepository,
        output_dir: Path,
        batch_id: str,
        telemetry_sink: TelemetrySink | None = None,
        experiment_id: str = "",
        lease: BatchTaskLease | None = None,
        jsonl_path: Path | None = None,
        snapshot_cache: _SnapshotCache | None = None,
        telemetry_recorder: CaseTelemetryRecorder | None = None,
        slow_write_log_state: _SlowWriteLogState | None = None,
        jsonl_writer: BackgroundJsonlWriter | None = None,
        owns_jsonl_writer: bool = False,
    ) -> None:
        self.repository = repository
        self.output_dir = output_dir
        self.batch_id = batch_id
        self.lease = lease
        self.telemetry_sink = telemetry_sink
        self.experiment_id = experiment_id or batch_id
        self.lifecycle_path = output_dir / "sandbox_events.jsonl"
        self.attempt_events_path = output_dir / "batch_attempt_events.jsonl"
        self.prepared_events_path = output_dir / "prepared_sandbox_events.jsonl"
        self.jsonl_path = jsonl_path or output_dir / "batch_state.jsonl"
        self._snapshot_cache = snapshot_cache or _SnapshotCache()
        self._slow_write_log_state = slow_write_log_state or _SlowWriteLogState()
        self._jsonl_writer = jsonl_writer
        self._owns_jsonl_writer = owns_jsonl_writer
        self.telemetry = telemetry_recorder or (
            CaseTelemetryRecorder(sink=telemetry_sink, batch_id=batch_id, experiment_id=self.experiment_id)
            if telemetry_sink is not None
            else None
        )
        if self.telemetry is not None and telemetry_recorder is None:
            for state in self.repository.all():
                self.telemetry.seed_batch_state(state)
        self.lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
        self.attempt_events_path.parent.mkdir(parents=True, exist_ok=True)
        self.prepared_events_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def for_lease(self, lease: BatchTaskLease) -> "BatchEventWriter":
        """Create a task-scoped writer that carries a lease token.

        Args:
            lease: Active task lease used to authorize state writes.

        Returns:
            Writer for the same output directory and repository.
        """

        return BatchEventWriter(
            repository=self.repository,
            output_dir=self.output_dir,
            batch_id=self.batch_id,
            telemetry_sink=self.telemetry_sink,
            experiment_id=self.experiment_id,
            lease=lease,
            jsonl_path=self.jsonl_path,
            snapshot_cache=self._snapshot_cache,
            telemetry_recorder=self.telemetry,
            slow_write_log_state=self._slow_write_log_state,
            jsonl_writer=self._jsonl_writer,
            owns_jsonl_writer=False,
        )

    def upsert_task(self, state: BatchTaskState) -> bool:
        """Persist a task state snapshot.

        Args:
            state: Latest state for a manifest task.

        Returns:
            ``True`` when the repository accepted the state write.
        """

        started_at = time.monotonic()
        changed: bool | None = None
        try:
            lease_token = self.lease.lease_token if self.lease is not None else ""
            changed = self.repository.upsert_state(state, lease_token=lease_token)
            if changed:
                self._append_text(self.jsonl_path, state.model_dump_json() + "\n")
                if self.telemetry is not None:
                    self.telemetry.record_batch_state(state)
        finally:
            self._maybe_log_slow_write(
                operation="upsert_task",
                started_at=started_at,
                case_id=state.task_id,
                status=state.status.value,
                changed=changed,
            )
        return bool(changed)

    async def upsert_task_async(self, state: BatchTaskState) -> bool:
        """Persist a task state snapshot without blocking the event loop.

        Args:
            state: Latest state for a manifest task.

        Returns:
            ``True`` when the repository accepted the state write.
        """

        started_at = time.monotonic()
        changed: bool | None = None
        try:
            lease_token = self.lease.lease_token if self.lease is not None else ""
            changed = await repository_call(lambda: self.repository.upsert_state(state, lease_token=lease_token))
            if changed:
                self._append_text(self.jsonl_path, state.model_dump_json() + "\n")
                if self.telemetry is not None:
                    self.telemetry.record_batch_state(state)
        finally:
            self._maybe_log_slow_write(
                operation="upsert_task_async",
                started_at=started_at,
                case_id=state.task_id,
                status=state.status.value,
                changed=changed,
            )
        return bool(changed)

    def sandbox_event(self, event: SandboxLifecycleEvent, *, phase_override: CasePhase | None = None) -> None:
        """Append one sandbox lifecycle event.

        Args:
            event: Provider lifecycle event emitted while acquiring or releasing
                sandbox capacity.
            phase_override: Optional case phase for background flows that
                should not be attributed to ordinary claim-time sandbox setup.
        """

        started_at = time.monotonic()
        try:
            self._append_json(self.lifecycle_path, event.model_dump(mode="json"))
            if self.telemetry is not None:
                self.telemetry.record_case_event(
                    case_id=event.task_id,
                    entity_kind=TelemetryEntityKind.SANDBOX,
                    entity_id=event.sandbox_id or event.task_id,
                    phase=phase_override or _phase_for_sandbox_event(event),
                    status=_status_for_sandbox_event(event),
                    message=event.message,
                    details=event.metadata,
                    sandbox_id=event.sandbox_id,
                )
        finally:
            self._maybe_log_slow_write(
                operation="sandbox_event",
                started_at=started_at,
                case_id=event.task_id,
                status=event.state.value,
            )

    def prepared_event(
        self,
        *,
        case_id: str,
        message: PreparedSandboxEventMessage | str,
        status: CaseStatus = CaseStatus.PROVISIONING,
        attempt: int = 0,
        details: dict[str, object] | None = None,
        sandbox_id: str = "",
        device_id: str = "",
    ) -> None:
        """Record prepared-sandbox lifecycle telemetry for one case.

        Args:
            case_id: Batch task/case id.
            message: Stable event message.
            status: Case-level telemetry status for the event.
            attempt: One-based attempt count when known.
            details: Optional JSON-safe event details.
            sandbox_id: Provider sandbox id when known.
            device_id: Device worker id when known.
        """

        started_at = time.monotonic()
        message_value = message.value if isinstance(message, PreparedSandboxEventMessage) else str(message)
        try:
            safe_details = _json_safe_dict(details or {})
            row = {
                "timestamp": utc_now_iso(),
                "batch_id": self.batch_id,
                "case_id": case_id,
                "attempt": attempt,
                "message": message_value,
                "status": status.value,
                "sandbox_id": sandbox_id,
                "device_id": device_id,
                "details": safe_details,
            }
            self._append_json(self.prepared_events_path, row)
            if self.telemetry is None:
                return
            self.telemetry.record_case_event(
                case_id=case_id,
                entity_kind=TelemetryEntityKind.SANDBOX,
                entity_id=sandbox_id or case_id,
                phase=CasePhase.PREPARED_SANDBOX,
                status=status,
                attempt=attempt,
                message=message_value,
                details=safe_details,
                sandbox_id=sandbox_id,
                device_id=device_id,
            )
        finally:
            self._maybe_log_slow_write(
                operation="prepared_event",
                started_at=started_at,
                case_id=case_id,
                status=status.value,
            )

    def attempt_event(
        self,
        *,
        case_id: str,
        message: BatchAttemptEventMessage,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int = 0,
        entity_kind: TelemetryEntityKind = TelemetryEntityKind.TASK,
        entity_id: str = "",
        details: dict[str, object] | None = None,
        controller_task_id: str = "",
        trace_id: str = "",
        sandbox_id: str = "",
        device_id: str = "",
    ) -> None:
        """Record a per-attempt diagnostic event even when telemetry is disabled.

        Args:
            case_id: Batch task/case id.
            message: Stable per-attempt event message.
            phase: Case phase associated with the event.
            status: Case-level status associated with the event.
            attempt: One-based attempt count when known.
            entity_kind: Telemetry entity kind for the event.
            entity_id: Explicit entity id; defaults to the first available task,
                sandbox, device, or case identifier.
            details: Optional JSON-safe event metadata.
            controller_task_id: Controller task id associated with the attempt.
            trace_id: Trace id associated with the attempt.
            sandbox_id: Sandbox id associated with the attempt.
            device_id: Device worker id associated with the attempt.
        """

        started_at = time.monotonic()
        try:
            snapshots = _snapshots(
                cache=self._snapshot_cache,
                repository=self.repository,
                output_dir=self.output_dir,
            )
            safe_details = _json_safe_dict(
                {
                    **(details or {}),
                    "concurrency_snapshot": snapshots.concurrency,
                    "resource_snapshot": snapshots.resource,
                }
            )
            row = {
                "timestamp": utc_now_iso(),
                "batch_id": self.batch_id,
                "case_id": case_id,
                "attempt": attempt,
                "message": message.value,
                "phase": phase.value,
                "status": status.value,
                "entity_kind": entity_kind.value,
                "entity_id": entity_id or controller_task_id or sandbox_id or device_id or case_id,
                "controller_task_id": controller_task_id,
                "trace_id": trace_id,
                "sandbox_id": sandbox_id,
                "device_id": device_id,
                "details": safe_details,
            }
            self._append_json(self.attempt_events_path, row)
            if self.telemetry is not None:
                telemetry_details = dict(safe_details)
                if controller_task_id:
                    telemetry_details["controller_task_id"] = controller_task_id
                if trace_id:
                    telemetry_details["trace_id"] = trace_id
                self.telemetry.record_case_event(
                    case_id=case_id,
                    entity_kind=entity_kind,
                    entity_id=row["entity_id"],
                    phase=phase,
                    status=status,
                    attempt=attempt,
                    message=message.value,
                    details=telemetry_details,
                    sandbox_id=sandbox_id,
                    device_id=device_id,
                )
        finally:
            self._maybe_log_slow_write(
                operation="attempt_event",
                started_at=started_at,
                case_id=case_id,
                status=status.value,
            )

    def _maybe_log_slow_write(
        self,
        *,
        operation: str,
        started_at: float,
        case_id: str = "",
        status: str = "",
        changed: bool | None = None,
    ) -> None:
        elapsed_seconds = max(time.monotonic() - started_at, 0.0)
        if elapsed_seconds < _SLOW_WRITE_SECONDS:
            return
        now = time.monotonic()
        last = self._slow_write_log_state.last_log_at_by_operation.get(operation, 0.0)
        if now - last < _SLOW_WRITE_LOG_INTERVAL_SECONDS:
            return
        self._slow_write_log_state.last_log_at_by_operation[operation] = now
        logger.warning(
            "batch event writer slow sync operation",
            extra={
                "batch_id": self.batch_id,
                "operation": operation,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "case_id": case_id,
                "status": status,
                "changed": changed,
                "output_dir": str(self.output_dir),
            },
        )

    def flush(self) -> None:
        """Flush owned background JSONL resources."""

        if self._jsonl_writer is None:
            return
        if self._owns_jsonl_writer:
            self._jsonl_writer.close()
            self._owns_jsonl_writer = False
        else:
            self._jsonl_writer.flush()

    def _append_json(self, path: Path, payload: dict[str, object]) -> None:
        if self._jsonl_writer is not None:
            self._jsonl_writer.append_json(path, payload)
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _append_text(self, path: Path, text: str) -> None:
        if self._jsonl_writer is not None:
            self._jsonl_writer.append_text(path, text)
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @contextmanager
    def phase_timer(
        self,
        *,
        case_id: str,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int = 0,
        entity_kind: TelemetryEntityKind = TelemetryEntityKind.CASE,
        entity_id: str = "",
    ) -> Iterator[None]:
        """Measure a phase when telemetry is enabled.

        Args:
            case_id: Manifest task/case id.
            phase: Case phase being measured.
            status: Status associated with the measured phase.
            attempt: One-based attempt count.
            entity_kind: Entity kind that owns this timing.
            entity_id: Entity id; defaults to ``case_id``.

        Returns:
            Context manager that records the phase when telemetry is enabled,
            or a no-op context manager otherwise.
        """

        if self.telemetry is None:
            yield
            return
        with self.telemetry.phase_timer(
            case_id=case_id,
            phase=phase,
            status=status,
            attempt=attempt,
            entity_kind=entity_kind,
            entity_id=entity_id,
        ):
            yield


def _phase_for_sandbox_event(event: SandboxLifecycleEvent) -> CasePhase:
    message = event.message
    if message == SandboxLifecycleMessage.SANDBOX_ACQUIRE_STARTED:
        return CasePhase.SANDBOX_CREATE
    if message == SandboxLifecycleMessage.DEVICE_WORKER_STARTED:
        return CasePhase.WORKER_BOOTSTRAP
    if message in {
        SandboxLifecycleMessage.DEVICE_READINESS_WAIT_STARTED,
        SandboxLifecycleMessage.DEVICE_READINESS_WAIT_FINISHED,
    }:
        return CasePhase.WORKER_READY
    if message == SandboxLifecycleMessage.DEVICE_READY:
        return CasePhase.WORKER_READY
    if message == SandboxLifecycleMessage.SANDBOX_RELEASED or event.state == SandboxLifecycleState.TERMINATED:
        return CasePhase.SANDBOX_TERMINATE
    return CasePhase.SANDBOX_CREATE


def _status_for_sandbox_event(event: SandboxLifecycleEvent) -> CaseStatus:
    if event.state == SandboxLifecycleState.TERMINATED:
        return CaseStatus.FINALIZING
    if event.message in {
        SandboxLifecycleMessage.DEVICE_WORKER_STARTED,
        SandboxLifecycleMessage.DEVICE_READINESS_WAIT_STARTED,
        SandboxLifecycleMessage.DEVICE_READINESS_WAIT_FINISHED,
        SandboxLifecycleMessage.DEVICE_READY,
    }:
        return CaseStatus.BOOTSTRAPPING
    return CaseStatus.PROVISIONING


def _json_safe(value: object) -> JsonSafe:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _json_safe_dict(value: dict[str, object]) -> dict[str, object]:
    safe_value = _json_safe(value)
    return cast(dict[str, object], safe_value) if isinstance(safe_value, dict) else {}


def _snapshots(*, cache: _SnapshotCache, repository: BatchStateRepository, output_dir: Path) -> _SnapshotCache:
    now = time.monotonic()
    if now - cache.captured_at <= _SNAPSHOT_CACHE_SECONDS:
        return cache
    cache.concurrency = _concurrency_snapshot(repository)
    cache.resource = _resource_snapshot(output_dir)
    cache.captured_at = now
    return cache


def _concurrency_snapshot(repository: BatchStateRepository) -> dict[str, object]:
    """Return lightweight live scheduling counts without deserializing every row."""

    path = getattr(repository, "path", None)
    if path:
        try:
            return _sqlite_concurrency_snapshot(Path(path))
        except sqlite3.Error:
            return {}
    counts: dict[str, int] = {}
    for state in repository.all():
        counts[state.status.value] = counts.get(state.status.value, 0) + 1
    return _concurrency_from_counts(counts, prepared_counts={})


def _sqlite_concurrency_snapshot(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05) as connection:
        task_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT status, COUNT(*) FROM batch_tasks GROUP BY status").fetchall()
        }
        prepared_counts: dict[str, int] = {}
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_sandboxes'"
        ).fetchone()
        if table is not None:
            prepared_counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM prepared_sandboxes GROUP BY status"
                ).fetchall()
            }
    return _concurrency_from_counts(task_counts, prepared_counts=prepared_counts)


def _concurrency_from_counts(counts: dict[str, int], *, prepared_counts: dict[str, int]) -> dict[str, object]:
    active_statuses = ("starting_sandbox", "bootstrapping_device", "waiting_device", "submitted", "running")
    prepare_inflight = sum(prepared_counts.get(status, 0) for status in ("reserved", "preparing"))
    active_unassigned = prepare_inflight + prepared_counts.get("prepared", 0)
    assigned_live = prepared_counts.get("assigned", 0) + prepared_counts.get("cleanup_deferred", 0)
    prepared_live_total = active_unassigned + assigned_live
    return {
        "task_status_counts": counts,
        "active": sum(counts.get(status, 0) for status in active_statuses),
        "starting_sandbox": counts.get("starting_sandbox", 0),
        "submit_inflight": counts.get("submitted", 0) + counts.get("running", 0),
        "prepare_inflight": prepare_inflight,
        "prepared_pool_depth": prepared_counts.get("prepared", 0),
        "prepared_live_total": prepared_live_total,
        "assigned_live": assigned_live,
        "cleanup_deferred": prepared_counts.get("cleanup_deferred", 0),
        "prepared_status_counts": prepared_counts,
    }


def _resource_snapshot(output_dir: Path) -> dict[str, object]:
    """Return cheap host/resource counters for correlating bursts with failures."""

    snapshot: dict[str, object] = {}
    try:
        load1, load5, load15 = os.getloadavg()
        snapshot["loadavg"] = [round(load1, 3), round(load5, 3), round(load15, 3)]
    except OSError:
        pass
    try:
        usage = shutil.disk_usage(output_dir)
        snapshot["disk"] = {
            "path": str(output_dir),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError:
        pass
    return snapshot
