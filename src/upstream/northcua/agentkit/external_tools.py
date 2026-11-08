"""Shared external tool dispatch helpers for AgentKit step execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from northcua.app.logging import log_runtime_event
from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord, CommandRequestRecord, ToolExecutionRecord
from northcua.core.protocol import CommandRequestPayload, CommandResultPayload
from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor

logger = logging.getLogger(__name__)


class AgentKitCommandExecutor(Protocol):
    """Port for executing AgentKit MCP command batches."""

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Execute a command batch and return normalized command results."""
        ...


@dataclass(slots=True)
class ExternalToolDispatchResult:
    """Result of one paused external tool batch.

    Attributes:
        command_results: Raw command results returned by the MCP data plane.
        tool_executions: Trace-compatible records replayed into prompt history.
    """

    command_results: list[CommandExecutionResultRecord]
    tool_executions: list[ToolExecutionRecord]


class AgentKitExternalToolDispatcher:
    """Execute model-emitted external tool calls through the MCP data plane.

    The model step stops at agent-owned action tools so the harness can run
    those actions under session, artifact, and policy controls before the next
    observation is built. This helper makes that shape reusable for low-code
    agents.
    """

    def __init__(
        self,
        *,
        server_name: str,
        registration_hook: str = "",
        tool_type: str = "action",
        call_id_prefix: str = "agentkit",
        device_executor: AgentKitCommandExecutor | None = None,
    ) -> None:
        self.server_name = server_name
        self.registration_hook = registration_hook
        self.tool_type = tool_type
        self.call_id_prefix = call_id_prefix
        self.device_executor = device_executor or NorthCUADeviceCommandExecutor(allowed_server_names={server_name})

    async def dispatch_pending_tools(
        self,
        *,
        session_id: str,
        round_num: int,
        round_step: int,
        pending_tool_calls: list[JsonObject],
    ) -> ExternalToolDispatchResult:
        """Execute all pending tool calls and return command plus trace records.

        Args:
            session_id: Active NorthCUA session id used by MCP command routing.
            round_num: One-based user-query round number.
            round_step: One-based model step number inside the round.
            pending_tool_calls: Provider-normalized tool calls waiting to run.

        Returns:
            Raw command results plus normalized tool execution records.
        """

        commands = command_records_from_pending_tool_calls(
            pending_tool_calls=pending_tool_calls,
            server_name=self.server_name,
            registration_hook=self.registration_hook,
            tool_type=self.tool_type,
            call_id_prefix=self.call_id_prefix,
            round_num=round_num,
            round_step=round_step,
        )
        command_payload = CommandRequestPayload(
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            commands=commands,
        )
        command_result = await self.device_executor.execute_command_request(command_payload)
        command_results = list(command_result.results)
        tool_executions = tool_execution_records_from_command_results(
            pending_tool_calls=pending_tool_calls,
            command_results=command_results,
        )
        log_runtime_event(
            logger,
            "agentkit_external_tools_dispatched",
            level=logging.DEBUG,
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            server_name=self.server_name,
            tool_names=[command.tool_name for command in commands],
            command_count=len(commands),
            failure_count=sum(1 for result in command_results if result.status == ResultStatus.FAILURE),
        )
        return ExternalToolDispatchResult(
            command_results=command_results,
            tool_executions=tool_executions,
        )


def pending_tool_calls_from_meta(meta: JsonObject) -> list[JsonObject]:
    """Extract pending external tool calls from a model-step metadata payload.

    Args:
        meta: Metadata returned by the shared model step executor.

    Returns:
        List of JSON tool-call objects, or an empty list when absent.
    """

    raw_pending = meta.get("pending_tool_calls")
    if not isinstance(raw_pending, list):
        return []
    return [dict(call) for call in raw_pending if isinstance(call, dict)]


def command_records_from_pending_tool_calls(
    *,
    pending_tool_calls: list[JsonObject],
    server_name: str,
    registration_hook: str = "",
    tool_type: str = "action",
    call_id_prefix: str = "agentkit",
    round_num: int,
    round_step: int,
) -> list[CommandRequestRecord]:
    """Build MCP command records from transport-normalized tool calls.

    Args:
        pending_tool_calls: Provider-normalized tool calls waiting to run.
        server_name: MCP server that should receive each command.
        registration_hook: Optional hook used to register agent-owned servers.
        tool_type: Command category recorded in runtime traces.
        call_id_prefix: Prefix used to make command ids unique and readable.
        round_num: One-based user-query round number.
        round_step: One-based model step number inside the round.

    Returns:
        Command request records ready for the MCP device executor.
    """

    commands: list[CommandRequestRecord] = []
    for index, call in enumerate(pending_tool_calls, start=1):
        raw_input = call.get("input") or {}
        parameters: JsonObject = dict(raw_input) if isinstance(raw_input, dict) else {}
        tool_call_id = str(call.get("id") or f"tool-{index}")
        routed_server_name = str(call.get("server_name") or server_name or "")
        commands.append(
            CommandRequestRecord(
                call_id=f"{call_id_prefix}:action:{round_num}:{round_step}:{tool_call_id}",
                server_name=routed_server_name,
                tool_name=str(call.get("name") or ""),
                registration_hook=registration_hook,
                parameters=parameters,
                tool_type=tool_type,
            )
        )
    return commands


def tool_execution_records_from_command_results(
    *,
    pending_tool_calls: list[JsonObject],
    command_results: list[CommandExecutionResultRecord],
) -> list[ToolExecutionRecord]:
    """Mirror externally dispatched tool results into trace-compatible records.

    Args:
        pending_tool_calls: Tool calls originally emitted by the model.
        command_results: Results returned by local or remote MCP execution.

    Returns:
        Tool execution records suitable for llm trace and history replay.
    """

    results_by_call_id = {result.call_id: result for result in command_results}
    executions: list[ToolExecutionRecord] = []
    for index, tool_call in enumerate(pending_tool_calls, start=1):
        source_call_id = str(tool_call.get("id") or f"tool-{index}")
        matching_result = results_by_call_id.get(source_call_id) or next(
            (result for result in command_results if result.call_id.endswith(source_call_id)),
            None,
        )
        if matching_result is None:
            continue
        raw_input = tool_call.get("input") or {}
        tool_input: JsonObject = dict(raw_input) if isinstance(raw_input, dict) else {}
        tool_output = _trace_tool_output(matching_result)
        executions.append(
            ToolExecutionRecord(
                tool_call_id=source_call_id,
                tool_name=str(tool_call.get("name") or ""),
                tool_input=tool_input,
                tool_output=tool_output,
                llm_tool_output=tool_output,
                is_error=_is_error_result(matching_result),
            )
        )
    return executions


def _trace_tool_output(result: CommandExecutionResultRecord) -> JsonObject | str | None:
    if result.status == ResultStatus.SUCCESS:
        return result.result
    return {
        "status": "error",
        "error": result.error or result.call_id,
        "error_type": "ToolExecutionError",
    }


def _is_error_result(result: CommandExecutionResultRecord) -> bool:
    if result.status == ResultStatus.FAILURE:
        return True
    payload = result.result
    return isinstance(payload, dict) and payload.get("status") == "error"
