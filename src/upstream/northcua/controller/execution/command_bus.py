"""Controller command bus with idempotency and command result tracking."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping

from northcua.controller.auth import ControllerAuth
from northcua.controller.control_plane.registry import DeviceRegistry
from northcua.controller.execution.command_artifacts import CommandArtifactMaterializer
from northcua.controller.execution.command_authority import validate_active_lease, validate_and_claim_active_command
from northcua.controller.execution.command_dispatcher import CommandDispatcher, DeviceSender
from northcua.controller.execution.command_idempotency import command_idempotency_key
from northcua.controller.execution.command_results import CommandResultRecorder
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import (
    ControllerStoreCallMetrics,
    ControllerStorePriority,
    controller_store_call_profiled,
    controller_store_read_profiled,
)
from northcua.controller.storage.blob_store import LocalControllerBlobStore
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.capabilities import ToolDescriptor
from northcua_protocol.payloads import AGENTKIT_SETTINGS_PARAM, CommandRequest, CommandResult
from northcua_protocol.state import CommandState

_COMMAND_TIMEOUT_FLOOR_ENV = "NORTHCUA_CONTROLLER_COMMAND_TIMEOUT_FLOOR_SECONDS"
_LEGACY_COMMAND_TIMEOUT_FLOOR_ENV = "NORTHCUA_ACTION_COMMAND_TIMEOUT_SECONDS"
_COMMAND_SHORT_TIMEOUT_FLOOR_ENV = "NORTHCUA_CONTROLLER_COMMAND_SHORT_TIMEOUT_FLOOR_SECONDS"
_DEFAULT_SHORT_TIMEOUT_FLOOR_SECONDS = 90.0
_LONG_DESCRIPTOR_TIMEOUT_THRESHOLD_SECONDS = 120.0


class CommandBus:
    """Send commands to devices through a pluggable sender."""

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        auth: ControllerAuth,
        events: ControllerEventLog,
        registry: DeviceRegistry | None = None,
        blob_store: LocalControllerBlobStore | None = None,
        max_inline_artifact_bytes: int = 4_000_000,
        command_timeout_floor_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.auth = auth
        self.events = events
        self.registry = registry
        self.blob_store = blob_store
        self.max_inline_artifact_bytes = max_inline_artifact_bytes
        self.command_timeout_floor_seconds = (
            _command_timeout_floor_seconds()
            if command_timeout_floor_seconds is None
            else max(0.0, float(command_timeout_floor_seconds))
        )
        self._result_recorder = CommandResultRecorder(
            store=store,
            events=events,
            artifacts=CommandArtifactMaterializer(
                store=store,
                blob_store=blob_store,
                max_inline_artifact_bytes=max_inline_artifact_bytes,
            ),
        )
        self._sender_by_device_id: dict[str, DeviceSender] = {}
        self._dispatcher = CommandDispatcher(
            store=store,
            events=events,
            result_recorder=self._result_recorder,
            registry=registry,
            sender_by_device_id=self._sender_by_device_id,
        )
        self._inflight_by_idempotency_key: dict[str, asyncio.Task[CommandResult]] = {}
        self._claim_locks_by_idempotency_key: dict[str, asyncio.Lock] = {}

    def register_sender(self, device_id: str, sender: DeviceSender) -> None:
        """Register an async sender for a device.

        Args:
            device_id: Device id whose websocket sender is being installed.
            sender: Async callable that delivers commands and returns results.
        """

        self._sender_by_device_id[device_id] = sender

    async def send(self, request: CommandRequest, *, trace_id: str) -> CommandResult:
        """Persist and dispatch one command request.

        Args:
            request: Command request to validate, claim, and deliver.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Command result from cache, device, or controller-side failure path.

        Raises:
            PermissionError: If device, tool, lease, or policy checks fail.
            ValueError: If tool id or parameter validation fails.
        """

        started_at = time.perf_counter()
        descriptor = None
        if self.registry is not None:
            device = self.registry.get(request.device_id)
            if device is None:
                raise PermissionError("command device is not registered")
            descriptor = self.registry.tool_descriptor(request.device_id, request.tool_id)
            if descriptor is None:
                raise ValueError(f"unknown tool_id for device {request.device_id}: {request.tool_id}")
        self.auth.validate_command(request, descriptor=descriptor)
        request = self._cap_timeout_by_descriptor(request, descriptor=descriptor, trace_id=trace_id)
        lock = self._claim_locks_by_idempotency_key.setdefault(request.idempotency_key, asyncio.Lock())
        task: asyncio.Task[CommandResult] | None = None
        wait_for_task: asyncio.Task[CommandResult] | None = None
        immediate_result: CommandResult | None = None
        claim_created = False
        try:
            async with lock:
                inflight = self._inflight_by_idempotency_key.get(request.idempotency_key)
                if inflight is not None:
                    _validated, validation_metrics = await controller_store_read_profiled(
                        self.store.path,
                        lambda store: validate_active_lease(store, request),
                        operation_name="command_validate_duplicate",
                    )
                    self.events.write(
                        "command_duplicate_in_flight_waited",
                        trace_id=trace_id,
                        entity_type="command",
                        entity_id=request.command_id,
                        validation_ms=validation_metrics.total_ms,
                        preclaim_elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                        inflight_commands=len(self._inflight_by_idempotency_key),
                        **_store_metric_fields(validation_metrics),
                    )
                    wait_for_task = inflight
                else:
                    claim_task = asyncio.create_task(
                        controller_store_call_profiled(
                            self.store.path,
                            lambda store: validate_and_claim_active_command(store, request),
                            operation_name="command_validate_and_claim",
                            priority=ControllerStorePriority.COMMAND,
                        )
                    )
                    try:
                        claim_outcome, claim_metrics = await asyncio.shield(claim_task)
                    except asyncio.CancelledError:
                        claim_outcome, claim_metrics = await claim_task
                        if claim_outcome.claim == "new":
                            await self._cancel_claimed_command_after_cancellation(request, trace_id=trace_id)
                        raise
                    claim = claim_outcome.claim
                    cached = claim_outcome.cached
                    validation_ms = claim_outcome.validation_exec_ms
                    claim_ms = claim_outcome.claim_exec_ms
                    store_metrics = _store_metric_fields(claim_metrics)
                    if cached is not None:
                        self.events.write(
                            "command_deduped",
                            trace_id=trace_id,
                            entity_type="command",
                            entity_id=request.command_id,
                            validation_ms=validation_ms,
                            claim_ms=claim_ms,
                            predispatch_elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                            **store_metrics,
                        )
                        immediate_result = cached
                    elif claim == "in_flight":
                        self.events.write(
                            "command_duplicate_in_flight_rejected",
                            trace_id=trace_id,
                            entity_type="command",
                            entity_id=request.command_id,
                            validation_ms=validation_ms,
                            claim_ms=claim_ms,
                            predispatch_elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                            **store_metrics,
                        )
                        immediate_result = CommandResult(
                            command_id=request.command_id,
                            session_id=request.session_id,
                            lease_id=request.lease_id,
                            device_id=request.device_id,
                            status=CommandState.FAILED,
                            error="COMMAND_IN_FLIGHT",
                        )
                    else:
                        claim_created = True
                        self.events.write(
                            "command_dispatch_claimed",
                            trace_id=trace_id,
                            entity_type="command",
                            entity_id=request.command_id,
                            device_id=request.device_id,
                            tool_id=request.tool_id,
                            validation_ms=validation_ms,
                            claim_ms=claim_ms,
                            predispatch_elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                            inflight_commands=len(self._inflight_by_idempotency_key),
                            **store_metrics,
                        )
                        task = asyncio.create_task(self._dispatcher.dispatch(request, trace_id=trace_id))
                        self._inflight_by_idempotency_key[request.idempotency_key] = task
        except asyncio.CancelledError:
            if claim_created and task is None:
                await self._cancel_claimed_command_after_cancellation(request, trace_id=trace_id)
            raise
        finally:
            if self._claim_locks_by_idempotency_key.get(request.idempotency_key) is lock:
                self._claim_locks_by_idempotency_key.pop(request.idempotency_key, None)
        if immediate_result is not None:
            return immediate_result
        if wait_for_task is not None:
            return await asyncio.shield(wait_for_task)
        if task is None:
            raise RuntimeError("command dispatch did not create or find an in-flight task")
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight_by_idempotency_key.get(request.idempotency_key) is task:
                self._inflight_by_idempotency_key.pop(request.idempotency_key, None)

    async def _cancel_claimed_command_after_cancellation(self, request: CommandRequest, *, trace_id: str) -> None:
        result = CommandResult(
            command_id=request.command_id,
            session_id=request.session_id,
            lease_id=request.lease_id,
            device_id=request.device_id,
            lease_generation=request.lease_generation,
            status=CommandState.CANCELLED,
            error="COMMAND_SEND_CANCELLED_BEFORE_DISPATCH",
        )
        await self._result_recorder.record_async(result, trace_id=trace_id)

    def record_result(self, result: CommandResult, *, trace_id: str) -> CommandResult:
        """Record a command result and return the controller-canonical result.

        Args:
            result: Command result received from a device or adapter.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Persisted result after artifact materialization and catalog-id
            rewriting, or cached terminal result for duplicates and late updates.
        """

        artifact_count, artifact_bytes = _artifact_metrics(result)
        self.events.write(
            "command_result_received",
            trace_id=trace_id,
            entity_type="command",
            entity_id=result.command_id,
            device_id=result.device_id,
            status=result.status.value,
            artifact_count=artifact_count,
            artifact_bytes=artifact_bytes,
            out_of_band=True,
        )
        return self._result_recorder.record(result, trace_id=trace_id)

    async def record_result_async(
        self,
        result: CommandResult,
        *,
        trace_id: str,
        preloaded_command_row: Mapping[str, object] | None = None,
    ) -> CommandResult:
        """Record a command result without compacting payloads on the event loop.

        Args:
            result: Command result received from a device or adapter.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Persisted result after artifact materialization and catalog-id
            rewriting, or cached terminal result for duplicates and late updates.
        """

        artifact_count, artifact_bytes = _artifact_metrics(result)
        self.events.write(
            "command_result_received",
            trace_id=trace_id,
            entity_type="command",
            entity_id=result.command_id,
            device_id=result.device_id,
            status=result.status.value,
            artifact_count=artifact_count,
            artifact_bytes=artifact_bytes,
            out_of_band=True,
        )
        return await self._result_recorder.record_async(
            result,
            trace_id=trace_id,
            preloaded_command_row=preloaded_command_row,
        )

    def _cap_timeout_by_descriptor(
        self,
        request: CommandRequest,
        *,
        descriptor: ToolDescriptor | None,
        trace_id: str,
    ) -> CommandRequest:
        descriptor_timeout = None if descriptor is None else descriptor.timeout_seconds
        if not isinstance(descriptor_timeout, (int, float)) or descriptor_timeout <= 0:
            return request
        descriptor_timeout = float(descriptor_timeout)
        runtime_timeout = _web_agent_runtime_timeout_seconds(request)
        timeout_policy = _command_timeout_policy(
            max(descriptor_timeout, runtime_timeout),
            configured_floor_seconds=self.command_timeout_floor_seconds,
        )
        if runtime_timeout > descriptor_timeout:
            timeout_policy = {
                **timeout_policy,
                "descriptor_timeout_seconds": descriptor_timeout,
                "runtime_timeout_seconds": runtime_timeout,
                "effective_timeout_seconds": max(float(timeout_policy["effective_timeout_seconds"]), runtime_timeout),
                "floor_class": "runtime_side_channel",
            }
        effective_timeout = float(timeout_policy["effective_timeout_seconds"])
        if runtime_timeout > 0 and request.timeout_seconds < effective_timeout:
            self.events.write(
                "command_timeout_raised_by_runtime_side_channel",
                trace_id=trace_id,
                entity_type="command",
                entity_id=request.command_id,
                device_id=request.device_id,
                tool_id=request.tool_id,
                requested_timeout_seconds=request.timeout_seconds,
                **timeout_policy,
            )
            raised_request = request.model_copy(update={"timeout_seconds": effective_timeout})
            return _with_controller_timeout_policy(raised_request, timeout_policy)
        if request.timeout_seconds <= effective_timeout:
            if request.timeout_seconds > descriptor_timeout and effective_timeout > descriptor_timeout:
                self.events.write(
                    "command_timeout_floor_applied",
                    trace_id=trace_id,
                    entity_type="command",
                    entity_id=request.command_id,
                    device_id=request.device_id,
                    tool_id=request.tool_id,
                    requested_timeout_seconds=request.timeout_seconds,
                    **timeout_policy,
                )
                return _with_controller_timeout_policy(request, timeout_policy)
            return request
        self.events.write(
            "command_timeout_capped_by_descriptor",
            trace_id=trace_id,
            entity_type="command",
            entity_id=request.command_id,
            device_id=request.device_id,
            tool_id=request.tool_id,
            requested_timeout_seconds=request.timeout_seconds,
            **timeout_policy,
        )
        capped_request = request.model_copy(update={"timeout_seconds": effective_timeout})
        return _with_controller_timeout_policy(capped_request, timeout_policy)


def _command_timeout_floor_seconds() -> float:
    configured = _read_timeout_env(_COMMAND_TIMEOUT_FLOOR_ENV)
    if configured > 0:
        return configured
    return _read_timeout_env(_LEGACY_COMMAND_TIMEOUT_FLOOR_ENV)


def _store_metric_fields(metrics: ControllerStoreCallMetrics) -> dict[str, float | int | str]:
    return {
        "store_operation": metrics.operation,
        "store_mode": metrics.mode,
        "store_queue_ms": metrics.queue_ms,
        "store_admission_ms": metrics.admission_ms,
        "store_executor_queue_ms": metrics.executor_queue_ms,
        "store_operation_ms": metrics.operation_ms,
        "store_exec_ms": metrics.exec_ms,
        "store_sql_execute_ms": metrics.exec_ms,
        "store_exec_scope": "execute_and_transaction_excluding_fetch",
        "store_total_ms": metrics.total_ms,
        "store_queue_depth": metrics.queue_depth,
        "store_admitted_queue_depth": metrics.admitted_queue_depth,
        "store_sql_count": metrics.sql_count,
    }


def _short_command_timeout_floor_seconds() -> float:
    configured = _read_timeout_env(_COMMAND_SHORT_TIMEOUT_FLOOR_ENV)
    return configured if configured > 0 else _DEFAULT_SHORT_TIMEOUT_FLOOR_SECONDS


def _read_timeout_env(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _command_timeout_policy(
    descriptor_timeout_seconds: float,
    *,
    configured_floor_seconds: float | None = None,
) -> dict[str, float | str]:
    configured_floor = (
        _command_timeout_floor_seconds()
        if configured_floor_seconds is None
        else max(0.0, float(configured_floor_seconds))
    )
    if configured_floor <= 0:
        return {
            "descriptor_timeout_seconds": descriptor_timeout_seconds,
            "effective_timeout_seconds": descriptor_timeout_seconds,
            "configured_floor_seconds": 0.0,
            "floor_class": "none",
        }
    if descriptor_timeout_seconds >= _LONG_DESCRIPTOR_TIMEOUT_THRESHOLD_SECONDS:
        effective_timeout = max(descriptor_timeout_seconds, configured_floor)
        floor_class = "long"
    else:
        short_floor = min(configured_floor, _short_command_timeout_floor_seconds())
        effective_timeout = max(descriptor_timeout_seconds, short_floor)
        floor_class = "short"
    return {
        "descriptor_timeout_seconds": descriptor_timeout_seconds,
        "effective_timeout_seconds": effective_timeout,
        "configured_floor_seconds": configured_floor,
        "floor_class": floor_class,
    }


def _web_agent_runtime_timeout_seconds(request: CommandRequest) -> float:
    """Return Web Agent runtime timeout from AgentKit side-channel settings.

    Remote Web Agent tools are composite browser operations whose actual
    timeout is controlled by ``WebAgentConfig.command_timeout_seconds``.  The
    controller still sees generic MCP descriptors, but those descriptors should
    not truncate the explicit web runtime timeout carried with the command.
    """

    if not request.tool_id.startswith("web.web_agent"):
        return 0.0
    raw_settings = request.parameters.get(AGENTKIT_SETTINGS_PARAM)
    if not isinstance(raw_settings, dict):
        return 0.0
    raw_timeout = raw_settings.get("command_timeout_seconds")
    if not isinstance(raw_timeout, (int, float, str)):
        return 0.0
    try:
        parsed = float(raw_timeout)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _with_controller_timeout_policy(
    request: CommandRequest,
    timeout_policy: dict[str, float | str],
) -> CommandRequest:
    metadata = dict(request.metadata)
    metadata["controller_timeout_policy"] = dict(timeout_policy)
    return request.model_copy(update={"metadata": metadata})


def _artifact_metrics(result: CommandResult) -> tuple[int, int]:
    artifacts = list(result.artifacts or [])
    return len(artifacts), sum(int(artifact.size_bytes or 0) for artifact in artifacts)
