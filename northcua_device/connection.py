"""Websocket connection wrapper for device workers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from collections.abc import AsyncIterator
import asyncio
import os

import websockets
from websockets.asyncio.client import ClientConnection

from northcua_protocol.envelope import Envelope, parse_envelope

DEFAULT_MAX_MESSAGE_BYTES = 16_000_000
DEFAULT_WEBSOCKET_PING_INTERVAL_SECONDS: float | None = None
DEFAULT_WEBSOCKET_PING_TIMEOUT_SECONDS: float | None = None
_PING_INTERVAL_ENV = "NORTHCUA_WEBSOCKET_CLIENT_PING_INTERVAL_SECONDS"
_PING_TIMEOUT_ENV = "NORTHCUA_WEBSOCKET_CLIENT_PING_TIMEOUT_SECONDS"


@dataclass(frozen=True, slots=True)
class DeviceSendStats:
    """Basic websocket send metrics for device telemetry.

    Attributes:
        envelope_bytes: Serialized envelope size in bytes.
        elapsed_ms: Wall-clock send duration in milliseconds.
    """

    envelope_bytes: int
    elapsed_ms: int


class DeviceConnection:
    """Typed websocket connection to the NorthCUA controller."""

    def __init__(self, server_url: str, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        self.server_url = server_url
        self.max_message_bytes = max_message_bytes
        self._connection: ClientConnection | None = None
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the websocket connection.

        Returns:
            ``None`` after the websocket is connected.
        """

        # Controller/device connections are usually private cluster or
        # localhost traffic. Disable ambient HTTP/SOCKS proxy discovery so
        # local sandbox tests and deployments do not unexpectedly route device
        # control traffic through a user-level proxy.
        self._connection = await websockets.connect(
            self.server_url,
            proxy=None,
            max_size=self.max_message_bytes,
            ping_interval=_optional_positive_seconds(
                os.getenv(_PING_INTERVAL_ENV),
                default=DEFAULT_WEBSOCKET_PING_INTERVAL_SECONDS,
            ),
            ping_timeout=_optional_positive_seconds(
                os.getenv(_PING_TIMEOUT_ENV),
                default=DEFAULT_WEBSOCKET_PING_TIMEOUT_SECONDS,
            ),
        )

    async def close(self) -> None:
        """Close the websocket connection.

        Returns:
            ``None`` after the websocket is closed or already absent.
        """

        async with self._send_lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def send(self, envelope: Envelope) -> DeviceSendStats:
        """Send one protocol envelope.

        Args:
            envelope: Validated envelope to serialize over the websocket.

        Returns:
            Send duration and payload size metrics.

        Raises:
            RuntimeError: If the websocket is not open.
        """

        if self._connection is None:
            raise RuntimeError("device connection is not open")
        return await self.send_raw(envelope.model_dump_json())

    async def send_raw(self, raw: str) -> DeviceSendStats:
        """Send one already-serialized protocol envelope.

        Args:
            raw: JSON envelope payload.

        Returns:
            Send duration and payload size metrics.

        Raises:
            RuntimeError: If the websocket is not open.
        """

        started_at = time.perf_counter()
        async with self._send_lock:
            if self._connection is None:
                raise RuntimeError("device connection is not open")
            await self._connection.send(raw)
        return DeviceSendStats(
            envelope_bytes=len(raw.encode("utf-8")),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )

    async def messages(self) -> AsyncIterator[Envelope]:
        """Yield envelopes from the controller.

        Returns:
            Async iterator of parsed envelopes.

        Raises:
            RuntimeError: If the websocket is not open.
        """

        if self._connection is None:
            raise RuntimeError("device connection is not open")
        async for raw in self._connection:
            yield parse_envelope(raw)


def _optional_positive_seconds(value: object, *, default: float | None) -> float | None:
    if value in (None, ""):
        return default
    seconds = float(str(value))
    return None if seconds <= 0 else seconds
