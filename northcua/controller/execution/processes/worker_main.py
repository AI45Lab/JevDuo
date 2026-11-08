"""Worker process entrypoint for controller-owned task execution."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel

from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.processes.contracts import (
    WorkerEnvelope,
    WorkerError,
    WorkerHello,
    WorkerJobFailureKind,
    WorkerMessageType,
    WorkerProgressEvent,
    WorkerRunRequest,
    WorkerTerminalResult,
    artifact_manifest_from_root,
)
from northcua.controller.execution.processes.ipc import read_envelope, write_envelope
from northcua.controller.execution.processes.runners import ProcessWorkerRunnerRegistry
from northcua.controller.execution.processes.runners.contracts import ProcessWorkerRunnerBuildContext
from northcua.controller.execution.processes.worker_client import WorkerIPCClient, worker_heartbeat
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import TaskFinished, TelemetryUpdate

logger = logging.getLogger(__name__)


class _RuntimeStateUpdate(TypedDict, total=False):
    phase: str
    step_index: int
    active_command_id: str


class _WorkerStartupPhaseReporter:
    """Track startup phase without letting sync bootstrap silence heartbeats."""

    def __init__(self, *, ipc: WorkerIPCClient, request: WorkerRunRequest) -> None:
        self._ipc = ipc
        self._request = request
        self._phase = "running"
        self._step_index: int | None = None
        self._active_command_id = ""
        self._loop = asyncio.get_running_loop()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def phase(self) -> str:
        """Return the latest startup phase reported by this worker.

        Returns:
            Current startup phase label used in worker heartbeats.
        """

        return self._phase

    @property
    def step_index(self) -> int | None:
        """Return the latest known runtime step index, if any."""

        return self._step_index

    @property
    def active_command_id(self) -> str:
        """Return the command currently blocking this worker, if known."""

        return self._active_command_id

    async def set_phase(self, phase: str) -> None:
        """Publish a startup phase from the worker event loop.

        Args:
            phase: Human-readable startup phase name.
        """

        if self._closed:
            return
        self._phase = phase
        await self._send_phase_update(phase)

    async def set_runtime_state(
        self,
        *,
        phase: str | None = None,
        step_index: int | None = None,
        active_command_id: str | None = None,
    ) -> None:
        """Publish runtime state that should appear in subsequent heartbeats."""

        if self._closed:
            return
        if step_index is not None and self._step_index is not None and step_index < self._step_index:
            return
        if phase is not None:
            self._phase = phase
        if step_index is not None:
            self._step_index = step_index
        if active_command_id is not None:
            self._active_command_id = active_command_id
        await self._send_heartbeat()

    async def observe_progress(self, message_type: MessageType, payload: object) -> None:
        """Update heartbeat state from task-local telemetry progress."""

        state = _worker_state_from_progress(message_type, payload)
        if not state:
            return
        try:
            await self.set_runtime_state(**state)
        except Exception:
            logger.debug(
                "worker_progress_heartbeat_state_update_failed",
                extra={"task_id": self._request.request.task_id, "session_id": self._request.session_id},
                exc_info=True,
            )

    async def set_active_command(self, command_id: str) -> None:
        """Record an active command and immediately publish a heartbeat."""

        await self.set_runtime_state(phase="command_dispatch", active_command_id=command_id)

    async def clear_active_command(self, command_id: str) -> None:
        """Clear an active command only when it still matches."""

        if self._active_command_id != command_id:
            return
        await self.set_runtime_state(phase="command_finished", active_command_id="")

    def set_phase_from_thread(self, phase: str) -> None:
        """Schedule a startup phase update from synchronous bootstrap code.

        Args:
            phase: Human-readable startup phase name.
        """

        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._schedule_phase_update, phase)
        except RuntimeError:
            return

    def _schedule_phase_update(self, phase: str) -> None:
        if self._closed:
            return
        self._phase = phase
        task = asyncio.create_task(
            self._send_phase_update(phase),
            name=f"northcua-worker-startup-phase:{phase}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._observe_phase_update)

    def _observe_phase_update(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.result()

    async def close(self) -> None:
        """Cancel any pending phase-update tasks before worker shutdown."""

        self._closed = True
        if not self._tasks:
            return
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()

    async def _send_phase_update(self, phase: str) -> None:
        try:
            await self._send_heartbeat()
            await self._ipc.send_progress(
                WorkerProgressEvent(
                    message_type=MessageType.TELEMETRY_UPDATE,
                    payload_json=TelemetryUpdate(
                        events=[
                            {
                                "event_type": "worker_startup_phase",
                                "phase": phase,
                                "task_id": self._request.request.task_id,
                                "trace_id": self._request.request.trace_id,
                                "session_id": self._request.session_id,
                                "worker_pid": os.getpid(),
                                "artifact_root": self._request.artifact_root_dir_override or "",
                            }
                        ]
                    ).model_dump_json(),
                )
            )
        except Exception:
            logger.debug(
                "worker_startup_phase_update_failed",
                extra={
                    "task_id": self._request.request.task_id,
                    "session_id": self._request.session_id,
                    "phase": phase,
                },
                exc_info=True,
            )

    async def _send_heartbeat(self) -> None:
        await self._ipc.send_heartbeat(
            worker_heartbeat(
                phase=self._phase,
                artifact_root=self._request.artifact_root_dir_override or "",
                active_command_id=self._active_command_id,
                step_index=self._step_index,
            )
        )


def _worker_state_from_progress(message_type: MessageType, payload: object) -> _RuntimeStateUpdate:
    if message_type != MessageType.TELEMETRY_UPDATE or not isinstance(payload, TelemetryUpdate):
        return {}
    for event in payload.events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type == "desktop_worker_runtime_phase":
            phase = str(event.get("phase") or "")
            step_index = _coerce_optional_int(event.get("step_index"))
            state: _RuntimeStateUpdate = {}
            if phase:
                state["phase"] = phase
            if step_index is not None:
                state["step_index"] = step_index
            return state
        if event_type == "desktop_step":
            step = event.get("step")
            step_index = _coerce_optional_int(step.get("step_index") if isinstance(step, dict) else None)
            state: _RuntimeStateUpdate = {"phase": "step_finished"}
            if step_index is not None:
                state["step_index"] = step_index
            return state
    return {}


def _progress_is_worker_heartbeat_only(message_type: MessageType, payload: object) -> bool:
    if message_type != MessageType.TELEMETRY_UPDATE or not isinstance(payload, TelemetryUpdate):
        return False
    if not payload.events:
        return False
    for event in payload.events:
        if not isinstance(event, dict):
            return False
        if event.get("event_type") != "desktop_worker_runtime_phase":
            return False
        if not bool(event.get("worker_heartbeat_only")):
            return False
    return True


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        integer = int(value)
        return integer if integer > 0 else None
    if isinstance(value, str):
        try:
            as_float = float(value.strip())
        except ValueError:
            return None
        if as_float.is_integer():
            integer = int(as_float)
            return integer if integer > 0 else None
    return None


async def _run_worker(socket_path: str, *, nonce: str) -> int:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await write_envelope(
        writer,
        WorkerEnvelope(
            message_type=WorkerMessageType.HELLO,
            payload_json=WorkerHello(nonce=nonce, worker_pid=os.getpid()).model_dump_json(),
        ),
    )
    run_envelope = await read_envelope(reader)
    if run_envelope.message_type != WorkerMessageType.RUN_REQUEST:
        raise RuntimeError(f"expected run_request, got {run_envelope.message_type.value}")
    request = WorkerRunRequest.model_validate_json(run_envelope.payload_json)
    ipc = WorkerIPCClient(reader=reader, writer=writer, trace_id=request.request.trace_id)
    await ipc.start()
    await ipc.send_payload(WorkerMessageType.READY, "{}")
    phase_reporter = _WorkerStartupPhaseReporter(ipc=ipc, request=request)
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(ipc, request, phase_reporter),
        name="northcua-worker-heartbeat",
    )
    try:
        finished = await _execute_while_control_plane_alive(
            ipc,
            heartbeat_task,
            _execute_request(ipc, request, phase_reporter=phase_reporter),
        )
        manifest = await asyncio.to_thread(
            artifact_manifest_from_root,
            _artifact_root_from_finished(finished.artifacts, request),
        )
        terminal = WorkerTerminalResult(finished=finished, artifact_manifest=manifest)
        await ipc.send_payload(WorkerMessageType.TERMINAL, terminal.model_dump_json())
        return 0
    except Exception as exc:
        logger.exception("worker task execution failed")
        with contextlib.suppress(Exception):
            await ipc.send_payload(
                WorkerMessageType.WORKER_ERROR,
                WorkerError(
                    failure_kind=WorkerJobFailureKind.TASK_FAILED,
                    message=str(exc),
                    retryable=True,
                ).model_dump_json(),
            )
        return 1
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await phase_reporter.close()
        await ipc.close()


async def _execute_while_control_plane_alive(
    ipc: WorkerIPCClient,
    heartbeat_task: asyncio.Task[None],
    execution: Awaitable[TaskFinished],
) -> TaskFinished:
    """Cancel task execution as soon as the controller control plane is lost.

    Args:
        ipc: Connected worker IPC client exposing its lifecycle signal.
        heartbeat_task: Heartbeat loop whose failure also means the control
            plane can no longer supervise this attempt.
        execution: Runner execution for the current attempt.

    Returns:
        The runner's terminal result when execution wins the race.

    Raises:
        RuntimeError: If IPC or heartbeat supervision ends before execution.
        Exception: Any error raised by the runner execution.
    """

    execution_task = asyncio.ensure_future(execution)
    disconnect_task = asyncio.create_task(
        ipc.wait_disconnected(),
        name="northcua-worker-ipc-disconnect-monitor",
    )
    try:
        done, _pending = await asyncio.wait(
            {execution_task, disconnect_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if execution_task in done:
            return await execution_task
        if disconnect_task in done:
            await disconnect_task
            raise RuntimeError("worker IPC disconnect monitor stopped unexpectedly")
        await heartbeat_task
        raise RuntimeError("worker heartbeat stopped unexpectedly")
    finally:
        if not execution_task.done():
            execution_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution_task
        if not disconnect_task.done():
            disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnect_task


async def _execute_request(
    ipc: WorkerIPCClient,
    request: WorkerRunRequest,
    *,
    phase_reporter: _WorkerStartupPhaseReporter,
    runner_registry: ProcessWorkerRunnerRegistry | None = None,
) -> TaskFinished:
    """Execute one deserialized run request in the worker process.

    Args:
        ipc: Worker IPC client used for command dispatch and progress.
        request: Controller-supplied task context and lease.

    Returns:
        Terminal task payload returned by the selected remote runner.
    """

    registry = runner_registry or ProcessWorkerRunnerRegistry.default()
    factory = registry.resolve(request.request.agent_kind)
    if factory is None:
        supported = ", ".join(registry.supported_agent_kinds()) or "none"
        raise RuntimeError(
            f"process executor worker does not support agent_kind: {request.request.agent_kind}; supported: {supported}"
        )
    await phase_reporter.set_phase("importing_runner")

    async def progress_sink(message_type: MessageType, payload: object) -> None:
        """Forward runner progress over the worker IPC channel.

        Args:
            message_type: Protocol message type emitted by the runner.
            payload: Pydantic payload to serialize for the supervisor.
        """

        await phase_reporter.observe_progress(message_type, payload)
        if _progress_is_worker_heartbeat_only(message_type, payload):
            return
        payload_json = payload.model_dump_json() if isinstance(payload, BaseModel) else "{}"
        await ipc.send_progress(WorkerProgressEvent(message_type=message_type, payload_json=payload_json))

    runner = await factory.build(
        ProcessWorkerRunnerBuildContext(
            request=request,
            ipc=ipc,
            phase_reporter=phase_reporter,
        )
    )
    context = ControllerExecutionContext(
        request=request.request,
        session_id=request.session_id,
        lease=request.lease,
        blob_store_root=Path(request.blob_store_root) if request.blob_store_root else None,
        progress_sink=progress_sink,
    )
    await phase_reporter.set_phase("runner_constructed")
    return await runner.run(context)


async def _heartbeat_loop(
    ipc: WorkerIPCClient,
    request: WorkerRunRequest,
    phase_reporter: _WorkerStartupPhaseReporter,
) -> None:
    """Send worker liveness samples until the worker is canceled.

    Args:
        ipc: Worker IPC client connected to the supervisor.
        request: Run request containing heartbeat interval and artifact root.
    """

    while True:
        await ipc.send_heartbeat(
            worker_heartbeat(
                phase=phase_reporter.phase,
                artifact_root=request.artifact_root_dir_override or "",
                active_command_id=phase_reporter.active_command_id,
                step_index=phase_reporter.step_index,
            )
        )
        await asyncio.sleep(request.heartbeat_interval_seconds)


def _artifact_root_from_finished(artifacts: object, request: WorkerRunRequest) -> Path | None:
    """Resolve the artifact root that should be scanned at terminal time.

    Args:
        artifacts: Terminal artifact payload, accepted for future routing.
        request: Run request containing the artifact root override.

    Returns:
        Session artifact root, or ``None`` when no root was configured.
    """

    del artifacts
    if request.artifact_root_dir_override:
        return Path(request.artifact_root_dir_override) / request.session_id
    return None


def main() -> int:
    """Run one process-executor worker.

    Returns:
        Process exit code from the async worker entrypoint.
    """

    parser = argparse.ArgumentParser(description="NorthCUA controller process worker")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    return asyncio.run(_run_worker(args.socket, nonce=args.nonce))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
