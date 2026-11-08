"""Internal observation probe executors."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from northcua.core.config import ProbeToolCallConfig
from northcua.core.enums import ResultStatus, WebSocketMessageType
from northcua.core.errors import ObservationProbeExecutionError
from northcua.core.models import CommandExecutionResultRecord
from northcua.core.protocol import CommandRequestPayload, CommandRequestRecord, NorthCUAWebSocketEnvelope
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua.runtime.mcp.result_adapter import normalize_tool_result

from .base import ProbeExecutionRecord, ProbeExecutor


class _LocalMCPServer(Protocol):
    """Subset of an in-process MCP server used by observation probes."""

    async def call_tool(self, tool_name: str, arguments: object) -> object:
        """Execute one MCP tool call.

        Args:
            tool_name: Registered MCP tool name.
            arguments: JSON-like tool arguments.

        Returns:
            Provider-specific MCP result object to be normalized by the caller.
        """

        ...


class LocalInternalProbeExecutor(ProbeExecutor):
    """Execute internal probe chains through registered in-process MCP servers."""

    def __init__(self) -> None:
        """Initialize the local executor server cache."""

        self._servers_by_name: dict[str, _LocalMCPServer] = {}

    async def execute_chain(
        self,
        chain_name: str,
        *,
        session_step: int,
        steps: list[ProbeToolCallConfig],
    ) -> list[ProbeExecutionRecord]:
        """Execute a probe chain through in-process MCP servers.

        Args:
            chain_name: Configured probe chain name.
            session_step: Current session step number, unused locally.
            steps: Probe tool-call steps to execute.

        Returns:
            Probe execution records in step order.

        Raises:
            ObservationProbeExecutionError: If any probe step fails.
        """

        del session_step
        ensure_builtin_mcp_servers_registered()
        records: list[ProbeExecutionRecord] = []
        for index, step in enumerate(steps):
            server = self._servers_by_name.get(step.server)
            if server is None:
                server = NorthCUAMCPRegistry.build(
                    step.server,
                    context=MCPFactoryContext(process_name=f"northcua-observation:{step.server}"),
                )
                self._servers_by_name[step.server] = cast(_LocalMCPServer, server)
                server = self._servers_by_name[step.server]
            try:
                output = normalize_tool_result(await server.call_tool(step.tool, step.arguments))
            except Exception as exc:
                raise ObservationProbeExecutionError(
                    f"Observation probe `{chain_name}` failed at `{step.server}.{step.tool}`: {exc}"
                ) from exc
            records.append(
                ProbeExecutionRecord(
                    chain_name=chain_name,
                    step_index=index,
                    server_name=step.server,
                    tool_name=step.tool,
                    arguments=dict(step.arguments),
                    output=output,
                    source=f"mcp:{step.server}.{step.tool}",
                )
            )
        return records


class RemoteInternalProbeExecutor(ProbeExecutor):
    """Execute internal probe chains through the websocket device command path."""

    def __init__(
        self,
        *,
        session_id: str,
        round_num: int,
        round_step: int,
        send_payload: Callable[[NorthCUAWebSocketEnvelope], Awaitable[None]],
        await_results: Callable[[list[str]], Awaitable[list[CommandExecutionResultRecord]]],
    ) -> None:
        """Initialize remote websocket probe execution.

        Args:
            session_id: Active CUA session id.
            round_num: Current round number.
            round_step: Current round step number.
            send_payload: Callback that sends a websocket command payload.
            await_results: Callback that waits for command results by call id.
        """

        self.session_id = session_id
        self.round_num = round_num
        self.round_step = round_step
        self.send_payload = send_payload
        self.await_results = await_results

    async def execute_chain(
        self,
        chain_name: str,
        *,
        session_step: int,
        steps: list[ProbeToolCallConfig],
    ) -> list[ProbeExecutionRecord]:
        """Execute a probe chain through the websocket command path.

        Args:
            chain_name: Configured probe chain name.
            session_step: Current session step number, unused remotely.
            steps: Probe tool-call steps to execute.

        Returns:
            Probe execution records in step order.

        Raises:
            ObservationProbeExecutionError: If a command result is missing or
                reports failure.
        """

        del session_step
        commands = [
            CommandRequestRecord(
                call_id=f"probe-{uuid.uuid4().hex[:8]}",
                server_name=step.server,
                tool_name=step.tool,
                parameters=dict(step.arguments),
                tool_type="data",
            )
            for step in steps
        ]
        await self.send_payload(
            NorthCUAWebSocketEnvelope.wrap(
                WebSocketMessageType.COMMAND_REQUEST,
                CommandRequestPayload(
                    session_id=self.session_id,
                    round_num=self.round_num,
                    round_step=self.round_step,
                    commands=commands,
                ),
            )
        )
        results = await self.await_results([command.call_id for command in commands])
        result_by_call_id = {result.call_id: result for result in results}
        records: list[ProbeExecutionRecord] = []
        for index, step in enumerate(steps):
            command = commands[index]
            result = result_by_call_id.get(command.call_id)
            if result is None:
                raise ObservationProbeExecutionError(
                    f"Observation probe `{chain_name}` is missing result for `{step.server}.{step.tool}`."
                )
            if result.status != ResultStatus.SUCCESS:
                error = result.error or f"status={result.status.value}"
                raise ObservationProbeExecutionError(
                    f"Observation probe `{chain_name}` failed at `{step.server}.{step.tool}`: {error}"
                )
            records.append(
                ProbeExecutionRecord(
                    chain_name=chain_name,
                    step_index=index,
                    server_name=step.server,
                    tool_name=step.tool,
                    arguments=dict(step.arguments),
                    output=normalize_tool_result(result.result),
                    source=f"mcp:{step.server}.{step.tool}",
                )
            )
        return records
