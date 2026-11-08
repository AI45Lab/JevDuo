"""Device-side command executor with tool allowlist enforcement."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass

from pydantic import BaseModel

from northcua_protocol.capabilities import Capability, DeviceDescriptor, PlatformKind, ToolDescriptor
from northcua_protocol.payloads import (
    COMMAND_RUNTIME_SIDE_CHANNEL_KEYS,
    CommandRequest,
    CommandResult,
    sanitize_command_runtime_side_channels,
)
from northcua_protocol.state import CommandState
from northcua_runtime_shared import RuntimeAdapter, RuntimeHealth, ToolSurfaceRegistry
from northcua_runtime_shared.execution import RuntimeExecutionError, current_execution, execution_scope
from northcua_runtime_shared.tool_contracts import ToolContract, ToolDispatcher, ToolDispatchPolicy

logger = logging.getLogger(__name__)

_BLOCKING_EXECUTOR_MAX_WORKERS = 4
_ISOLATED_EXECUTION_CAPABILITIES = {
    Capability.DESKTOP_SCREENSHOT,
    Capability.DESKTOP_INPUT,
    Capability.DESKTOP_BACKGROUND_INPUT,
    Capability.DESKTOP_MONITOR,
    Capability.DESKTOP_WINDOW,
    Capability.A11Y_TREE,
}


@dataclass(frozen=True)
class _PreparedCommand:
    command: CommandRequest
    adapter: RuntimeAdapter
    contract: ToolContract

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the wire descriptor projection for logging/protocol helpers."""

        return self.contract.to_descriptor()


