"""Long-lived websocket task server for NorthCUA."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import cast

import websockets
from nexau.core.messages import ImageBlock, TextBlock, ToolResultBlock
from websockets.asyncio.server import Server, ServerConnection

from northcua.agentinfra.runtime.server import AgentServerBase
from northcua.agents.desktop.facade import NorthCUAAgentFacade
from northcua.agents.desktop.runtime import DesktopObservationContextProvider, DesktopRuntimeOptions, DesktopTaskRuntime
from northcua.agents.desktop.server_remote import DesktopRemoteServerBridge, command_result_to_resume_block
from northcua.core.budget import AgentStepBudget
from northcua.core.enums import ResultStatus, RuntimeMode, TaskStatus, WebSocketMessageType
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua.core.json_types import JsonObject
from northcua.core.models import (
    CommandExecutionResultRecord,
    ObservationContext,
)
from northcua.core.protocol import (
    CommandResultPayload,
    DesktopTaskRequestPayload,
    ErrorPayload,
    HeartbeatPayload,
    NorthCUAWebSocketEnvelope,
    TaskAcceptedPayload,
    TaskFinishedPayload,
    parse_task_request_payload,
)
from northcua.transport.ws.result_blocks import RemoteToolDispatchResult
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

logger = logging.getLogger(__name__)


class NorthCUAWebSocketServer(AgentServerBase[NorthCUAAgentFacade]):  # type: ignore[type-var]
    """Async websocket server keeping NorthCUA task lifecycle semantics."""

    _MAX_STEPS_PER_TASK = 100

    def __init__(
        self,
        facade: NorthCUAAgentFacade,
        execution_transport: NorthCUAWebSocketTransport,
        host: str,
        port: int,
        *,
        runtime_mode: RuntimeMode = RuntimeMode.LOCAL,
        command_result_timeout_seconds: float = 60.0,
        command_poll_interval_seconds: float = 0.1,
    ) -> None:
        """Initialize the websocket task server.

        Args:
            facade: Agent facade that owns task/session state.
            execution_transport: Runtime transport used by the facade.
            host: Interface to bind.
            port: TCP port to bind.
            runtime_mode: Local or remote runtime mode.
            command_result_timeout_seconds: Timeout for remote command results.
            command_poll_interval_seconds: Poll interval for command results.
        """
        super().__init__(facade=facade, host=host, port=port)
        self.execution_transport = execution_transport
        self.runtime_mode = runtime_mode
        self.command_result_timeout_seconds = command_result_timeout_seconds
        self.command_poll_interval_seconds = command_poll_interval_seconds
        self._remote_bridge = DesktopRemoteServerBridge(
            command_result_timeout_seconds=command_result_timeout_seconds,
            command_poll_interval_seconds=command_poll_interval_seconds,
        )
        self._server: Server | None = None

    async def start(self) -> None:
        """Start accepting websocket connections."""
        self._server = await websockets.serve(self._handle_connection, self.host, self.port)
        logger.info("NorthCUA websocket server listening on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the websocket server."""
        self._closing = True
        server = self._server
        if server is not None:
            server.close()
        try:
            await self.shutdown_session_cache()
        finally:
            if server is not None:
                await server.wait_closed()
            self._server = None

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        """Handle one websocket client."""
        async for raw in websocket:
            try:
                envelope = NorthCUAWebSocketEnvelope.model_validate_json(raw)
                await self._dispatch_envelope(websocket, envelope)
            except Exception as exc:
                logger.exception("Failed to handle websocket envelope")
                await websocket.send(
                    NorthCUAWebSocketEnvelope.wrap(
                        WebSocketMessageType.ERROR, ErrorPayload(error=str(exc), session_id=self.facade.session_id)
                    ).model_dump_json()
                )

    async def _dispatch_envelope(self, websocket: ServerConnection, envelope: NorthCUAWebSocketEnvelope) -> None:
        """Dispatch one typed websocket envelope."""
        if envelope.type == WebSocketMessageType.HEARTBEAT:
            payload = envelope.payload
            if not isinstance(payload, HeartbeatPayload):
                payload = HeartbeatPayload.model_validate(payload)
            session_id = payload.session_id or self.facade.rounds.state.session_id
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.HEARTBEAT,
                    HeartbeatPayload(session_id=session_id, timestamp=asyncio.get_running_loop().time()),
                ).model_dump_json()
            )
            return
        if envelope.type == WebSocketMessageType.COMMAND_RESULT:
            if self._closing:
                return
            payload = envelope.payload
            if not isinstance(payload, CommandResultPayload):
                payload = CommandResultPayload.model_validate(payload)
            owner = self._session_connections.get(payload.session_id)
            if owner is not None and owner is not websocket:
                logger.warning("Ignoring cross-connection command result", extra={"session_id": payload.session_id})
                return
            facade = self._get_facade(payload.session_id)
            await facade.record_command_results(payload.results)
            return
        if envelope.type != WebSocketMessageType.TASK_REQUEST:
            return
        task_request = parse_task_request_payload(cast(JsonObject, envelope.payload))
        if not isinstance(task_request, DesktopTaskRequestPayload):
            return
        session_id = task_request.session_id or self.facade.rounds.state.session_id
        if self._closing:
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.ERROR,
                    ErrorPayload(session_id=session_id, error="The desktop-agent server is shutting down."),
                ).model_dump_json()
            )
            return
        if session_id in self._active_sessions or (self.runtime_mode == RuntimeMode.LOCAL and self._active_sessions):
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.ERROR,
                    ErrorPayload(session_id=session_id, error="A NorthCUA task is already running for this session."),
                ).model_dump_json()
            )
            return
        self._active_sessions.add(session_id)
        admission = asyncio.current_task()
        if admission is not None:
            self._admission_tasks.add(admission)
        acknowledged = False
        try:
            facade = self._get_facade(session_id)
            self._session_connections[session_id] = websocket
            # An unsuccessful ACK must not enqueue a task that a later request runs.
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.TASK_ACCEPTED, TaskAcceptedPayload(session_id=session_id)
                ).model_dump_json()
            )
            acknowledged = True
            await facade.hydrate_state()
            task_query = task_request.request
            if not facade.rounds.state.pending_round_inputs and not facade.rounds.state.step_logs:
                await facade.enqueue_initial_request(task_query, metadata=task_request.metadata)
            else:
                await facade.enqueue_followup(task_query, metadata=task_request.metadata)
            if self._closing:
                raise RuntimeError("The desktop-agent server shut down before execution started.")
            task = asyncio.create_task(self._run_pending_rounds(websocket, facade))
            self._track_background_task(task, session_id=session_id)
        except BaseException as exc:
            self._active_sessions.discard(session_id)
            self._session_connections.pop(session_id, None)
            if acknowledged and isinstance(exc, Exception) and not self._closing:
                decision = AgentTerminalDecision.error(
                    reason=AgentTerminalReason.RUNTIME_ERROR,
                    final_answer=str(exc),
                    summary="The desktop task could not start its requested round.",
                    retryable=True,
                )
                await websocket.send(
                    NorthCUAWebSocketEnvelope.wrap(
                        WebSocketMessageType.TASK_FINISHED,
                        TaskFinishedPayload(
                            session_id=session_id,
                            status=TaskStatus.FAIL,
                            error=str(exc),
                            terminal_decision=decision.to_json(),
                        ),
                    ).model_dump_json()
                )
            else:
                raise
        finally:
            if admission is not None:
                self._admission_tasks.discard(admission)

    async def _run_pending_rounds(self, websocket: ServerConnection, facade: NorthCUAAgentFacade) -> None:
        """Run queued rounds and only emit task completion for terminal outcomes."""
        session_id = facade.rounds.state.session_id
        try:
            while True:
                round_input = await facade.next_round()
                if round_input is None:
                    break
                observation_context = None
                remote_dispatcher = None
                if self.runtime_mode == RuntimeMode.REMOTE:
                    remote_dispatcher = lambda pending_tool_calls: self._dispatch_external_tool_calls(
                        websocket, facade, pending_tool_calls
                    )

                    async def _collect_observation(session_step, previous_steps):
                        del session_step, previous_steps
                        return await self._collect_remote_observation_context(websocket, facade)

                    observation_provider = DesktopObservationContextProvider(_collect_observation)
                else:
                    observation_provider = None
                budget = self._budget_from_round_input(round_input)
                task_result = await DesktopTaskRuntime(facade).run_round_input(
                    round_input,
                    max_steps=budget.max_round_steps or self._MAX_STEPS_PER_TASK,
                    max_session_steps=budget.max_session_steps,
                    options=DesktopRuntimeOptions(
                        observation_context=observation_context,
                        observation_provider=observation_provider,
                        remote_tool_dispatcher=remote_dispatcher,
                    ),
                    drain_background_tasks=False,
                )
                if not task_result.steps:
                    raise RuntimeError("NorthCUA could not execute any steps for the requested task.")
                step_log = task_result.steps[-1]
                await websocket.send(
                    NorthCUAWebSocketEnvelope.wrap(
                        WebSocketMessageType.TASK_FINISHED,
                        TaskFinishedPayload(
                            session_id=step_log.session_id,
                            status=step_log.status,
                            result=step_log.result,
                            terminal_decision=step_log.terminal_decision,
                        ),
                    ).model_dump_json()
                )
        except Exception as exc:
            logger.exception("Desktop-agent task failed", extra={"session_id": session_id})
            decision = AgentTerminalDecision.error(
                reason=AgentTerminalReason.RUNTIME_ERROR,
                final_answer=str(exc),
                summary="The desktop task failed before completing the requested round.",
                retryable=True,
            )
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.TASK_FINISHED,
                    TaskFinishedPayload(
                        session_id=session_id,
                        status=TaskStatus.FAIL,
                        error=str(exc),
                        terminal_decision=decision.to_json(),
                    ),
                ).model_dump_json()
            )
        finally:
            self._session_connections.pop(session_id, None)

    def _budget_from_round_input(self, round_input) -> AgentStepBudget:
        """Resolve server round/session budgets from round metadata."""
        metadata = getattr(round_input, "metadata", {}) or {}
        return AgentStepBudget(
            max_round_steps=self._metadata_int(metadata.get("max_steps"), self._MAX_STEPS_PER_TASK),
            max_session_steps=self._metadata_int(metadata.get("max_session_steps"), 0),
        )

    @staticmethod
    def _metadata_int(value: object, default: int) -> int:
        """Parse a non-negative integer metadata value."""
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return default

    async def _collect_remote_observation_context(
        self, websocket: ServerConnection, facade: NorthCUAAgentFacade
    ) -> ObservationContext:
        """Collect the current-round observation context from the remote device client."""
        return await self._remote_bridge.collect_observation_context(websocket=websocket, facade=facade)

    async def _dispatch_external_tool_calls(
        self,
        websocket: ServerConnection,
        facade: NorthCUAAgentFacade,
        pending_tool_calls: list[JsonObject],
    ) -> RemoteToolDispatchResult:
        """Send pending external tool calls to the remote device and wait for results."""
        return await self._remote_bridge.dispatch_external_tool_calls(
            websocket=websocket,
            facade=facade,
            pending_tool_calls=pending_tool_calls,
        )

    async def _await_command_results(
        self, facade: NorthCUAAgentFacade, call_ids: list[str]
    ) -> list[CommandExecutionResultRecord]:
        """Wait for all requested command results to be received over websocket."""
        return await self._remote_bridge.await_command_results(facade=facade, call_ids=call_ids)

    @staticmethod
    def _command_result_to_tool_result_block(result: CommandExecutionResultRecord) -> ToolResultBlock:
        """Convert one websocket command result into a ToolResultBlock resume payload."""
        return command_result_to_resume_block(result)
