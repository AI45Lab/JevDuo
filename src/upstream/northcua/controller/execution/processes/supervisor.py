"""Per-task process worker supervisor."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.processes.contracts import (
    WorkerCommandDispatchErrorCode,
    WorkerCommandDispatchRequest,
    WorkerCommandDispatchResponse,
    WorkerEnvelope,
    WorkerError,
    WorkerHeartbeat,
    WorkerHello,
    WorkerJobFailureKind,
    WorkerJobStatus,
    WorkerMessageType,
    WorkerProgressEvent,
    WorkerRunRequest,
    WorkerTerminalResult,
)
from northcua.controller.execution.processes.ipc import WorkerIPCError, read_envelope, write_envelope
from northcua.controller.execution.processes.metrics import (
    ProcessExecutorMetric,
    ProcessExecutorMetricName,
    log_process_metric,
)
from northcua.controller.execution.processes.transport import build_command_request
from northcua.controller.execution.sdk import ControllerSDK
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.worker_heartbeat_batcher import WorkerHeartbeatBatcher
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import CommandRequest, CommandResult, TaskFinished, TelemetryUpdate
from northcua_protocol.state import TaskState
from northcua_runtime_shared.validation import ToolParameterValidationError

logger = logging.getLogger(__name__)

_MAX_IN_FLIGHT_WORKER_COMMANDS = 16
_TERMINAL_DRAIN_GRACE_SECONDS = 1.0
_HEARTBEAT_PERSIST_INTERVAL_SECONDS = 15.0
_COMMAND_TIMEOUT_FLOOR_ENV = "NORTHCUA_CONTROLLER_COMMAND_TIMEOUT_FLOOR_SECONDS"
_LEGACY_COMMAND_TIMEOUT_FLOOR_ENV = "NORTHCUA_ACTION_COMMAND_TIMEOUT_SECONDS"
_COMMAND_SHORT_TIMEOUT_FLOOR_ENV = "NORTHCUA_CONTROLLER_COMMAND_SHORT_TIMEOUT_FLOOR_SECONDS"
_DEFAULT_SHORT_TIMEOUT_FLOOR_SECONDS = 90.0
_COMMAND_RESPONSE_GRACE_SECONDS = 30.0
_WORKER_METADATA_OVERRIDE_KEYS = {
    "northcua.desktop.coordinate_space",
    "northcua.desktop.coordinate_clamp_to_bounds",
}


class WorkerHeartbeatTimeoutError(TimeoutError):
    """Raised when an already-started worker stops sending heartbeats."""


class WorkerReportedError(RuntimeError):
    """Worker-side structured failure with retryability preserved."""

    def __init__(self, error: WorkerError) -> None:
        super().__init__(error.message)
        self.error = error


class ProcessWorkerSupervisor:
    """Own one single-use worker process for one controller task."""

    def __init__(
        self,
        *,
        context: ControllerExecutionContext,
        sdk: ControllerSDK,
        store: SQLiteControllerStore,
        job_id: str,
        attempt_id: str,
        app_config_path: str,
        artifact_root_dir_override: str | None,
        command_timeout_seconds: float,
        start_timeout_seconds: float,
        heartbeat_timeout_seconds: float,
        worker_module: str = "northcua.controller.execution.processes.worker_main",
        command_timeout_floor_seconds: float | None = None,
        heartbeat_batcher: WorkerHeartbeatBatcher | None = None,
    ) -> None:
        self.context = context
        self.sdk = sdk
        self.store = store
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.app_config_path = app_config_path
        self.artifact_root_dir_override = artifact_root_dir_override
        self.command_timeout_seconds = command_timeout_seconds
        self.command_timeout_floor_seconds = (
            None if command_timeout_floor_seconds is None else max(0.0, float(command_timeout_floor_seconds))
        )
        self.start_timeout_seconds = start_timeout_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.worker_module = worker_module
        self._heartbeat_batcher = heartbeat_batcher
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._connection_ready: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
        self._last_heartbeat_at = time.monotonic()
        self._last_heartbeat_persisted_at = 0.0
        self._last_heartbeat_state_key: tuple[object, ...] | None = None
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._command_slots = asyncio.Semaphore(_MAX_IN_FLIGHT_WORKER_COMMANDS)
        self._process: asyncio.subprocess.Process | None = None
        self._cancel_requested_reason = ""
        self._startup_started_at = 0.0
        self._ready_recorded = False
        self._command_roundtrip_ms: list[float] = []
        self._command_failure_count = 0

    async def run(self) -> TaskFinished:
        """Run the worker process and return a terminal task payload.

        Returns:
            Canonical terminal payload for the controller task.

        Raises:
            RuntimeError: If worker startup exits before IPC is established.
            TimeoutError: If worker startup exceeds the configured timeout.
        """

        self._connection_ready = asyncio.get_running_loop().create_future()
        with tempfile.TemporaryDirectory(prefix="northcua-worker-") as temp_dir:
            socket_path = str(Path(temp_dir) / "worker.sock")
            server = await asyncio.start_unix_server(self._accept_connection, path=socket_path)
            process: asyncio.subprocess.Process | None = None
            reader_loop: asyncio.Task[TaskFinished] | None = None
            heartbeat_monitor: asyncio.Task[None] | None = None
            wait_task: asyncio.Task[int] | None = None
            connect_task: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] | None = None
            stderr_path = Path(temp_dir) / "worker.stderr.log"
            worker_nonce = secrets.token_hex(24)
            try:
                await controller_store_call(
                    self.store.path,
                    lambda store: store.mark_worker_job_status(
                        job_id=self.job_id,
                        status=WorkerJobStatus.STARTING,
                    ),
                    operation_name="worker_job_starting",
                    priority=ControllerStorePriority.CRITICAL,
                )
                self._startup_started_at = time.perf_counter()
                with stderr_path.open("wb") as stderr_file:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        self.worker_module,
                        "--socket",
                        socket_path,
                        "--nonce",
                        worker_nonce,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=stderr_file,
                    )
                self._process = process
                wait_task = asyncio.create_task(process.wait(), name=f"northcua-worker-wait:{self.job_id}")
                connect_task = asyncio.ensure_future(self._connection_ready)
                active_wait_task = wait_task
                active_connect_task = connect_task
                done, _pending = await asyncio.wait(
                    {active_connect_task, active_wait_task},
                    timeout=self.start_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if active_connect_task in done:
                    reader, writer = active_connect_task.result()
                    await asyncio.wait_for(
                        self._receive_worker_hello(reader, expected_nonce=worker_nonce),
                        timeout=max(1.0, self.start_timeout_seconds - (time.perf_counter() - self._startup_started_at)),
                    )
                elif active_wait_task in done:
                    if self._cancel_requested_reason:
                        return await self._canceled_payload(self._cancel_requested_reason)
                    exit_code = active_wait_task.result()
                    stderr_tail = _read_tail(stderr_path)
                    raise RuntimeError(f"worker exited before IPC connect: exit_code={exit_code}; stderr={stderr_tail}")
                else:
                    raise TimeoutError(f"worker did not start within {self.start_timeout_seconds}s")
                await self._record_metric(
                    ProcessExecutorMetric(
                        metric_name=ProcessExecutorMetricName.WORKER_CONNECT,
                        job_id=self.job_id,
                        task_id=self.context.request.task_id,
                        attempt_id=self.attempt_id,
                        value_ms=(time.perf_counter() - self._startup_started_at) * 1000,
                        metadata={"worker_pid": int(process.pid or 0)},
                    )
                )
                self._writer = writer
                await controller_store_call(
                    self.store.path,
                    lambda store: store.mark_worker_job_status(
                        job_id=self.job_id,
                        status=WorkerJobStatus.STARTING,
                        worker_pid=int(process.pid or 0),
                    ),
                    operation_name="worker_job_pid",
                    priority=ControllerStorePriority.CRITICAL,
                )
                await self._send_run_request(writer)
                reader_loop = asyncio.create_task(
                    self._read_worker(reader, writer), name=f"northcua-worker:{self.job_id}"
                )
                heartbeat_monitor = asyncio.create_task(
                    self._monitor_heartbeat(process),
                    name=f"northcua-worker-heartbeat:{self.job_id}",
                )
                done, _pending = await asyncio.wait(
                    {reader_loop, wait_task, heartbeat_monitor},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reader_loop in done:
                    terminal = reader_loop.result()
                    await self._close_writer()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wait_task, timeout=10.0)
                    return terminal
                if heartbeat_monitor in done:
                    heartbeat_monitor.result()
                if wait_task in done and not reader_loop.done():
                    if self._cancel_requested_reason:
                        return await self._canceled_payload(self._cancel_requested_reason)
                    with contextlib.suppress(TimeoutError):
                        terminal = await asyncio.wait_for(reader_loop, timeout=_TERMINAL_DRAIN_GRACE_SECONDS)
                        await self._close_writer()
                        return terminal
                exit_code = process.returncode if process.returncode is not None else await wait_task
                stderr_tail = _read_tail(stderr_path)
                raise RuntimeError(
                    f"worker exited before terminal payload: exit_code={exit_code}; stderr={stderr_tail}"
                )
            except WorkerHeartbeatTimeoutError as exc:
                if self._cancel_requested_reason:
                    return await self._canceled_payload(self._cancel_requested_reason)
                return await self._failure_payload(WorkerJobFailureKind.HEARTBEAT_TIMEOUT, str(exc), exc=exc)
            except WorkerReportedError as exc:
                if self._cancel_requested_reason:
                    return await self._canceled_payload(self._cancel_requested_reason)
                return await self._failure_payload(
                    exc.error.failure_kind,
                    exc.error.message,
                    retryable=exc.error.retryable,
                    exc=exc,
                )
            except WorkerIPCError as exc:
                if self._cancel_requested_reason:
                    return await self._canceled_payload(self._cancel_requested_reason)
                return await self._failure_payload(WorkerJobFailureKind.IPC_PROTOCOL_ERROR, str(exc), exc=exc)
            except TimeoutError as exc:
                if self._cancel_requested_reason:
                    return await self._canceled_payload(self._cancel_requested_reason)
                return await self._failure_payload(
                    WorkerJobFailureKind.START_TIMEOUT,
                    f"worker did not start within {self.start_timeout_seconds}s",
                    exc=exc,
                )
            except Exception as exc:
                if self._cancel_requested_reason:
                    return await self._canceled_payload(self._cancel_requested_reason)
                logger.warning(
                    "process worker crashed before terminal payload",
                    extra={"job_id": self.job_id, "task_id": self.context.request.task_id},
                    exc_info=True,
                )
                return await self._failure_payload(WorkerJobFailureKind.WORKER_CRASH, str(exc), exc=exc)
            finally:
                if connect_task is not None and not connect_task.done():
                    connect_task.cancel()
                if heartbeat_monitor is not None:
                    heartbeat_monitor.cancel()
                if reader_loop is not None and not reader_loop.done():
                    reader_loop.cancel()
                await self._cancel_command_tasks()
                await self._terminate_process(process)
                if self._heartbeat_batcher is not None:
                    await self._heartbeat_batcher.discard(self.job_id)
                self._process = None
                server.close()
                await server.wait_closed()
                await self._flush_command_roundtrip_summary()

    async def cancel(self, *, reason: str) -> None:
        """Ask the worker to stop, then terminate the process if it lingers.

        Args:
            reason: Operator or controller reason recorded on the worker job.
        """

        self._cancel_requested_reason = reason
        writer = self._writer
        if writer is not None:
            with contextlib.suppress(Exception):
                await self._write(writer, WorkerMessageType.CANCEL, json.dumps({"reason": reason}))
        await controller_store_call(
            self.store.path,
            lambda store: store.mark_worker_job_status(
                job_id=self.job_id,
                status=WorkerJobStatus.CANCELED,
                failure_kind=WorkerJobFailureKind.CANCELED,
                error=reason,
            ),
            operation_name="worker_job_cancel",
            priority=ControllerStorePriority.CRITICAL,
        )
        await self._cancel_command_tasks()
        await self._terminate_process(self._process)
        if self._heartbeat_batcher is not None:
            await self._heartbeat_batcher.discard(self.job_id)

    async def _canceled_payload(self, reason: str) -> TaskFinished:
        await controller_store_call(
            self.store.path,
            lambda store: store.mark_worker_job_status(
                job_id=self.job_id,
                status=WorkerJobStatus.CANCELED,
                failure_kind=WorkerJobFailureKind.CANCELED,
                error=reason,
            ),
            operation_name="worker_job_cancel",
            priority=ControllerStorePriority.CRITICAL,
        )
        return TaskFinished(
            task_id=self.context.request.task_id,
            trace_id=self.context.request.trace_id,
            session_id=self.context.session_id,
            device_id=self.context.lease.device_id,
            lease_id=self.context.lease.lease_id,
            state=TaskState.INTERRUPTED,
            error=reason,
            terminal_decision=AgentTerminalDecision.error(
                reason=AgentTerminalReason.INTERRUPTED,
                final_answer=reason,
                summary="The controller canceled the process worker.",
                retryable=False,
                metadata={"worker_job_id": self.job_id, "worker_failure_kind": WorkerJobFailureKind.CANCELED.value},
            ).to_json(),
        )

    async def _accept_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connection_ready.done():
            writer.close()
            await writer.wait_closed()
            return
        self._connection_ready.set_result((reader, writer))

    async def _receive_worker_hello(self, reader: asyncio.StreamReader, *, expected_nonce: str) -> None:
        envelope = await read_envelope(reader)
        if envelope.message_type != WorkerMessageType.HELLO:
            raise WorkerIPCError(f"expected worker hello, got {envelope.message_type.value}")
        hello = WorkerHello.model_validate_json(envelope.payload_json)
        if hello.nonce != expected_nonce:
            raise WorkerIPCError("worker hello nonce mismatch")

    async def _send_run_request(self, writer: asyncio.StreamWriter) -> None:
        request = WorkerRunRequest(
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            app_config_path=self.app_config_path,
            artifact_root_dir_override=self.artifact_root_dir_override,
            command_timeout_seconds=self.command_timeout_seconds,
            command_response_timeout_seconds=self._worker_command_response_timeout_seconds(),
            heartbeat_interval_seconds=self._worker_heartbeat_interval_seconds(),
            request=self.context.request,
            session_id=self.context.session_id,
            lease=self.context.lease,
            blob_store_root=str(self.context.blob_store_root) if self.context.blob_store_root is not None else None,
        )
        await self._write(writer, WorkerMessageType.RUN_REQUEST, request.model_dump_json())

    async def _read_worker(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> TaskFinished:
        while True:
            envelope = await read_envelope(reader)
            if envelope.message_type == WorkerMessageType.READY:
                await controller_store_call(
                    self.store.path,
                    lambda store: store.mark_worker_job_status(
                        job_id=self.job_id,
                        status=WorkerJobStatus.RUNNING,
                    ),
                    operation_name="worker_job_running",
                    priority=ControllerStorePriority.CRITICAL,
                )
                await self._record_worker_ready_metric()
                continue
            if envelope.message_type == WorkerMessageType.HEARTBEAT:
                await self._handle_heartbeat(envelope)
                continue
            if envelope.message_type == WorkerMessageType.PROGRESS:
                await self._handle_progress(envelope)
                continue
            if envelope.message_type == WorkerMessageType.COMMAND_DISPATCH:
                if self._active_command_task_count() >= _MAX_IN_FLIGHT_WORKER_COMMANDS:
                    raise WorkerIPCError("worker exceeded in-flight command dispatch limit")
                task = asyncio.create_task(
                    self._run_command_dispatch(envelope, writer),
                    name=f"northcua-worker-command:{self.job_id}",
                )
                self._command_tasks.add(task)
                task.add_done_callback(self._command_tasks.discard)
                continue
            if envelope.message_type == WorkerMessageType.TERMINAL:
                terminal = WorkerTerminalResult.model_validate_json(envelope.payload_json)
                self._validate_worker_terminal(terminal)
                await self._cancel_command_tasks()
                await controller_store_call(
                    self.store.path,
                    lambda store: store.mark_worker_job_status(
                        job_id=self.job_id,
                        status=WorkerJobStatus.FINISHING,
                        artifact_manifest=terminal.artifact_manifest,
                    ),
                    operation_name="worker_job_finishing",
                    priority=ControllerStorePriority.CRITICAL,
                )
                return terminal.finished
            if envelope.message_type == WorkerMessageType.WORKER_ERROR:
                error = WorkerError.model_validate_json(envelope.payload_json)
                raise WorkerReportedError(error)

    async def _handle_heartbeat(self, envelope: WorkerEnvelope) -> None:
        heartbeat = WorkerHeartbeat.model_validate_json(envelope.payload_json)
        now = time.monotonic()
        self._last_heartbeat_at = now
        state_key = _heartbeat_state_key(heartbeat)
        state_changed = state_key != self._last_heartbeat_state_key
        if not state_changed and now - self._last_heartbeat_persisted_at < _HEARTBEAT_PERSIST_INTERVAL_SECONDS:
            return
        self._last_heartbeat_persisted_at = now
        self._last_heartbeat_state_key = state_key
        payload = heartbeat.model_dump(mode="json")
        if self._heartbeat_batcher is None:
            await controller_store_call(
                self.store.path,
                lambda store: store.heartbeat_worker_job(
                    job_id=self.job_id,
                    worker_pid=heartbeat.worker_pid,
                    heartbeat_payload=payload,
                ),
                operation_name="worker_heartbeat",
                priority=ControllerStorePriority.BACKGROUND,
            )
            return
        self._heartbeat_batcher.publish(job_id=self.job_id, worker_pid=heartbeat.worker_pid, payload=payload)

    async def _handle_progress(self, envelope: WorkerEnvelope) -> None:
        if self.context.progress_sink is None:
            return
        try:
            event = WorkerProgressEvent.model_validate_json(envelope.payload_json)
            payload = _progress_payload(event)
        except Exception as exc:
            raise WorkerIPCError(f"invalid worker progress event: {exc}") from exc
        try:
            await self.context.progress_sink(event.message_type, payload)
        except Exception:
            logger.debug(
                "worker progress sink failed; continuing task execution",
                extra={"job_id": self.job_id, "task_id": self.context.request.task_id},
                exc_info=True,
            )

    async def _handle_command_dispatch(self, envelope: WorkerEnvelope, writer: asyncio.StreamWriter) -> None:
        request = WorkerCommandDispatchRequest.model_validate_json(envelope.payload_json)
        started = time.perf_counter()
        try:
            metadata_overrides = _safe_worker_metadata_overrides(request.metadata_overrides)
            command = build_command_request(
                lease=self.context.lease,
                metadata_overrides=metadata_overrides,
                tool_id=request.tool_id,
                parameters=request.parameters,
                command_id=request.command_id,
                timeout_seconds=request.timeout_seconds,
            )
            result = await self.sdk.send_command(command, trace_id=self.context.request.trace_id)
            response = WorkerCommandDispatchResponse(request_id=request.request_id, result=result)
        except Exception as exc:
            logger.debug(
                "worker command dispatch failed",
                extra={
                    "job_id": self.job_id,
                    "task_id": self.context.request.task_id,
                    "request_id": request.request_id,
                    "tool_id": request.tool_id,
                },
                exc_info=True,
            )
            response = WorkerCommandDispatchResponse(
                request_id=request.request_id,
                error=str(exc),
                error_code=(
                    WorkerCommandDispatchErrorCode.TOOL_PARAMETER_VALIDATION
                    if isinstance(exc, ToolParameterValidationError)
                    else None
                ),
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._command_roundtrip_ms.append(elapsed_ms)
        if response.result is None:
            self._command_failure_count += 1
        await self._record_metric(
            ProcessExecutorMetric(
                metric_name=ProcessExecutorMetricName.WORKER_COMMAND_ROUNDTRIP,
                job_id=self.job_id,
                task_id=self.context.request.task_id,
                attempt_id=self.attempt_id,
                value_ms=elapsed_ms,
                metadata={
                    "request_id": request.request_id,
                    "command_id": request.command_id,
                    "tool_id": request.tool_id,
                    "ok": response.result is not None,
                },
            ),
            persist=False,
        )
        await self._write(writer, WorkerMessageType.COMMAND_RESPONSE, response.model_dump_json())

    async def _record_metric(self, metric: ProcessExecutorMetric, *, persist: bool = True) -> None:
        log_process_metric(logger, metric)
        if not persist:
            return
        with contextlib.suppress(Exception):
            payload = metric.model_dump(mode="json")
            await controller_store_call(
                self.store.path,
                lambda store: store.append_event(
                    trace_id=self.context.request.trace_id,
                    entity_type="worker_job",
                    entity_id=self.job_id,
                    event_type="process_executor_metric",
                    payload=payload,
                ),
                operation_name="worker_metric",
                priority=ControllerStorePriority.BACKGROUND,
            )

    async def _record_worker_ready_metric(self) -> None:
        if self._ready_recorded:
            return
        self._ready_recorded = True
        await self._record_metric(
            ProcessExecutorMetric(
                metric_name=ProcessExecutorMetricName.WORKER_READY,
                job_id=self.job_id,
                task_id=self.context.request.task_id,
                attempt_id=self.attempt_id,
                value_ms=(time.perf_counter() - self._startup_started_at) * 1000,
                metadata={},
            )
        )

    async def _flush_command_roundtrip_summary(self) -> None:
        values = sorted(value for value in self._command_roundtrip_ms if value >= 0)
        if not values:
            return
        p50 = _percentile(values, 50)
        p95 = _percentile(values, 95)
        await self._record_metric(
            ProcessExecutorMetric(
                metric_name=ProcessExecutorMetricName.WORKER_COMMAND_ROUNDTRIP,
                job_id=self.job_id,
                task_id=self.context.request.task_id,
                attempt_id=self.attempt_id,
                value_ms=p95,
                metadata={
                    "summary": True,
                    "count": len(values),
                    "failed_count": self._command_failure_count,
                    "min_ms": round(values[0], 3),
                    "p50_ms": round(p50, 3),
                    "p95_ms": round(p95, 3),
                    "max_ms": round(values[-1], 3),
                },
            )
        )

    async def _run_command_dispatch(self, envelope: WorkerEnvelope, writer: asyncio.StreamWriter) -> None:
        async with self._command_slots:
            try:
                await self._handle_command_dispatch(envelope, writer)
            except Exception:
                logger.warning(
                    "worker command dispatch task failed",
                    extra={"job_id": self.job_id, "task_id": self.context.request.task_id},
                    exc_info=True,
                )

    def _validate_worker_command(self, command: CommandRequest) -> None:
        lease = self.context.lease
        if command.session_id != lease.session_id:
            raise PermissionError("worker command session_id does not match lease")
        if command.lease_id != lease.lease_id:
            raise PermissionError("worker command lease_id does not match lease")
        if command.device_id != lease.device_id:
            raise PermissionError("worker command device_id does not match lease")
        if command.lease_generation != lease.lease_generation:
            raise PermissionError("worker command lease_generation does not match lease")

    def _validate_worker_terminal(self, terminal: WorkerTerminalResult) -> None:
        finished = terminal.finished
        lease = self.context.lease
        expected = {
            "task_id": self.context.request.task_id,
            "trace_id": self.context.request.trace_id,
            "session_id": self.context.session_id,
            "device_id": lease.device_id,
            "lease_id": lease.lease_id,
        }
        actual = {
            "task_id": finished.task_id,
            "trace_id": finished.trace_id,
            "session_id": finished.session_id,
            "device_id": finished.device_id,
            "lease_id": finished.lease_id,
        }
        mismatched = [key for key, expected_value in expected.items() if actual[key] != expected_value]
        if mismatched:
            raise WorkerIPCError(f"worker terminal payload mismatch: {', '.join(mismatched)}")
        _validate_artifact_manifest_scope(terminal.artifact_manifest, self.artifact_root_dir_override)

    async def _monitor_heartbeat(self, process: asyncio.subprocess.Process) -> None:
        while True:
            await asyncio.sleep(self._worker_heartbeat_interval_seconds())
            if process.returncode is not None:
                return
            age = time.monotonic() - self._last_heartbeat_at
            if age > self.heartbeat_timeout_seconds:
                raise WorkerHeartbeatTimeoutError(f"worker heartbeat stale for {age:.1f}s")

    def _worker_heartbeat_interval_seconds(self) -> float:
        return max(0.5, min(5.0, self.heartbeat_timeout_seconds / 3.0))

    def _worker_command_response_timeout_seconds(self) -> float:
        configured_floor = self.command_timeout_floor_seconds
        floor = (
            max(
                _read_nonnegative_timeout(_COMMAND_TIMEOUT_FLOOR_ENV),
                _read_nonnegative_timeout(_LEGACY_COMMAND_TIMEOUT_FLOOR_ENV),
            )
            if configured_floor is None
            else configured_floor
        )
        short_floor = (
            _read_nonnegative_timeout(_COMMAND_SHORT_TIMEOUT_FLOOR_ENV) or _DEFAULT_SHORT_TIMEOUT_FLOOR_SECONDS
        )
        effective_floor = max(floor, short_floor if floor > 0 else 0.0)
        return max(1.0, self.command_timeout_seconds, effective_floor) + _COMMAND_RESPONSE_GRACE_SECONDS

    async def _write(self, writer: asyncio.StreamWriter, message_type: WorkerMessageType, payload_json: str) -> None:
        async with self._write_lock:
            await write_envelope(
                writer,
                WorkerEnvelope(
                    message_type=message_type,
                    trace_id=self.context.request.trace_id,
                    payload_json=payload_json,
                ),
            )

    async def _terminate_process(self, process: asyncio.subprocess.Process | None) -> None:
        await self._close_writer()
        if process is None or process.returncode is not None:
            return
        process.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)
            return
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    async def _cancel_command_tasks(self) -> None:
        tasks = [task for task in self._command_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _active_command_task_count(self) -> int:
        return sum(1 for task in self._command_tasks if not task.done())

    async def _close_writer(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _failure_payload(
        self,
        failure_kind: WorkerJobFailureKind,
        message: str,
        *,
        retryable: bool = True,
        exc: Exception | None = None,
    ) -> TaskFinished:
        logger.warning(
            "controller process worker failed",
            extra={
                "job_id": self.job_id,
                "task_id": self.context.request.task_id,
                "failure_kind": failure_kind.value,
                "error": message,
            },
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
        )
        await controller_store_call(
            self.store.path,
            lambda store: store.mark_worker_job_status(
                job_id=self.job_id,
                status=WorkerJobStatus.FINISHING,
                failure_kind=failure_kind,
                error=message,
            ),
            operation_name="worker_job_failure",
            priority=ControllerStorePriority.CRITICAL,
        )
        return TaskFinished(
            task_id=self.context.request.task_id,
            trace_id=self.context.request.trace_id,
            session_id=self.context.session_id,
            device_id=self.context.lease.device_id,
            lease_id=self.context.lease.lease_id,
            state=TaskState.FAILED,
            error=message,
            terminal_decision=AgentTerminalDecision.error(
                reason=AgentTerminalReason.RUNTIME_ERROR,
                final_answer=message,
                summary="The controller process worker failed before returning a terminal payload.",
                retryable=retryable,
                metadata={
                    "worker_job_id": self.job_id,
                    "worker_failure_kind": failure_kind.value,
                    "failure_category": "controller_submit",
                },
            ).to_json(),
        )


def _progress_payload(event: WorkerProgressEvent) -> object:
    if event.message_type == MessageType.COMMAND_REQUEST:
        return CommandRequest.model_validate_json(event.payload_json)
    if event.message_type == MessageType.COMMAND_RESULT:
        return CommandResult.model_validate_json(event.payload_json)
    if event.message_type == MessageType.TELEMETRY_UPDATE:
        return TelemetryUpdate.model_validate_json(event.payload_json)
    raise WorkerIPCError(f"unsupported worker progress message type: {event.message_type.value}")


def _heartbeat_state_key(heartbeat: WorkerHeartbeat) -> tuple[object, ...]:
    return (
        heartbeat.phase,
        heartbeat.step_index,
        heartbeat.active_command_id,
        heartbeat.artifact_root,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = int(round((percentile / 100.0) * (len(values) - 1)))
    return values[min(max(index, 0), len(values) - 1)]


def _read_nonnegative_timeout(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _read_tail(path: Path, *, limit_bytes: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit_bytes), os.SEEK_SET)
            data = handle.read(limit_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def _safe_worker_metadata_overrides(metadata: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in metadata.items() if key in _WORKER_METADATA_OVERRIDE_KEYS}


def _validate_artifact_manifest_scope(manifest, artifact_root_dir_override: str | None) -> None:
    if not manifest.artifact_root:
        return
    if not artifact_root_dir_override:
        raise WorkerIPCError("worker artifact manifest root is not allowed without artifact_root_dir_override")
    root = Path(manifest.artifact_root).resolve()
    allowed = Path(artifact_root_dir_override).resolve()
    if root != allowed and allowed not in root.parents:
        raise WorkerIPCError("worker artifact manifest root is outside configured artifact root")
    for raw_path in manifest.artifact_paths:
        path = Path(raw_path).resolve()
        if path != allowed and allowed not in path.parents:
            raise WorkerIPCError("worker artifact manifest path is outside configured artifact root")
