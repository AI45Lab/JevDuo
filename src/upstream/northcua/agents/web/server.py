"""Websocket task server for generic local web-agent execution."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import websockets
from websockets.asyncio.server import ServerConnection

from northcua.agentinfra.runtime.server import AgentServerBase
from northcua.agentkernel.tools.surface import normalize_allowed_tool_names
from northcua.core.enums import TaskStatus, WebSocketMessageType
from northcua.core.json_types import JsonObject
from northcua.core.protocol import (
    ErrorPayload,
    HeartbeatPayload,
    NorthCUAWebSocketEnvelope,
    TaskAcceptedPayload,
    TaskFinishedPayload,
    WebTaskRequestPayload,
    parse_task_request_payload,
)
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason

from .domain.results import WebRunStatus
from .facade import WebAgentFacade

logger = logging.getLogger(__name__)


class WebAgentWebSocketServer(AgentServerBase[WebAgentFacade]):
    """Websocket server for generic web-agent tasks.

    v1 keeps execution local to the server process and does not implement the
    controller/device external command plane.
    """

    def __init__(self, *, facade: WebAgentFacade, host: str, port: int) -> None:
        super().__init__(facade=facade, host=host, port=port)
        self._server = None

    async def start(self) -> None:
        """Start the websocket server."""

        self._server = await websockets.serve(self._handle_connection, self.host, self.port)
        logger.info("Web-agent websocket server listening on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the websocket server and drain cached facades."""

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
        async for raw in websocket:
            try:
                envelope = NorthCUAWebSocketEnvelope.model_validate_json(raw)
                await self._dispatch_envelope(websocket, envelope)
            except Exception as exc:
                logger.warning("Failed to handle web-agent websocket envelope", exc_info=True)
                await websocket.send(
                    NorthCUAWebSocketEnvelope.wrap(
                        WebSocketMessageType.ERROR,
                        ErrorPayload(error=str(exc)),
                    ).model_dump_json()
                )

    async def _dispatch_envelope(self, websocket: ServerConnection, envelope: NorthCUAWebSocketEnvelope) -> None:
        if envelope.type == WebSocketMessageType.HEARTBEAT:
            payload = envelope.payload
            if not isinstance(payload, HeartbeatPayload):
                payload = HeartbeatPayload.model_validate(payload)
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.HEARTBEAT,
                    HeartbeatPayload(session_id=payload.session_id, timestamp=asyncio.get_running_loop().time()),
                ).model_dump_json()
            )
            return
        if envelope.type != WebSocketMessageType.TASK_REQUEST:
            return
        task_request = parse_task_request_payload(cast(JsonObject, envelope.payload))
        if not isinstance(task_request, WebTaskRequestPayload):
            return
        session_id = self._resolve_session_id(task_request.session_id, self.facade.session_id)
        if self._closing:
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.ERROR,
                    ErrorPayload(session_id=session_id, error="The web-agent server is shutting down."),
                ).model_dump_json()
            )
            return
        if session_id in self._active_sessions:
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.ERROR,
                    ErrorPayload(session_id=session_id, error="A web-agent task is already running for this session."),
                ).model_dump_json()
            )
            return
        # Reserve before the ACK can yield to another connection with this ID.
        self._active_sessions.add(session_id)
        admission = asyncio.current_task()
        if admission is not None:
            self._admission_tasks.add(admission)
        try:
            facade = self._get_facade(session_id)
            self._session_connections[session_id] = websocket
            await websocket.send(
                NorthCUAWebSocketEnvelope.wrap(
                    WebSocketMessageType.TASK_ACCEPTED,
                    TaskAcceptedPayload(session_id=session_id),
                ).model_dump_json()
            )
            if self._closing:
                raise RuntimeError("The web-agent server shut down before execution started.")
            task = asyncio.create_task(self._run_web_task(websocket, facade, task_request))
            self._track_background_task(task, session_id=session_id)
        except BaseException:
            self._active_sessions.discard(session_id)
            self._session_connections.pop(session_id, None)
            raise
        finally:
            if admission is not None:
                self._admission_tasks.discard(admission)

    async def _run_web_task(
        self,
        websocket: ServerConnection,
        facade: WebAgentFacade,
        task_request: WebTaskRequestPayload,
    ) -> None:
        session_id = self._resolve_session_id(task_request.session_id, facade.session_id)
        try:
            result = await facade.run_web(
                user_query=task_request.request,
                url=task_request.url,
                headed=task_request.headed,
                max_steps=task_request.max_steps or facade.app_config.web.max_steps,
                max_session_steps=task_request.max_session_steps,
                tool_choice_policy=str(task_request.metadata.get("tool_choice_policy", "auto")),
                allowed_tool_names=normalize_allowed_tool_names(task_request.metadata.get("allowed_tool_names")),
            )
            success = result.status == WebRunStatus.SUCCESS and not result.hit_step_limit
            error = None if success else "; ".join(result.tool_errors) or result.final_answer
            status = TaskStatus.FINISH if success else TaskStatus.FAIL
            terminal_decision = result.to_terminal_decision().to_json()
        except Exception as exc:  # pragma: no cover - defensive websocket boundary
            logger.exception("Web-agent task failed", extra={"session_id": session_id})
            status = TaskStatus.FAIL
            error = str(exc)
            terminal_decision = AgentTerminalDecision.error(
                reason=AgentTerminalReason.RUNTIME_ERROR,
                final_answer=error,
                summary="The web task failed before producing a structured result.",
                retryable=True,
            ).to_json()
        finally:
            self._session_connections.pop(session_id, None)
        await websocket.send(
            NorthCUAWebSocketEnvelope.wrap(
                WebSocketMessageType.TASK_FINISHED,
                TaskFinishedPayload(
                    session_id=session_id,
                    status=status,
                    result=str(facade.artifact_store.result_path),
                    error=error,
                    terminal_decision=terminal_decision,
                ),
            ).model_dump_json()
        )

    @staticmethod
    def _resolve_session_id(requested_session_id: str, fallback_session_id: str) -> str:
        """Preserve logical IDs; artifact stores alone encode filesystem keys."""

        return requested_session_id or fallback_session_id
