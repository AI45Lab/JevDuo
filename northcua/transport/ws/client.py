"""Typed websocket client helpers for NorthCUA."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection

from northcua.core.enums import WebSocketMessageType
from northcua.core.protocol import (
    CommandRequestPayload,
    CommandResultPayload,
    DesktopTaskRequestPayload,
    HeartbeatPayload,
    NorthCUAWebSocketEnvelope,
    WebTaskRequestPayload,
)
from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor
from northcua.transport.ws.keepalive import client_ping_interval_seconds, client_ping_timeout_seconds


class NorthCUAWebSocketClient:
    """Small typed websocket client for task submission and heartbeats."""

    def __init__(self, url: str) -> None:
        """Initialize the typed websocket client.

        Args:
            url: Websocket server URL.
        """

        self.url = url
        self._connection: ClientConnection | None = None

    async def connect(self) -> None:
        """Open the websocket connection."""

        self._connection = await websockets.connect(
            self.url,
            proxy=None,
            ping_interval=client_ping_interval_seconds(),
            ping_timeout=client_ping_timeout_seconds(),
        )

    async def close(self) -> None:
        """Close the websocket connection."""

        if self._connection is not None:
            await self._connection.close()

    async def send_task(self, payload: DesktopTaskRequestPayload | WebTaskRequestPayload) -> None:
        """Submit a task request.

        Args:
            payload: Desktop or web task request payload.
        """

        await self._send(NorthCUAWebSocketEnvelope.wrap(WebSocketMessageType.TASK_REQUEST, payload))

    async def send_heartbeat(self, session_id: str | None = None) -> None:
        """Send a heartbeat envelope.

        Args:
            session_id: Optional session identifier to echo through the server.
        """

        await self._send(
            NorthCUAWebSocketEnvelope.wrap(
                WebSocketMessageType.HEARTBEAT,
                HeartbeatPayload(session_id=session_id, timestamp=asyncio.get_running_loop().time()),
            )
        )

    async def send_command_results(self, payload: CommandResultPayload) -> None:
        """Submit command execution results back to the server.

        Args:
            payload: Command result payload produced by the device executor.
        """

        await self._send(NorthCUAWebSocketEnvelope.wrap(WebSocketMessageType.COMMAND_RESULT, payload))

    async def iter_messages(self) -> AsyncIterator[NorthCUAWebSocketEnvelope]:
        """Yield envelopes from the server.

        Yields:
            Parsed websocket envelopes received from the server.

        Returns:
            Async iterator that completes when the websocket closes.

        Raises:
            RuntimeError: If the client is not connected.
        """

        if self._connection is None:
            raise RuntimeError("WebSocket client is not connected")
        async for raw in self._connection:
            yield NorthCUAWebSocketEnvelope.model_validate_json(raw)

    async def run_device_loop(
        self,
        executor: NorthCUADeviceCommandExecutor | None = None,
    ) -> None:
        """Run a device-side loop that executes command requests.

        Args:
            executor: Optional command executor override.
        """

        executor = executor or NorthCUADeviceCommandExecutor()
        async for envelope in self.iter_messages():
            if envelope.type != WebSocketMessageType.COMMAND_REQUEST:
                continue
            payload = envelope.payload
            if not isinstance(payload, CommandRequestPayload):
                payload = CommandRequestPayload.model_validate(payload)
            result_payload = await executor.execute_command_request(payload)
            await self.send_command_results(result_payload)

    async def _send(self, envelope: NorthCUAWebSocketEnvelope) -> None:
        """Send one typed envelope over the active websocket connection.

        Args:
            envelope: Envelope to serialize and send.

        Raises:
            RuntimeError: If the client is not connected.
        """

        if self._connection is None:
            raise RuntimeError("WebSocket client is not connected")
        await self._connection.send(envelope.model_dump_json())