class CommandExecutor:
    """Route protocol commands to registered runtime adapters."""

    def __init__(
        self,
        *,
        device_id: str,
        platform: PlatformKind,
        adapters: Iterable[RuntimeAdapter],
        allowed_tools: set[str],
        allowed_capabilities: set[Capability] | None = None,
        event_writer: Callable[..., None] | None = None,
    ) -> None:
        """Create an executor from concrete runtime adapters.

        Args:
            device_id: Device id stamped on descriptors and command results.
            platform: Platform family for the worker.
            adapters: Runtime adapters that expose real tool descriptors.
            allowed_tools: Optional allowlist; empty means all adapter tools.
            allowed_capabilities: Optional capability allowlist; empty means all
                capabilities represented by allowed tools.
        """

        self.device_id = device_id
        self.platform = platform
        self._allowed_tools = allowed_tools
        self._allowed_capabilities = allowed_capabilities or set()
        self._event_writer = event_writer
        self._adapter_by_tool_id: dict[str, RuntimeAdapter] = {}
        self._tool_surface = ToolSurfaceRegistry()
        self._capabilities: set[Capability] = set()
        self._tool_dispatcher = ToolDispatcher()
        self._blocking_executor = ThreadPoolExecutor(
            max_workers=_BLOCKING_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="northcua-device-tool",
        )
        self._blocking_executor_recycles = 0
        self._pending_gui_mutations: set[Future[CommandResult]] = set()
        for adapter in adapters:
            contracts = getattr(adapter, "tool_contracts", None)
            if contracts is None:
                raise ValueError(
                    f"Runtime adapter {type(adapter).__name__} must expose tool_contracts; "
                    "descriptor-only runtime adapters are disabled"
                )
            for contract in contracts:
                if allowed_tools and contract.tool_id not in allowed_tools:
                    continue
                if self._allowed_capabilities and contract.capability not in self._allowed_capabilities:
                    continue
                if contract.tool_id in self._adapter_by_tool_id:
                    raise ValueError(f"Duplicate device tool_id advertised by runtime adapters: {contract.tool_id}")
                self._tool_surface.register(contract)
                self._adapter_by_tool_id[contract.tool_id] = adapter
                self._capabilities.add(contract.capability)

    @property
    def capabilities(self) -> set[Capability]:
        """Capabilities exposed by the executor.

        Returns:
            Coarse capabilities represented by all enabled runtime adapters.
        """

        return set(self._capabilities)

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Tools exposed by the executor.

        Returns:
            Stable, sorted list of advertised tool descriptors.
        """

        return self._tool_surface.descriptors()

    async def runtime_health_snapshots(self) -> list[RuntimeHealth]:
        """Collect one runtime-health snapshot per unique adapter.

        Returns:
            Health snapshots without duplicating adapters that expose multiple tools.
        """

        snapshots: list[RuntimeHealth] = []
        seen: set[int] = set()
        for adapter in self._adapter_by_tool_id.values():
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            snapshots.append(await adapter.healthcheck())
        return snapshots

    async def capability_health(self) -> dict[Capability, bool]:
        """Collect capability health from all registered runtime adapters.

        Returns:
            Capability-to-health map merged from adapter health checks.
        """

        health: dict[Capability, bool] = {}
        for snapshot in await self.runtime_health_snapshots():
            health.update(
                {
                    capability: status
                    for capability, status in snapshot.capability_health.items()
                    if capability in self._capabilities
                }
            )
        return health

    def descriptor(
        self,
        *,
        display_name: str,
        max_sessions: int,
        max_gui_sessions: int,
        gui_resource_id: str,
        max_concurrent_commands: int,
        supports_parallel_sessions: bool,
        requires_foreground_focus: bool,
    ) -> DeviceDescriptor:
        """Build a device descriptor for registration.

        Args:
            display_name: Operator-facing device label.
            max_sessions: Maximum sessions this worker accepts.
            max_gui_sessions: GUI session capacity.
            gui_resource_id: Shared GUI resource id used by controller
                scheduling to avoid overbooking one physical display.
            max_concurrent_commands: Maximum active commands.
            supports_parallel_sessions: Whether sessions may be colocated.
            requires_foreground_focus: Whether GUI focus limits scheduling.

        Returns:
            Descriptor advertised in ``DEVICE_HELLO``.
        """

        return DeviceDescriptor(
            device_id=self.device_id,
            display_name=display_name,
            platform=self.platform,
            capabilities=self.capabilities,
            tools=self.tools,
            max_sessions=max_sessions,
            max_gui_sessions=max_gui_sessions,
            gui_resource_id=gui_resource_id,
            max_concurrent_commands=max_concurrent_commands,
            supports_parallel_sessions=supports_parallel_sessions,
            requires_foreground_focus=requires_foreground_focus,
        )

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute under the remaining device queue and descriptor budget.

        Args:
            command: Validated protocol request with the caller's timeout.

        Returns:
            The original tool result or a typed infrastructure failure encoded
            using the existing terminal status and error fields.
        """

        with execution_scope(time.monotonic() + command.timeout_seconds):
            try:
                return await self._execute_with_deadline(command)
            except RuntimeExecutionError as exc:
                return self._failure(command, exc.code).model_copy(update={"status": exc.status})

    async def _execute_with_deadline(self, command: CommandRequest) -> CommandResult:
        """Execute one command after validating tool allowlist and payload size.

        Args:
            command: Controller-issued command request.

        Returns:
            Command result from the target adapter or a protocol-level failure.
        """

        self._write_event(
            "device_command_prepare_started",
            device_id=command.device_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            command_id=command.command_id,
            tool_id=command.tool_id,
            timeout_seconds=command.timeout_seconds,
        )
        prepared = self._prepare_command(command)
        if isinstance(prepared, CommandResult):
            self._write_event(
                "device_command_prepare_failed",
                device_id=command.device_id,
                session_id=command.session_id,
                lease_id=command.lease_id,
                command_id=command.command_id,
                tool_id=command.tool_id,
                status=prepared.status.value,
                error=prepared.error or "",
            )
            return prepared
        isolated_execution = _should_isolate_execution(prepared.descriptor)
        command = prepared.command
        context = current_execution()
        assert context is not None
        context.deadline = min(context.deadline, time.monotonic() + command.timeout_seconds)
        context.remaining()
        self._pending_gui_mutations = {future for future in self._pending_gui_mutations if not future.done()}
        if _is_gui_mutation(prepared.descriptor) and self._pending_gui_mutations:
            raise RuntimeExecutionError("COMMAND_EXECUTION_STILL_RUNNING")
        self._write_event(
            "device_command_prepare_finished",
            device_id=command.device_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            command_id=command.command_id,
            tool_id=command.tool_id,
            timeout_seconds=command.timeout_seconds,
            descriptor_timeout_seconds=prepared.descriptor.timeout_seconds,
            capability=prepared.descriptor.capability.value,
            isolated_execution=isolated_execution,
        )
        started_at = time.perf_counter()
        self._write_event(
            "device_command_execution_started",
            device_id=command.device_id,
            session_id=command.session_id,
            command_id=command.command_id,
            tool_id=command.tool_id,
            timeout_seconds=command.timeout_seconds,
            descriptor_timeout_seconds=prepared.descriptor.timeout_seconds,
            capability=prepared.descriptor.capability.value,
            isolated_execution=isolated_execution,
        )
        logger.info(
            "device command execution started",
            extra={
                "device_id": command.device_id,
                "session_id": command.session_id,
                "command_id": command.command_id,
                "tool_id": command.tool_id,
                "timeout_seconds": command.timeout_seconds,
                "descriptor_timeout_seconds": prepared.descriptor.timeout_seconds,
                "capability": prepared.descriptor.capability.value,
                "isolated_execution": isolated_execution,
            },
        )
        try:
            self._write_event(
                "device_driver_call_started",
                device_id=command.device_id,
                session_id=command.session_id,
                command_id=command.command_id,
                tool_id=command.tool_id,
                timeout_seconds=command.timeout_seconds,
                descriptor_timeout_seconds=prepared.descriptor.timeout_seconds,
                capability=prepared.descriptor.capability.value,
                isolated_execution=isolated_execution,
            )
            execution = self._execute_prepared(prepared, isolated_execution=isolated_execution)
            result = await asyncio.wait_for(execution, timeout=context.remaining())
            if context.failure is not None:
                raise context.failure
            context.remaining()
        except TimeoutError:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            self._write_event(
                "device_driver_call_finished",
                device_id=command.device_id,
                session_id=command.session_id,
                command_id=command.command_id,
                tool_id=command.tool_id,
                status=CommandState.TIMED_OUT.value,
                elapsed_ms=elapsed_ms,
                timeout_seconds=command.timeout_seconds,
                isolated_execution=isolated_execution,
                error="COMMAND_TIMED_OUT",
            )
            if isolated_execution:
                self._handle_isolated_timeout(command=command)
            logger.warning(
                "device command execution timed out",
                extra={
                    "device_id": command.device_id,
                    "session_id": command.session_id,
                    "command_id": command.command_id,
                    "tool_id": command.tool_id,
                    "timeout_seconds": command.timeout_seconds,
                    "elapsed_ms": elapsed_ms,
                    "isolated_execution": isolated_execution,
                },
            )
            return CommandResult(
                command_id=command.command_id,
                session_id=command.session_id,
                lease_id=command.lease_id,
                device_id=command.device_id,
                lease_generation=command.lease_generation,
                status=CommandState.TIMED_OUT,
                error="COMMAND_TIMED_OUT",
            )
        except RuntimeExecutionError:
            raise
        except Exception as exc:  # pragma: no cover - defensive runtime boundary
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            self._write_event(
                "device_driver_call_finished",
                device_id=command.device_id,
                session_id=command.session_id,
                command_id=command.command_id,
                tool_id=command.tool_id,
                status=CommandState.FAILED.value,
                elapsed_ms=elapsed_ms,
                timeout_seconds=command.timeout_seconds,
                isolated_execution=isolated_execution,
                error=str(exc),
            )
            logger.warning(
                "device runtime adapter failed command",
                extra={
                    "device_id": command.device_id,
                    "session_id": command.session_id,
                    "lease_id": command.lease_id,
                    "command_id": command.command_id,
                    "tool_id": command.tool_id,
                    "elapsed_ms": elapsed_ms,
                    "isolated_execution": isolated_execution,
                },
                exc_info=True,
            )
            return self._failure(command, str(exc))
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        tool_stage_timings = _extract_tool_stage_timings(result)
        self._write_event(
            "device_driver_call_finished",
            device_id=command.device_id,
            session_id=command.session_id,
            command_id=command.command_id,
            tool_id=command.tool_id,
            status=result.status.value,
            elapsed_ms=elapsed_ms,
            timeout_seconds=command.timeout_seconds,
            isolated_execution=isolated_execution,
            tool_stage_timings=tool_stage_timings,
            error=result.error or "",
        )
        logger.info(
            "device command execution completed",
            extra={
                "device_id": command.device_id,
                "session_id": command.session_id,
                "command_id": command.command_id,
                "tool_id": command.tool_id,
                "status": result.status.value,
                "elapsed_ms": elapsed_ms,
                "isolated_execution": isolated_execution,
            },
        )
        return _strip_tool_stage_timings(result)

    async def _execute_prepared(self, prepared: _PreparedCommand, *, isolated_execution: bool) -> CommandResult:
        if not isolated_execution:
            return await prepared.adapter.execute(prepared.command)
        loop = asyncio.get_running_loop()
        copied_context = copy_context()
        future = self._blocking_executor.submit(
            copied_context.run, _execute_adapter_in_fresh_loop, prepared.adapter, prepared.command
        )
        if _is_gui_mutation(prepared.descriptor):
            self._pending_gui_mutations.add(future)
        # Shield the asyncio wrapper: cancellation cannot cancel a running
        # thread. Keep the real Future until done, so no later mutation can
        # overtake it even after the executor pool has been recycled.
        wrapped = asyncio.wrap_future(future, loop=loop)
        wrapped.add_done_callback(_consume_isolated_completion)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # Cancels only work that has not started. A running Future stays
            # tracked until its actual completion, without claiming a kill.
            future.cancel()
            raise

    def _handle_isolated_timeout(self, *, command: CommandRequest) -> None:
        """Recycle the isolated executor after one GUI timeout.

        A Python worker thread that timed out can still mutate the desktop.
        Replacing the pool preserves read-only probes and telemetry; the real
        Future continues to block subsequent mutations until it has finished.
        """

        self._recycle_blocking_executor(command=command)
        logger.warning(
            "device isolated command pool recycled; prior GUI execution may still be active",
            extra={
                "device_id": command.device_id,
                "session_id": command.session_id,
                "command_id": command.command_id,
                "tool_id": command.tool_id,
                "executor_recycles": self._blocking_executor_recycles,
            },
        )

    def _recycle_blocking_executor(self, *, command: CommandRequest) -> None:
        """Replace the isolated executor after a GUI command times out.

        Python cannot forcibly stop a stuck worker thread. Replacing the pool
        prevents one hung GUI call from exhausting capacity; the next command
        runs on a fresh pool so the controller can still collect final
        screenshots, logs, and cleanup signals.
        """

        old_executor = self._blocking_executor
        self._blocking_executor_recycles += 1
        self._blocking_executor = ThreadPoolExecutor(
            max_workers=_BLOCKING_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="northcua-device-tool",
        )
        old_executor.shutdown(wait=False, cancel_futures=True)
        logger.warning(
            "device isolated command executor recycled after timeout",
            extra={
                "device_id": command.device_id,
                "session_id": command.session_id,
                "command_id": command.command_id,
                "tool_id": command.tool_id,
                "executor_recycles": self._blocking_executor_recycles,
            },
        )

    def _prepare_command(self, command: CommandRequest) -> _PreparedCommand | CommandResult:
        if self._allowed_tools and command.tool_id not in self._allowed_tools:
            return self._failure(command, "tool is not allowlisted")
        adapter = self._adapter_by_tool_id.get(command.tool_id)
        if adapter is None:
            return self._failure(command, f"unknown tool_id: {command.tool_id}")
        contract = self._tool_surface.contract(command.tool_id)
        if contract is None:
            return self._failure(command, f"missing ToolContract for tool_id: {command.tool_id}")
        if self._allowed_capabilities and contract.capability not in self._allowed_capabilities:
            return self._failure(command, "tool capability is not allowlisted")
        try:
            dispatch_parameters, side_channel_parameters = _split_runtime_side_channel_parameters(command.parameters)
            envelope = self._tool_dispatcher.prepare(
                contract,
                dispatch_parameters,
                policy=ToolDispatchPolicy(max_allowed_risk=None),
            )
        except (LookupError, PermissionError, ValueError) as exc:
            return self._failure(command, str(exc))
        validated_parameters = envelope.validated_parameters
        if isinstance(validated_parameters, BaseModel):
            validated_parameters = dict(validated_parameters.model_dump(mode="json"))
        else:
            validated_parameters = dict(validated_parameters)
        if side_channel_parameters:
            validated_parameters.update(side_channel_parameters)
        command = command.model_copy(update={"parameters": validated_parameters})
        descriptor = contract.to_descriptor()
        effective_descriptor_timeout = _effective_timeout_from_controller_policy(command, descriptor)
        if command.timeout_seconds <= effective_descriptor_timeout:
            return _PreparedCommand(command=command, adapter=adapter, contract=contract)
        self._write_event(
            "device_command_timeout_capped",
            device_id=command.device_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            command_id=command.command_id,
            tool_id=command.tool_id,
            requested_timeout_seconds=command.timeout_seconds,
            descriptor_timeout_seconds=descriptor.timeout_seconds,
            effective_timeout_seconds=effective_descriptor_timeout,
        )
        logger.info(
            "device command timeout capped by tool descriptor",
            extra={
                "device_id": command.device_id,
                "session_id": command.session_id,
                "command_id": command.command_id,
                "tool_id": command.tool_id,
                "requested_timeout_seconds": command.timeout_seconds,
                "descriptor_timeout_seconds": descriptor.timeout_seconds,
                "effective_timeout_seconds": effective_descriptor_timeout,
            },
        )
        capped_command = command.model_copy(update={"timeout_seconds": effective_descriptor_timeout})
        return _PreparedCommand(command=capped_command, adapter=adapter, contract=contract)

    def _write_event(self, event: str, **payload: object) -> None:
        if self._event_writer is None:
            return
        try:
            self._event_writer(event, **payload)
        except Exception:  # pragma: no cover - telemetry must not break command execution
            logger.debug("device event telemetry write failed", exc_info=True)

    @staticmethod
    def _failure(command: CommandRequest, error: str) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            device_id=command.device_id,
            lease_generation=command.lease_generation,
            status=CommandState.FAILED,
            error=error,
        )


