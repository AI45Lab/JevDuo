"""Device dispatch path for claimed controller commands."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from northcua.controller.control_plane.registry import DeviceRegistry
from northcua.controller.execution.command_results import CommandResultRecorder
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.policy.gui_resource import DEFAULT_GUI_RESOURCE_POLICY, GuiResourcePolicy
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call_profiled
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.runtime_sidecars import attach_runtime_sidecar, split_runtime_sidecar
from northcua_protocol.state import CommandState

DeviceSender = Callable[[CommandRequest, str], Awaitable[CommandResult]]
logger = logging.getLogger(__name__)
_CONTROLLER_DEVICE_RESULT_GRACE_FRACTION_ENV = "NORTHCUA_CONTROLLER_COMMAND_RESULT_GRACE_FRACTION"
_CONTROLLER_DEVICE_RESULT_GRACE_MAX_SECONDS_ENV = "NORTHCUA_CONTROLLER_COMMAND_RESULT_GRACE_MAX_SECONDS"
_CONTROLLER_DEVICE_RESULT_GRACE_FRACTION = 0.10
_CONTROLLER_DEVICE_RESULT_GRACE_MAX_SECONDS = 10.0


class CommandDispatcher:
    """Dispatch claimed commands and persist canonical results."""

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        events: ControllerEventLog,
        result_recorder: CommandResultRecorder,
        registry: DeviceRegistry | None,
        sender_by_device_id: dict[str, DeviceSender],
        gui_policy: GuiResourcePolicy = DEFAULT_GUI_RESOURCE_POLICY,
    ) -> None:
        self.store = store
        self.events = events
        self.result_recorder = result_recorder
        self.registry = registry
        self.sender_by_device_id = sender_by_device_id
        self.gui_policy = gui_policy
        self._gui_resource_locks: dict[str, asyncio.Lock] = {}

    async def dispatch(self, request: CommandRequest, *, trace_id: str) -> CommandResult:
        """Dispatch a command after its idempotency key has been claimed.

        Args:
            request: Claimed command request to send to the target device.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Controller-canonical command result.
        """

        self.events.write(
            "command_queued",
            trace_id=trace_id,
            entity_type="command",
            entity_id=request.command_id,
            device_id=request.device_id,
            tool_id=request.tool_id,
            attempt=request.attempt,
            lease_generation=request.lease_generation,
        )
        sender = self.sender_by_device_id.get(request.device_id)
        if sender is None:
            return await self._record_missing_sender(request, trace_id=trace_id)
        gui_resource_id = self._gui_resource_id_for(request)
        if gui_resource_id:
            lock = self._gui_resource_locks.setdefault(gui_resource_id, asyncio.Lock())
            if lock.locked():
                self.events.write(
                    "command_gui_resource_waiting",
                    trace_id=trace_id,
                    entity_type="command",
                    entity_id=request.command_id,
                    device_id=request.device_id,
                    tool_id=request.tool_id,
                    gui_resource_id=gui_resource_id,
                )
            wait_started_at = time.perf_counter()
            try:
                await asyncio.wait_for(lock.acquire(), timeout=request.timeout_seconds)
            except TimeoutError:
                return await self._record_timeout(request, trace_id=trace_id)
            try:
                waited_ms = int((time.perf_counter() - wait_started_at) * 1000)
                self.events.write(
                    "command_gui_resource_acquired",
                    trace_id=trace_id,
                    entity_type="command",
                    entity_id=request.command_id,
                    device_id=request.device_id,
                    tool_id=request.tool_id,
                    gui_resource_id=gui_resource_id,
                    waited_ms=waited_ms,
                )
                remaining = request.timeout_seconds - (time.perf_counter() - wait_started_at)
                if remaining <= 0:
                    return await self._record_timeout(request, trace_id=trace_id)
                return await self._dispatch_to_sender(
                    request.model_copy(update={"timeout_seconds": remaining}), sender=sender, trace_id=trace_id
                )
            finally:
                lock.release()
        return await self._dispatch_to_sender(request, sender=sender, trace_id=trace_id)

    async def _dispatch_to_sender(
        self,
        request: CommandRequest,
        *,
        sender: DeviceSender,
        trace_id: str,
    ) -> CommandResult:
        preempted = await self._mark_sent_and_running(request, trace_id=trace_id)
        if preempted is not None:
            return preempted
        if self.registry is not None:
            self.registry.reserve_command(request.device_id)
        started_at = time.perf_counter()
        logger.info(
            "controller command dispatch started",
            extra={
                "command_id": request.command_id,
                "session_id": request.session_id,
                "device_id": request.device_id,
                "tool_id": request.tool_id,
                "timeout_seconds": request.timeout_seconds,
            },
        )
        try:
            result = await asyncio.wait_for(
                sender(request, trace_id),
                timeout=_device_result_wait_timeout(request.timeout_seconds),
            )
        except asyncio.CancelledError:
            await self._record_dispatch_cancelled(request, trace_id=trace_id)
            raise
        except TimeoutError:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.warning(
                "controller command dispatch timed out",
                extra={
                    "command_id": request.command_id,
                    "session_id": request.session_id,
                    "device_id": request.device_id,
                    "tool_id": request.tool_id,
                    "timeout_seconds": request.timeout_seconds,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return await self._record_timeout(request, trace_id=trace_id)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.warning(
                "controller command sender raised before returning a result",
                extra={
                    "device_id": request.device_id,
                    "session_id": request.session_id,
                    "lease_id": request.lease_id,
                    "command_id": request.command_id,
                    "tool_id": request.tool_id,
                    "elapsed_ms": elapsed_ms,
                },
                exc_info=True,
            )
            return await self._record_dispatch_error(request, trace_id=trace_id, error=exc)
        finally:
            if self.registry is not None:
                self.registry.release_command(request.device_id)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        artifact_count, artifact_bytes = _artifact_metrics(result)
        self.events.write(
            "command_result_received",
            trace_id=trace_id,
            entity_type="command",
            entity_id=request.command_id,
            device_id=request.device_id,
            tool_id=request.tool_id,
            status=result.status.value,
            elapsed_ms=elapsed_ms,
            artifact_count=artifact_count,
            artifact_bytes=artifact_bytes,
        )
        if elapsed_ms > int(request.timeout_seconds * 1000):
            self.events.write(
                "command_result_arrived_during_controller_grace",
                trace_id=trace_id,
                entity_type="command",
                entity_id=request.command_id,
                device_id=request.device_id,
                tool_id=request.tool_id,
                timeout_seconds=request.timeout_seconds,
                elapsed_ms=elapsed_ms,
                status=result.status.value,
            )
        logger.info(
            "controller command dispatch completed",
            extra={
                "command_id": request.command_id,
                "session_id": request.session_id,
                "device_id": request.device_id,
                "tool_id": request.tool_id,
                "status": result.status.value,
                "elapsed_ms": elapsed_ms,
            },
        )
        persisted_payload, runtime_sidecar = split_runtime_sidecar(result.result)
        persisted_result = result.model_copy(update={"result": persisted_payload})
        result_task = asyncio.create_task(
            self.result_recorder.record_async_with_status(persisted_result, trace_id=trace_id)
        )
        try:
            canonical, persisted = await asyncio.shield(result_task)
        except asyncio.CancelledError:
            await result_task
            raise
        if not persisted or not runtime_sidecar:
            return canonical
        return canonical.model_copy(update={"result": attach_runtime_sidecar(canonical.result, runtime_sidecar)})

    def _gui_resource_id_for(self, request: CommandRequest) -> str:
        if self.registry is None:
            return ""
        device = self.registry.get(request.device_id)
        if device is None:
            return ""
        descriptor = self.registry.tool_descriptor(request.device_id, request.tool_id)
        return self.gui_policy.command_gui_resource_id(device=device, descriptor=descriptor)

    def record_result(self, result: CommandResult, *, trace_id: str) -> CommandResult:
        """Record a command result and return the controller-canonical result.

        Args:
            result: Command result received from a device or controller failure path.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Persisted result, cached duplicate result, or late-result projection.
        """

        return self.result_recorder.record(result, trace_id=trace_id)

    async def record_result_async(self, result: CommandResult, *, trace_id: str) -> CommandResult:
        """Record a device command result off the dispatch hot path.

        Args:
            result: Command result received from the device websocket.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Persisted result, cached duplicate result, or late-result projection.
        """

        return await self.result_recorder.record_async(result, trace_id=trace_id)

    async def _record_missing_sender(self, request: CommandRequest, *, trace_id: str) -> CommandResult:
        result = CommandResult(
            command_id=request.command_id,
            session_id=request.session_id,
            lease_id=request.lease_id,
            device_id=request.device_id,
            status=CommandState.FAILED,
            error="COMMAND_TRANSPORT_FAILED",
        )
        return await self.record_result_async(result, trace_id=trace_id)

    async def _mark_sent_and_running(self, request: CommandRequest, *, trace_id: str) -> CommandResult | None:
        dispatch_started_at = time.perf_counter()
        preempted, store_metrics = await controller_store_call_profiled(
            self.store.path,
            lambda store: store.mark_command_dispatched_for_request(request),
            operation_name="command_mark_running",
            priority=ControllerStorePriority.COMMAND,
        )
        status_update_ms = int((time.perf_counter() - dispatch_started_at) * 1000)
        if preempted is not None:
            self.events.write(
                "command_dispatch_preempted",
                trace_id=trace_id,
                entity_type="command",
                entity_id=request.command_id,
                device_id=request.device_id,
                tool_id=request.tool_id,
                status=preempted.status.value,
                status_update_ms=status_update_ms,
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
            return preempted
        for event_type, status in (
            ("command_sent", CommandState.SENT),
            ("command_running", CommandState.RUNNING),
        ):
            store_fields = (
                {
                    "store_operation": store_metrics.operation,
                    "store_mode": store_metrics.mode,
                    "store_queue_ms": store_metrics.queue_ms,
                    "store_admission_ms": store_metrics.admission_ms,
                    "store_executor_queue_ms": store_metrics.executor_queue_ms,
                    "store_operation_ms": store_metrics.operation_ms,
                    "store_exec_ms": store_metrics.exec_ms,
                    "store_sql_execute_ms": store_metrics.exec_ms,
                    "store_exec_scope": "execute_and_transaction_excluding_fetch",
                    "store_total_ms": store_metrics.total_ms,
                    "store_queue_depth": store_metrics.queue_depth,
                    "store_admitted_queue_depth": store_metrics.admitted_queue_depth,
                    "store_sql_count": store_metrics.sql_count,
                }
                if status == CommandState.RUNNING
                else {}
            )
            self.events.write(
                event_type,
                trace_id=trace_id,
                entity_type="command",
                entity_id=request.command_id,
                device_id=request.device_id,
                tool_id=request.tool_id,
                status=status.value,
                status_update_ms=status_update_ms,
                **store_fields,
            )
        return None

    async def _record_timeout(self, request: CommandRequest, *, trace_id: str) -> CommandResult:
        result = CommandResult(
            command_id=request.command_id,
            session_id=request.session_id,
            lease_id=request.lease_id,
            device_id=request.device_id,
            status=CommandState.TIMED_OUT,
            error="COMMAND_TIMED_OUT",
        )
        canonical = await self.record_result_async(result, trace_id=trace_id)
        self.events.write(
            "command_timed_out",
            trace_id=trace_id,
            entity_type="command",
            entity_id=request.command_id,
            timeout_seconds=request.timeout_seconds,
        )
        return canonical

    async def _record_dispatch_error(
        self,
        request: CommandRequest,
        *,
        trace_id: str,
        error: Exception,
    ) -> CommandResult:
        result = CommandResult(
            command_id=request.command_id,
            session_id=request.session_id,
            lease_id=request.lease_id,
            device_id=request.device_id,
            status=CommandState.FAILED,
            error="COMMAND_TRANSPORT_FAILED",
        )
        canonical = await self.record_result_async(result, trace_id=trace_id)
        self.events.write(
            "command_failed",
            trace_id=trace_id,
            entity_type="command",
            entity_id=request.command_id,
            error=str(error),
        )
        return canonical

    async def _record_dispatch_cancelled(self, request: CommandRequest, *, trace_id: str) -> CommandResult:
        result = CommandResult(
            command_id=request.command_id,
            session_id=request.session_id,
            lease_id=request.lease_id,
            device_id=request.device_id,
            status=CommandState.CANCELLED,
            error="COMMAND_DISPATCH_CANCELLED",
        )
        record_task = asyncio.create_task(self.record_result_async(result, trace_id=trace_id))
        try:
            canonical = await asyncio.shield(record_task)
        except asyncio.CancelledError:
            canonical = await record_task
            raise
        self.events.write(
            "command_cancelled",
            trace_id=trace_id,
            entity_type="command",
            entity_id=request.command_id,
            reason="dispatch task cancelled",
        )
        return canonical


__all__ = ["CommandDispatcher", "DeviceSender"]


def _device_result_wait_timeout(command_timeout_seconds: float) -> float:
    """Return controller wait budget, giving device-side timeout a small lead."""

    grace_fraction = _read_nonnegative_float_env(
        _CONTROLLER_DEVICE_RESULT_GRACE_FRACTION_ENV,
        default=_CONTROLLER_DEVICE_RESULT_GRACE_FRACTION,
    )
    grace_max_seconds = _read_nonnegative_float_env(
        _CONTROLLER_DEVICE_RESULT_GRACE_MAX_SECONDS_ENV,
        default=_CONTROLLER_DEVICE_RESULT_GRACE_MAX_SECONDS,
    )
    grace_seconds = min(
        grace_max_seconds,
        max(0.0, command_timeout_seconds) * grace_fraction,
    )
    return command_timeout_seconds + grace_seconds


def _read_nonnegative_float_env(name: str, *, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _artifact_metrics(result: CommandResult) -> tuple[int, int]:
    artifacts = list(result.artifacts or [])
    return len(artifacts), sum(int(artifact.size_bytes or 0) for artifact in artifacts)
