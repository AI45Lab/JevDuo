"""Command result recording and artifact cataloging helpers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

from northcua.controller.execution.command_artifacts import CommandArtifactMaterializer
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call_profiled
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.result_compaction import compact_command_result_for_sqlite
from northcua_protocol.payloads import CommandResult
from northcua_protocol.state import CommandState


class CommandResultRecorder:
    """Persist controller-canonical command results and artifact rows."""

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        events: ControllerEventLog,
        artifacts: CommandArtifactMaterializer,
    ) -> None:
        self.store = store
        self.events = events
        self.artifacts = artifacts

    def record(self, result: CommandResult, *, trace_id: str) -> CommandResult:
        """Record a command result and return the canonical persisted result.

        Args:
            result: Command result received from a device or adapter.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Persisted result after artifact materialization and catalog-id
            rewriting, or cached terminal result for duplicates and late updates.
        """

        cached = self._terminal_cached_result(result, trace_id=trace_id)
        if cached is not None:
            return cached

        persist_started_at = time.perf_counter()
        task_id = ""
        if result.artifacts:
            task_row = self.store.session_row(result.session_id)
            task_id = str(task_row["task_id"]) if task_row is not None else ""
        result = self.artifacts.materialize_result(result, task_id=task_id)
        result, artifact_catalog_ids = self.artifacts.rewrite_result_metadata(
            result,
            trace_id=trace_id,
            task_id=task_id,
        )
        persisted_result = compact_command_result_for_sqlite(result)
        catalog_artifacts = persisted_result.artifacts
        cached = self.store.record_command_result(result, persisted_result=persisted_result)
        if cached is not None:
            elapsed_ms = int((time.perf_counter() - persist_started_at) * 1000)
            event_type = (
                "late_command_result"
                if cached.status in {CommandState.TIMED_OUT, CommandState.CANCELLED}
                else "command_result_deduped"
            )
            self.events.write(
                event_type,
                trace_id=trace_id,
                entity_type="command",
                entity_id=result.command_id,
                cached_status=cached.status.value,
                persist_elapsed_ms=elapsed_ms,
            )
            return cached
        for artifact in catalog_artifacts:
            self.store.record_artifact_ref(
                artifact,
                trace_id=trace_id,
                task_id=task_id,
                session_id=result.session_id,
                command_id=result.command_id,
                source="command_output",
                catalog_artifact_id=artifact_catalog_ids.get(artifact.artifact_id, ""),
            )
        elapsed_ms = int((time.perf_counter() - persist_started_at) * 1000)
        self.events.write(
            "command_result_persist_done",
            trace_id=trace_id,
            entity_type="command",
            entity_id=result.command_id,
            status=result.status.value,
            artifact_sha256=[artifact.sha256 for artifact in result.artifacts],
            persist_elapsed_ms=elapsed_ms,
            artifact_count=len(result.artifacts),
            catalog_artifact_count=len(catalog_artifacts),
        )
        return result

    async def record_async(
        self,
        result: CommandResult,
        *,
        trace_id: str,
        preloaded_command_row: Mapping[str, object] | None = None,
    ) -> CommandResult:
        recorded, _persisted = await self.record_async_with_status(
            result,
            trace_id=trace_id,
            preloaded_command_row=preloaded_command_row,
        )
        return recorded

    async def record_async_with_status(
        self,
        result: CommandResult,
        *,
        trace_id: str,
        preloaded_command_row: Mapping[str, object] | None = None,
    ) -> tuple[CommandResult, bool]:
        """Record a result off-loop and report whether this call won persistence.

        Command result validation, artifact catalog lookups, compaction, and
        SQLite mutations run on the controller DB worker so device websocket
        handling does not block the event loop when the controller DB is large
        or write-locked. Event emission remains on the event-loop thread.

        Args:
            result: Command result received from a device.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Canonical result and whether this call performed the result write.
        """

        cached = _cached_terminal_result_from_row(result, preloaded_command_row)
        if cached is not None:
            self.events.write(
                (
                    "late_command_result"
                    if cached.status in {CommandState.TIMED_OUT, CommandState.CANCELLED}
                    else "command_result_deduped"
                ),
                trace_id=trace_id,
                entity_type="command",
                entity_id=result.command_id,
                cached_status=cached.status.value,
                skipped_materialization=True,
                terminal_source="preloaded_read_pool",
            )
            return cached, False

        outcome, store_metrics = await controller_store_call_profiled(
            self.store.path,
            lambda store: _record_command_result_with_store(
                store,
                artifacts=self.artifacts,
                result=result,
                trace_id=trace_id,
            ),
            operation_name="command_record_result",
            priority=ControllerStorePriority.COMMAND,
        )
        self.events.write(
            outcome.event_type,
            trace_id=trace_id,
            entity_type="command",
            entity_id=outcome.result.command_id,
            **outcome.payload,
            store_operation=store_metrics.operation,
            store_mode=store_metrics.mode,
            store_queue_ms=store_metrics.queue_ms,
            store_admission_ms=store_metrics.admission_ms,
            store_executor_queue_ms=store_metrics.executor_queue_ms,
            store_operation_ms=store_metrics.operation_ms,
            store_exec_ms=store_metrics.exec_ms,
            store_sql_execute_ms=store_metrics.exec_ms,
            store_exec_scope="execute_and_transaction_excluding_fetch",
            store_total_ms=store_metrics.total_ms,
            store_queue_depth=store_metrics.queue_depth,
            store_admitted_queue_depth=store_metrics.admitted_queue_depth,
            store_sql_count=store_metrics.sql_count,
        )
        return outcome.result, outcome.persisted

    def _terminal_cached_result(self, result: CommandResult, *, trace_id: str) -> CommandResult | None:
        row = self.store.command_row(result.command_id, session_id=result.session_id, lease_id=result.lease_id)
        if row is None or str(row["status"]) not in {
            CommandState.SUCCEEDED.value,
            CommandState.FAILED.value,
            CommandState.TIMED_OUT.value,
            CommandState.CANCELLED.value,
        }:
            return None
        cached_json = row["result_json"]
        if not cached_json and str(row["status"]) == result.status.value:
            return None
        cached = (
            CommandResult.model_validate_json(str(cached_json))
            if cached_json
            else CommandResult(
                command_id=result.command_id,
                session_id=result.session_id,
                lease_id=result.lease_id,
                device_id=result.device_id,
                status=CommandState(str(row["status"])),
                error=f"late result ignored after {row['status']}",
            )
        )
        self.events.write(
            (
                "late_command_result"
                if cached.status in {CommandState.TIMED_OUT, CommandState.CANCELLED}
                else "command_result_deduped"
            ),
            trace_id=trace_id,
            entity_type="command",
            entity_id=result.command_id,
            cached_status=cached.status.value,
            skipped_materialization=True,
        )
        return cached


@dataclass(frozen=True)
class _RecordOutcome:
    result: CommandResult
    event_type: str
    payload: dict[str, object]
    persisted: bool


def _record_command_result_with_store(
    store: SQLiteControllerStore,
    *,
    artifacts: CommandArtifactMaterializer,
    result: CommandResult,
    trace_id: str,
) -> _RecordOutcome:
    persist_started_at = time.perf_counter()
    task_id = ""
    if result.artifacts:
        terminal_row = store.command_row(
            result.command_id,
            session_id=result.session_id,
            lease_id=result.lease_id,
        )
        cached = _cached_terminal_result_from_row(
            result,
            dict(terminal_row) if terminal_row is not None else None,
        )
        if cached is not None:
            return _RecordOutcome(
                result=cached,
                event_type=(
                    "late_command_result"
                    if cached.status in {CommandState.TIMED_OUT, CommandState.CANCELLED}
                    else "command_result_deduped"
                ),
                payload={
                    "cached_status": cached.status.value,
                    "skipped_materialization": True,
                    "terminal_source": "artifact_writer_precheck",
                },
                persisted=False,
            )
        task_row = store.session_row(result.session_id)
        task_id = str(task_row["task_id"]) if task_row is not None else ""
    worker_artifacts = CommandArtifactMaterializer(
        store=store,
        blob_store=artifacts.blob_store,
        max_inline_artifact_bytes=artifacts.max_inline_artifact_bytes,
    )
    result = worker_artifacts.materialize_result(result, task_id=task_id)
    result, artifact_catalog_ids = worker_artifacts.rewrite_result_metadata(
        result,
        trace_id=trace_id,
        task_id=task_id,
    )
    persisted_result = compact_command_result_for_sqlite(result)
    catalog_artifacts = persisted_result.artifacts
    cached = store.record_command_result(result, persisted_result=persisted_result)
    elapsed_ms = int((time.perf_counter() - persist_started_at) * 1000)
    if cached is not None:
        return _RecordOutcome(
            result=cached,
            event_type=(
                "late_command_result"
                if cached.status in {CommandState.TIMED_OUT, CommandState.CANCELLED}
                else "command_result_deduped"
            ),
            payload={
                "cached_status": cached.status.value,
                "persist_elapsed_ms": elapsed_ms,
            },
            persisted=False,
        )
    for artifact in catalog_artifacts:
        store.record_artifact_ref(
            artifact,
            trace_id=trace_id,
            task_id=task_id,
            session_id=result.session_id,
            command_id=result.command_id,
            source="command_output",
            catalog_artifact_id=artifact_catalog_ids.get(artifact.artifact_id, ""),
        )
    return _RecordOutcome(
        result=result,
        event_type="command_result_persist_done",
        payload={
            "status": result.status.value,
            "artifact_sha256": [artifact.sha256 for artifact in result.artifacts],
            "persist_elapsed_ms": elapsed_ms,
            "artifact_count": len(result.artifacts),
            "catalog_artifact_count": len(catalog_artifacts),
        },
        persisted=True,
    )


def _cached_terminal_result_from_row(
    result: CommandResult,
    row: Mapping[str, object] | None,
) -> CommandResult | None:
    if row is None:
        return None
    status = str(row.get("status") or "")
    if status not in {
        CommandState.SUCCEEDED.value,
        CommandState.FAILED.value,
        CommandState.TIMED_OUT.value,
        CommandState.CANCELLED.value,
    }:
        return None
    cached_json = row.get("result_json")
    if cached_json:
        return CommandResult.model_validate_json(str(cached_json))
    if status == result.status.value:
        return None
    return CommandResult(
        command_id=result.command_id,
        session_id=result.session_id,
        lease_id=result.lease_id,
        device_id=result.device_id,
        lease_generation=result.lease_generation,
        status=CommandState(status),
        error=f"late result ignored after {status}",
    )
