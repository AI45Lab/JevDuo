"""Heartbeat service for long-lived device workers."""

from __future__ import annotations

import asyncio

from northcua_device.connection import DeviceConnection
from northcua_device.device_actor import DeviceActor
from northcua_device.executor import CommandExecutor
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import DeviceHeartbeat


class DeviceHeartbeatService:
    """Build and send device heartbeat envelopes."""

    def __init__(
        self,
        *,
        actor: DeviceActor,
        executor: CommandExecutor,
        connection: DeviceConnection,
        interval_seconds: float,
        trace_id: str,
    ) -> None:
        self.actor = actor
        self.executor = executor
        self.connection = connection
        self.interval_seconds = interval_seconds
        self.trace_id = trace_id

    async def run(self) -> None:
        """Send heartbeat messages until the task is cancelled."""

        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.connection.send(
                Envelope.wrap(
                    MessageType.DEVICE_HEARTBEAT,
                    await self.build_heartbeat(),
                    trace_id=self.trace_id,
                )
            )

    async def build_heartbeat(self) -> DeviceHeartbeat:
        """Return the current load and runtime-health heartbeat payload.

        Returns:
            Heartbeat payload with actor load and runtime capability health.
        """

        heartbeat = self.actor.heartbeat()
        return heartbeat.model_copy(update={"capability_health": await self.executor.capability_health()})


__all__ = ["DeviceHeartbeatService"]