def _should_isolate_execution(descriptor: ToolDescriptor) -> bool:
    """Return whether a tool should be executed away from the worker event loop."""

    if descriptor.capability == Capability.BROWSER_PAGE:
        return False
    return descriptor.requires_focus or descriptor.capability in _ISOLATED_EXECUTION_CAPABILITIES


def _execute_adapter_in_fresh_loop(adapter: RuntimeAdapter, command: CommandRequest) -> CommandResult:
    """Run one adapter command in a worker thread with its own event loop."""

    context = current_execution()
    if context is not None:
        context.remaining()
    logger.info("device driver thread started", extra={"command_id": command.command_id, "tool_id": command.tool_id})
    started_at = time.monotonic()
    try:
        result = adapter.execute(command)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result
    finally:
        logger.info(
            "device driver thread finished",
            extra={
                "command_id": command.command_id,
                "tool_id": command.tool_id,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            },
        )


def _is_gui_mutation(descriptor: ToolDescriptor) -> bool:
    return descriptor.side_effect and descriptor.capability in {
        Capability.DESKTOP_INPUT,
        Capability.DESKTOP_BACKGROUND_INPUT,
        Capability.DESKTOP_WINDOW,
    }


def _consume_isolated_completion(future: asyncio.Future[CommandResult]) -> None:
    """Observe abandoned thread failures without delivering their late result."""

    if not future.cancelled():
        future.exception()


