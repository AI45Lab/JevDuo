"""Standalone device worker implementation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from pathlib import Path

from northcua_device.config import DeviceClientConfig
from northcua_device.connection import DeviceConnection
from northcua_device.device_actor import DeviceActor
from northcua_device.executor import CommandExecutor
from northcua_device.health import DeviceHealthReport, DeviceHealthReporter
from northcua_device.heartbeat import DeviceHeartbeatService
from northcua_device.logging import DeviceEventLogger
from northcua_device.message_router import DeviceMessageRouter
from northcua_device.runtime import (
    BrowserRuntimeAdapter,
    DesktopRuntimeAdapter,
    RuntimeAdapter,
    ShellRuntimeAdapter,
)
from northcua_device.runtime.registry import device_runtime_registry
from northcua_device.telemetry import DeviceTelemetrySink
from northcua_protocol.capabilities import DeviceDescriptor, PlatformKind
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import (
    CommandRequest,
    DeviceDrain,
    DeviceHello,
    SessionLeaseGranted,
    SessionLeaseReleased,
    TaskRequest,
)

logger = logging.getLogger(__name__)
_ORDERED_ENVELOPE_QUEUE_MAXSIZE = 128
_SHELL_TOOL_IDS = frozenset({"shell.exec", "shell.exec_readonly"})
_BROWSER_AGENTKIT_KINDS = frozenset({"web", "web_feedback"})


class DeviceWorker:
    """High-level worker that owns controller connection and local execution."""

    def __init__(
        self,
        config: DeviceClientConfig,
        *,
        adapters: list[RuntimeAdapter] | None = None,
        connection: DeviceConnection | None = None,
        initial_task: TaskRequest | None = None,
    ) -> None:
        self.config = config
        resolved_adapters = adapters or self._default_adapters(config)
        self.events = DeviceEventLogger(Path(config.log_dir))
        self.telemetry = DeviceTelemetrySink(
            device_id=config.device_id,
            local_events=self.events,
            stream_mode=config.telemetry_stream_mode,
            sample_rate=config.telemetry_sample_rate,
            always_events=config.telemetry_always_events,
        )
        self.executor = CommandExecutor(
            device_id=config.device_id,
            platform=config.platform,
            adapters=resolved_adapters,
            allowed_tools=config.allowed_tools,
            allowed_capabilities=config.allowed_capabilities,
            event_writer=self._write_event,
        )
        self.actor = DeviceActor(
            device_id=config.device_id,
            executor=self.executor,
            max_sessions=config.max_sessions,
            max_concurrent_commands=config.max_concurrent_commands,
        )
        self.connection = connection or DeviceConnection(config.server_url)
        self.trace_id = uuid.uuid4().hex
        self.initial_task = initial_task
        self._command_received_at: dict[str, float] = {}
        self._initial_task_submitted = False
        self._heartbeat = DeviceHeartbeatService(
            actor=self.actor,
            executor=self.executor,
            connection=self.connection,
            interval_seconds=self.config.heartbeat_interval_seconds,
            trace_id=self.trace_id,
        )
        self._health_reporter = DeviceHealthReporter(self.executor)
        self._router = DeviceMessageRouter(
            config=self.config,
            actor=self.actor,
            connection=self.connection,
            event_writer=self._write_event,
            command_received_at=self._command_received_at,
        )

    @staticmethod
    def _default_adapters(config: DeviceClientConfig) -> list[RuntimeAdapter]:
        adapters = device_runtime_registry.build_for_platform(config.platform, context=config)
        browser_agent_kinds = {
            kind.strip().replace("-", "_")
            for kind in config.agentkit_agent_kinds
            if kind.strip().replace("-", "_") in _BROWSER_AGENTKIT_KINDS
        }
        if browser_agent_kinds and config.platform != PlatformKind.BROWSER:
            adapters.append(BrowserRuntimeAdapter(agent_kinds=browser_agent_kinds))
        # Browser remote harness is not fully wired yet, so the standalone
        # worker only auto-advertises adapters whose tool ids are already
        # backed by the registered production MCP surface.
        if not (config.allowed_tools & _SHELL_TOOL_IDS):
            adapters = [adapter for adapter in adapters if not isinstance(adapter, ShellRuntimeAdapter)]
        return [
            adapter
            for adapter in adapters
            if not isinstance(adapter, DesktopRuntimeAdapter) or config.platform.value in {"macos", "windows", "linux"}
        ]

    def descriptor(self) -> DeviceDescriptor:
        """Return the descriptor advertised to the controller.

        Returns:
            Device descriptor built from enabled runtime adapters and config limits.
        """

        return self.executor.descriptor(
            display_name=self.config.display_name or self.config.device_id,
            max_sessions=self.config.max_sessions,
            max_gui_sessions=self.config.max_gui_sessions,
            gui_resource_id=self.config.resolved_gui_resource_id(),
            max_concurrent_commands=self.config.max_concurrent_commands,
            supports_parallel_sessions=self.config.supports_parallel_sessions,
            requires_foreground_focus=self.config.requires_foreground_focus,
        )

    async def connect_once(self) -> None:
        """Connect, register, and process messages until the controller closes.

        Raises:
            Exception: Propagates websocket/runtime connection failures to the
                reconnect loop or caller.
        """

        await self.connection.connect()
        await self.telemetry.start(connection=self.connection, trace_id=self.trace_id)
        self._write_event("connected", server_url=self.config.server_url, device_id=self.config.device_id)
        await self._send_hello()
        if self.initial_task is not None and not self._initial_task_submitted:
            await self.submit_task(task=self.initial_task)
            self._write_event("task_submitted", task_id=self.initial_task.task_id, trace_id=self.initial_task.trace_id)
            self._initial_task_submitted = True
        heartbeat_task = asyncio.create_task(self._heartbeat.run())
        command_queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=_ORDERED_ENVELOPE_QUEUE_MAXSIZE)
        command_worker_task = asyncio.create_task(self._run_command_queue(command_queue))
        try:
            async for envelope in self.connection.messages():
                await self._raise_if_command_worker_failed(command_worker_task)
                if self._should_queue_ordered_envelope(envelope):
                    await self._enqueue_ordered_envelope(command_queue, envelope, command_worker_task)
                    continue
                await self._handle_envelope(envelope)
                await self._raise_if_command_worker_failed(command_worker_task)
        finally:
            heartbeat_task.cancel()
            try:
                await self._finish_command_queue(command_queue, command_worker_task)
            finally:
                await self.telemetry.stop()
                await self.connection.close()

    async def run_once(self) -> None:
        """Run one connection attempt without reconnect loop."""

        await self.connect_once()

    async def run_forever(self) -> None:
        """Run a reconnecting worker loop for long-lived deployments.

        Raises:
            asyncio.CancelledError: Propagated when the hosting process cancels
                the worker.
        """

        reconnect_delay = self.config.reconnect_initial_delay_seconds
        while True:
            try:
                await self.connect_once()
                self._write_event("disconnected", device_id=self.config.device_id, server_url=self.config.server_url)
            except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
                raise
            except Exception as exc:  # pragma: no cover - network/runtime boundary
                logger.warning(
                    "device worker connection attempt failed",
                    extra={"device_id": self.config.device_id, "server_url": self.config.server_url},
                    exc_info=True,
                )
                self._write_event(
                    "connection_failed",
                    device_id=self.config.device_id,
                    server_url=self.config.server_url,
                    error=str(exc),
                )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(
                self.config.reconnect_max_delay_seconds,
                max(self.config.reconnect_initial_delay_seconds, reconnect_delay * 2),
            )

    async def submit_task(self, *, task: TaskRequest) -> None:
        """Submit a task on the same connection used by this device worker.

        Args:
            task: Task request to self-submit for connection-affined sandbox runs.
        """

        await self.connection.send(Envelope.wrap(MessageType.TASK_REQUEST, task, trace_id=task.trace_id))

    async def health_report(self) -> DeviceHealthReport:
        """Return a health snapshot suitable for CLI/operator diagnostics.

        Returns:
            Aggregated runtime health report for doctor/readiness commands.
        """

        return await self._health_reporter.report()

    async def _send_hello(self) -> None:
        await self.connection.send(
            Envelope.wrap(
                MessageType.DEVICE_HELLO,
                DeviceHello(
                    descriptor=self.descriptor(), token="" if self.config.insecure_dev_no_auth else self.config.token
                ),
                trace_id=self.trace_id,
            )
        )
        self._write_event("device_hello_sent", device_id=self.config.device_id)

    async def _handle_envelope(self, envelope: Envelope) -> None:
        """Compatibility hook for tests and older worker integrations."""

        await self._router.handle(envelope)

    def _should_queue_ordered_envelope(self, envelope: Envelope) -> bool:
        """Return whether an envelope must preserve session-command ordering."""

        return envelope.type in {
            MessageType.COMMAND_REQUEST,
            MessageType.DEVICE_DRAIN,
            MessageType.SESSION_LEASE_GRANTED,
            MessageType.SESSION_LEASE_RELEASED,
        } and isinstance(envelope.payload, (CommandRequest, DeviceDrain, SessionLeaseGranted, SessionLeaseReleased))

    async def _enqueue_ordered_envelope(
        self,
        command_queue: asyncio.Queue[Envelope],
        envelope: Envelope,
        command_worker_task: asyncio.Task[None],
    ) -> None:
        """Queue ordered session work while keeping the websocket receive loop responsive."""

        payload = envelope.payload
        if isinstance(payload, CommandRequest):
            self._command_received_at[payload.command_id] = time.monotonic()
            self._write_event(
                "device_command_received",
                command_id=payload.command_id,
                session_id=payload.session_id,
                tool_id=payload.tool_id,
                timeout_seconds=payload.timeout_seconds,
                queue_depth_before=command_queue.qsize(),
            )
        elif isinstance(payload, SessionLeaseReleased):
            self._write_event(
                "device_session_release_queued",
                session_id=payload.session_id,
                lease_id=payload.lease_id,
                reason=payload.reason,
                queue_depth_before=command_queue.qsize(),
            )
        elif isinstance(payload, SessionLeaseGranted):
            self._write_event(
                "device_session_grant_queued",
                session_id=payload.session_id,
                lease_id=payload.lease_id,
                queue_depth_before=command_queue.qsize(),
            )
        elif isinstance(payload, DeviceDrain):
            self._write_event(
                "device_drain_queued",
                device_id=payload.device_id,
                reason=payload.reason,
                queue_depth_before=command_queue.qsize(),
            )
        if command_queue.full():
            self._write_event(
                "device_ordered_queue_full",
                maxsize=_ORDERED_ENVELOPE_QUEUE_MAXSIZE,
                device_id=self.config.device_id,
            )
        put_task = asyncio.create_task(command_queue.put(envelope))
        done, pending = await asyncio.wait(
            {put_task, command_worker_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            if task is put_task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if command_worker_task in done:
            await command_worker_task
        await put_task

    def _write_event(self, event: str, **payload: object) -> None:
        """Write device telemetry without breaking message routing."""

        try:
            self.telemetry.write(event, **payload)
        except Exception:  # pragma: no cover - telemetry sink is best-effort
            logger.debug("device worker event telemetry write failed", exc_info=True)

    async def _run_command_queue(self, command_queue: asyncio.Queue[Envelope]) -> None:
        """Run command envelopes serially without blocking control-plane reads."""

        while True:
            envelope = await command_queue.get()
            try:
                await self._handle_envelope(envelope)
            finally:
                command_queue.task_done()

    async def _raise_if_command_worker_failed(self, command_worker_task: asyncio.Task[None]) -> None:
        if not command_worker_task.done():
            return
        await command_worker_task

    async def _finish_command_queue(
        self,
        command_queue: asyncio.Queue[Envelope],
        command_worker_task: asyncio.Task[None],
    ) -> None:
        """Flush queued command work before closing the connection."""

        if command_worker_task.done():
            await command_worker_task
            return
        join_task = asyncio.create_task(command_queue.join())
        done, pending = await asyncio.wait(
            {join_task, command_worker_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        if command_worker_task in done:
            await command_worker_task
        command_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await command_worker_task

    async def _build_heartbeat(self):
        """Compatibility hook for tests and older worker integrations."""

        return await self._heartbeat.build_heartbeat()
