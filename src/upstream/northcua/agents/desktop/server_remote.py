"""Remote observation and command bridge for the desktop websocket server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from nexau.core.messages import ToolResultBlock
from websockets.asyncio.server import ServerConnection

from northcua.agents.desktop.facade import NorthCUAAgentFacade
from northcua.agents.desktop.observation import ObservationCoordinator, RemoteInternalProbeExecutor
from northcua.core.coordinates import CoordinateTarget, non_default_coordinate_policy_context_values, policy_from_config
from northcua.core.enums import WebSocketMessageType
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord, CommandRequestRecord, ObservationContext
from northcua.core.protocol import CommandRequestPayload, NorthCUAWebSocketEnvelope
from northcua.transport.ws.result_blocks import RemoteToolDispatchResult, command_result_to_tool_result_block


@dataclass(slots=True)
class DesktopRemoteServerBridge:
    """Bridge remote probes and command dispatch through websocket payloads.

    Attributes:
        command_result_timeout_seconds: Timeout for remote command results.
        command_poll_interval_seconds: Poll interval for command results.
    """

    command_result_timeout_seconds: float
    command_poll_interval_seconds: float

    async def collect_observation_context(
        self,
        *,
        websocket: ServerConnection,
        facade: NorthCUAAgentFacade,
    ) -> ObservationContext:
        """Collect the current-round observation context from the remote device.

        Args:
            websocket: Active websocket connection to the remote controller.
            facade: Desktop facade that owns session state and artifact paths.

        Returns:
            Observation context captured through remote internal probe commands.
        """

        facade.artifact_store.set_query_index(max(facade.rounds.state.round_num, 1))
        coordinator = ObservationCoordinator(
            app_config=facade.app_config,
            agent_config=facade.agent_config,
            artifact_store=facade.artifact_store,
            probe_executor=RemoteInternalProbeExecutor(
                session_id=facade.rounds.state.session_id,
                round_num=max(facade.rounds.state.round_num, 1),
                round_step=facade.rounds.state.round_step,
                send_payload=lambda envelope: websocket.send(envelope.model_dump_json()),
                await_results=lambda call_ids: self.await_command_results(facade=facade, call_ids=call_ids),
            ),
            platform_name="remote",
        )
        return await coordinator.collect(
            session_step=facade.rounds.state.session_step,
            previous_steps=facade.rounds.state.step_logs,
        )

    async def dispatch_external_tool_calls(
        self,
        *,
        websocket: ServerConnection,
        facade: NorthCUAAgentFacade,
        pending_tool_calls: list[JsonObject],
    ) -> RemoteToolDispatchResult:
        """Send pending external tool calls to the remote device and wait for results.

        Args:
            websocket: Active websocket connection to the remote controller.
            facade: Desktop facade that owns session state and pending results.
            pending_tool_calls: Tool call payloads emitted by the model.

        Returns:
            Remote dispatch result with NexAU tool blocks and raw command results.
        """

        commands = [
            CommandRequestRecord(
                call_id=str(call.get("id") or ""),
                tool_name=str(call.get("name") or ""),
                server_name=str(call.get("server_name") or ""),
                parameters=_json_object(call.get("input")),
                tool_type="action",
            )
            for call in pending_tool_calls
        ]
        await websocket.send(
            NorthCUAWebSocketEnvelope.wrap(
                WebSocketMessageType.COMMAND_REQUEST,
                CommandRequestPayload(
                    session_id=facade.rounds.state.session_id,
                    round_num=max(facade.rounds.state.round_num, 1),
                    round_step=facade.rounds.state.round_step,
                    metadata=cast(
                        JsonObject,
                        non_default_coordinate_policy_context_values(
                            CoordinateTarget.DESKTOP,
                            policy_from_config(facade.app_config.desktop.coordinate_policy),
                        ),
                    ),
                    commands=commands,
                ),
            ).model_dump_json()
        )
        results = await self.await_command_results(facade=facade, call_ids=[command.call_id for command in commands])
        return RemoteToolDispatchResult(
            tool_blocks=[command_result_to_tool_result_block(result) for result in results],
            command_results=results,
        )

    async def await_command_results(
        self,
        *,
        facade: NorthCUAAgentFacade,
        call_ids: list[str],
    ) -> list[CommandExecutionResultRecord]:
        """Wait for all requested command results to be received over websocket.

        Args:
            facade: Desktop facade that buffers command results by call id.
            call_ids: Command call ids that must all be consumed together.

        Returns:
            Command execution result records in the order returned by the facade.

        Raises:
            TimeoutError: If the remote controller does not return every result
                before ``command_result_timeout_seconds``.
        """

        deadline = asyncio.get_running_loop().time() + self.command_result_timeout_seconds
        while True:
            consumed = await facade.consume_command_results(call_ids)
            if consumed is not None:
                return consumed
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for remote command results: {', '.join(call_ids)}")
            await asyncio.sleep(self.command_poll_interval_seconds)


def command_result_to_resume_block(result: CommandExecutionResultRecord) -> ToolResultBlock:
    """Convert one websocket command result into a ToolResultBlock resume payload.

    Args:
        result: Raw command execution result received from the remote controller.

    Returns:
        NexAU tool result block suitable for the follow-up model resume call.
    """

    return command_result_to_tool_result_block(result)


def _json_object(value: object) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


__all__ = ["DesktopRemoteServerBridge", "command_result_to_resume_block"]