def _split_runtime_side_channel_parameters(
    parameters: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Separate runtime-only side channels from model/tool parameters.

    The device executor owns schema enforcement for command parameters. Agent
    runtimes may also need hidden controller-to-device session settings, such as
    AgentKit browser config, that are intentionally absent from the tool schema.
    Those keys are preserved for the runtime adapter after validation and never
    become model-visible tool parameters.
    """

    dispatch_parameters: dict[str, object] = {}
    for key, value in parameters.items():
        if key not in COMMAND_RUNTIME_SIDE_CHANNEL_KEYS:
            dispatch_parameters[key] = value
    return dispatch_parameters, sanitize_command_runtime_side_channels(parameters)


def _effective_timeout_from_controller_policy(command: CommandRequest, descriptor: ToolDescriptor) -> float:
    """Return the device-side timeout cap, honoring controller-normalized budgets."""

    policy = command.metadata.get("controller_timeout_policy") if isinstance(command.metadata, dict) else None
    if not isinstance(policy, dict):
        return float(descriptor.timeout_seconds)
    effective = policy.get("effective_timeout_seconds")
    if not isinstance(effective, (int, float, str)):
        return float(descriptor.timeout_seconds)
    try:
        effective_timeout = float(effective)
    except (TypeError, ValueError):
        return float(descriptor.timeout_seconds)
    return max(float(descriptor.timeout_seconds), effective_timeout)


def _extract_tool_stage_timings(result: CommandResult) -> dict[str, object]:
    payload = result.result
    if not isinstance(payload, dict):
        return {}
    timings = payload.get("northcua_stage_timings")
    return timings if isinstance(timings, dict) else {}


def _strip_tool_stage_timings(result: CommandResult) -> CommandResult:
    payload = result.result
    if not isinstance(payload, dict) or "northcua_stage_timings" not in payload:
        return result
    cleaned = dict(payload)
    cleaned.pop("northcua_stage_timings", None)
    return result.model_copy(update={"result": cleaned})
