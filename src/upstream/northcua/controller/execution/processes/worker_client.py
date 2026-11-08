"""Worker-side IPC client and command transport."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import sys
import uuid
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from northcua.controller.execution.processes.contracts import (
    WorkerCommandDispatchErrorCode,
    WorkerCommandDispatchRequest,
    WorkerCommandDispatchResponse,
    WorkerEnvelope,
    WorkerHeartbeat,
    WorkerMessageType,
    WorkerProgressEvent,
)
from northcua.controller.execution.processes.ipc import read_envelope, write_envelope
from northcua.controller.execution.processes.transport import CommandTransport
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import CommandRequest, CommandResult, SessionLeaseGranted
from northcua_runtime_shared.validation import ToolParameterValidationError

logger = logging.getLogger(__name__)
_HEARTBEAT_STATE_REPORT_TIMEOUT_SECONDS = 0.25


class WorkerIPCClient:
    """Worker-side client for the supervisor IPC server."""

    def __init__(self, *, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, trace_id: str) -> None:
        self.reader = reader
        self.writer = writer
        self.trace_id = trace_id
        self._write_lock = asyncio.Lock()
        self._response_futures: dict[str, asyncio.Future[WorkerCommandDispatchResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._closing = False
        self._disconnected = asyncio.Event()
        self._disconnect_error: BaseException | None = None

    async def start(self) -> None:
        """Start routing supervisor responses."""

        self._reader_task = asyncio.create_task(self._read_loop(), name="northcua-worker-ipc-reader")

    async def close(self) -> None:
        """Close the worker IPC connection."""

        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._fail_pending(RuntimeError("worker IPC client closed"))
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()

    async def wait_disconnected(self) -> None:
        """Wait until the controller control plane is no longer available.

        The worker entrypoint races this lifecycle signal against task
        execution.  This prevents a controller restart or IPC EOF from
        leaving an orphan worker that continues model calls or artifact
        writes after the supervisor has started a replacement attempt.

        Raises:
            RuntimeError: Always, unless the client is already performing an
                intentional local close.
        """

        await self._disconnected.wait()
        if self._closing:
            return
        if isinstance(self._disconnect_error, asyncio.CancelledError):
            raise RuntimeError("worker IPC canceled by controller") from self._disconnect_error
        if self._disconnect_error is not None:
            raise RuntimeError("worker IPC disconnected from controller") from self._disconnect_error
        raise RuntimeError("worker IPC reader stopped unexpectedly")

    async def send_command(self, request: WorkerCommandDispatchRequest) -> WorkerCommandDispatchResponse:
        """Send a command dispatch request and wait for a controller response.

        Args:
            request: Worker command request to send to the supervisor.

        Returns:
            Controller command response matching ``request.request_id``.

        Raises:
            TimeoutError: If the supervisor does not answer within the command
                response budget.
        """

        future: asyncio.Future[WorkerCommandDispatchResponse] = asyncio.get_running_loop().create_future()
        self._response_futures[request.request_id] = future
        await self.send_payload(WorkerMessageType.COMMAND_DISPATCH, request.model_dump_json())
        try:
            timeout = max(1.0, request.timeout_seconds + 5.0, request.response_timeout_seconds)
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"worker IPC command response timed out: {request.command_id}") from exc
        finally:
            self._response_futures.pop(request.request_id, None)

    async def send_progress(self, event: WorkerProgressEvent) -> None:
        """Send one best-effort progress event.

        Args:
            event: Validated progress event to forward.
        """

        await self.send_payload(WorkerMessageType.PROGRESS, event.model_dump_json())

    async def send_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        """Send one worker heartbeat.

        Args:
            heartbeat: Worker liveness and resource payload.
        """

        await self.send_payload(WorkerMessageType.HEARTBEAT, heartbeat.model_dump_json(exclude_none=True))

    async def send_payload(self, message_type: WorkerMessageType, payload_json: str) -> None:
        """Send one typed worker envelope.

        Args:
            message_type: IPC envelope discriminator.
            payload_json: Serialized payload for the envelope.

        Raises:
            RuntimeError: If the IPC client has already closed.
        """

        async with self._write_lock:
            if self._closed:
                raise RuntimeError("worker IPC client is closed")
            await write_envelope(
                self.writer,
                WorkerEnvelope(message_type=message_type, trace_id=self.trace_id, payload_json=payload_json),
            )

    async def _read_loop(self) -> None:
        try:
            while True:
                envelope = await read_envelope(self.reader)
                if envelope.message_type == WorkerMessageType.CANCEL:
                    raise asyncio.CancelledError("worker task canceled by controller")
                if envelope.message_type != WorkerMessageType.COMMAND_RESPONSE:
                    continue
                response = WorkerCommandDispatchResponse.model_validate_json(envelope.payload_json)
                future = self._response_futures.get(response.request_id)
                if future is not None and not future.done():
                    future.set_result(response)
        except asyncio.CancelledError:
            exc = asyncio.CancelledError("worker IPC reader canceled")
            if not self._closing:
                self._disconnect_error = exc
            self._fail_pending(exc)
            raise
        except Exception as exc:
            logger.debug("worker IPC read loop failed", exc_info=True)
            self._disconnect_error = exc
            self._fail_pending(exc)
            raise
        finally:
            self._disconnected.set()

    def _fail_pending(self, exc: BaseException) -> None:
        self._closed = True
        for future in list(self._response_futures.values()):
            if not future.done():
                future.set_exception(exc)


class WorkerHeartbeatStateReporter(Protocol):
    """Optional worker heartbeat state sink used by command dispatch."""

    async def set_active_command(self, command_id: str) -> None:
        """Record that command dispatch is blocking the worker."""

    async def clear_active_command(self, command_id: str) -> None:
        """Clear the blocking command when it is still current."""


class WorkerCommandTransport(CommandTransport):
    """Command transport that asks the controller supervisor to dispatch commands."""

    def __init__(
        self,
        *,
        ipc: WorkerIPCClient,
        lease: SessionLeaseGranted,
        metadata_overrides: Mapping[str, object] | None = None,
        command_response_timeout_seconds: float = 0.0,
        heartbeat_state_reporter: WorkerHeartbeatStateReporter | None = None,
    ) -> None:
        self.ipc = ipc
        self._lease = lease
        self._metadata_overrides: Mapping[str, object] = dict(metadata_overrides or {})
        self._command_response_timeout_seconds = max(0.0, float(command_response_timeout_seconds))
        self._heartbeat_state_reporter = heartbeat_state_reporter

    @property
    def lease(self) -> SessionLeaseGranted:
        """Return the controller lease mirrored into the worker process.

        Returns:
            Lease that scopes all worker command dispatches.
        """

        return self._lease

    @property
    def metadata_overrides(self) -> Mapping[str, object]:
        """Return safe command metadata overrides requested by the runner.

        Returns:
            Allowlisted metadata merged into each command.
        """

        return self._metadata_overrides

    async def send_command(self, command: CommandRequest, *, trace_id: str) -> CommandResult:
        """Send a command dispatch request through worker IPC.

        Args:
            command: Runner command request to dispatch through the controller.
            trace_id: Trace id accepted for parity with the in-process transport.

        Returns:
            Canonical command result returned by the controller.

        Raises:
            RuntimeError: If the supervisor reports a dispatch error.
        """

        del trace_id
        if self._heartbeat_state_reporter is not None:
            await _best_effort_report_command_state(
                self._heartbeat_state_reporter.set_active_command(command.command_id),
                event_name="worker_command_active_heartbeat_failed",
            )
        try:
            response = await self.ipc.send_command(
                WorkerCommandDispatchRequest(
                    request_id=uuid.uuid4().hex,
                    tool_id=command.tool_id,
                    parameters=command.parameters,
                    command_id=command.command_id,
                    timeout_seconds=command.timeout_seconds,
                    response_timeout_seconds=max(command.timeout_seconds, self._command_response_timeout_seconds),
                    metadata_overrides=dict(self.metadata_overrides),
                )
            )
        finally:
            if self._heartbeat_state_reporter is not None:
                await _best_effort_report_command_state(
                    self._heartbeat_state_reporter.clear_active_command(command.command_id),
                    event_name="worker_command_clear_heartbeat_failed",
                )
        if response.result is None:
            if response.error_code == WorkerCommandDispatchErrorCode.TOOL_PARAMETER_VALIDATION:
                raise ToolParameterValidationError(response.error or "tool parameters failed descriptor validation")
            raise RuntimeError(response.error or "worker command dispatch failed")
        return response.result

    async def emit_progress(self, message_type: MessageType, payload: object) -> None:
        """Send progress to the controller supervisor.

        Args:
            message_type: Protocol message type to forward.
            payload: Pydantic payload; non-model values are dropped to an empty
                object to keep progress forwarding schema-safe.
        """

        payload_json = payload.model_dump_json() if isinstance(payload, BaseModel) else "{}"
        await self.ipc.send_progress(WorkerProgressEvent(message_type=message_type, payload_json=payload_json))


async def _best_effort_report_command_state(coro: Awaitable[None], *, event_name: str) -> None:
    try:
        await asyncio.wait_for(coro, timeout=_HEARTBEAT_STATE_REPORT_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.debug(
            event_name,
            extra={"timeout_seconds": _HEARTBEAT_STATE_REPORT_TIMEOUT_SECONDS},
        )
    except Exception:
        logger.debug(event_name, exc_info=True)


def worker_heartbeat(
    *,
    phase: str,
    artifact_root: str = "",
    active_command_id: str = "",
    step_index: int | None = None,
) -> WorkerHeartbeat:
    """Build a lightweight worker heartbeat.

    Args:
        phase: Coarse worker phase for diagnostics.
        artifact_root: Artifact root currently used by the worker.
        active_command_id: Command currently blocking the worker.
        step_index: Agent step index when known, otherwise ``None``.

    Returns:
        Heartbeat payload with process id, memory, and CPU counters.
    """

    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss_bytes = int(usage.ru_maxrss) * (1 if sys.platform == "darwin" else 1024)
    current_rss_bytes = _current_rss_bytes()
    return WorkerHeartbeat(
        phase=phase,
        step_index=step_index,
        active_command_id=active_command_id,
        artifact_root=artifact_root,
        worker_pid=os.getpid(),
        rss_bytes=peak_rss_bytes,
        peak_rss_bytes=peak_rss_bytes,
        current_rss_bytes=current_rss_bytes,
        cpu_time_seconds=float(usage.ru_utime + usage.ru_stime),
    )


def _current_rss_bytes() -> int:
    """Return the current resident set size for this worker process."""

    if sys.platform != "linux":
        return 0
    try:
        statm = Path("/proc/self/statm").read_text(encoding="utf-8").split()
        if len(statm) >= 2:
            return int(statm[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        logger.debug("worker_current_rss_sample_failed", exc_info=True)
    return 0
