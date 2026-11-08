"""Local external-tool dispatch helpers for desktop round execution."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Protocol, cast

from nexau.archs.main_sub.execution.hooks import AfterToolHookInput, BeforeToolHookInput

from northcua.agentkernel.json_types import JsonObject
from northcua.app.logging import log_runtime_event
from northcua.core.enums import ResultStatus
from northcua.core.models import CommandExecutionResultRecord, CommandRequestRecord, ToolExecutionRecord
from northcua.core.protocol import CommandRequestPayload, CommandResultPayload
from northcua.transport.ws.result_blocks import RemoteToolDispatchResult, coerce_remote_tool_dispatch_result
from northcua_runtime_shared.tool_contracts import ToolPlacement
from northcua_runtime_shared.tool_contracts.runtime_metadata import (
    tool_contract_projection_is_current,
    tool_contract_projection_placement,
    tool_route_server_name,
)

logger = logging.getLogger(__name__)


class CommandExecutorProtocol(Protocol):
    """Command executor surface required by local external tool dispatch."""

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Execute a local command request batch.

        Args:
            payload: Batched desktop command request produced from paused tool calls.

        Returns:
            Batched command results preserving each command call id.
        """
        ...


class LocalExternalToolDispatcher:
    """Execute one paused batch of external desktop tools locally."""

    def __init__(self, *, device_executor: CommandExecutorProtocol) -> None:
        self._device_executor = device_executor

    def set_device_executor(self, device_executor: CommandExecutorProtocol) -> None:
        """Update the active device executor used for local tool batches.

        Args:
            device_executor: Desktop command executor that should receive the
                next paused tool batch.
        """

        self._device_executor = device_executor

    async def execute(
        self,
        *,
        session_id: str,
        round_num: int,
        round_step: int,
        pending_tool_calls: list[JsonObject],
        agent: object,
        agent_state: object,
    ) -> tuple[list[CommandExecutionResultRecord], list[ToolExecutionRecord]]:
        """Execute a paused tool batch and synthesize trace-friendly records.

        Args:
            session_id: Session identifier used for command logging.
            round_num: User-facing round number.
            round_step: Step number within the current round.
            pending_tool_calls: Tool calls emitted by the model when it paused.
            agent: Prepared NexAU agent that may expose middleware hooks.
            agent_state: Runtime state passed through NexAU middleware hooks.

        Returns:
            Pair of command execution results and normalized tool execution
            records suitable for llm_trace persistence.
        """

        middleware_manager = getattr(getattr(agent, "executor", None), "middleware_manager", None)
        payload = await self._build_command_payload(
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            pending_tool_calls=pending_tool_calls,
            middleware_manager=middleware_manager,
            agent_state=agent_state,
        )
        results = await self._execute_commands(payload=payload, agent=agent)
        result_status = (
            ResultStatus.SUCCESS if all(row.status == ResultStatus.SUCCESS for row in results) else ResultStatus.FAILURE
        )
        log_runtime_event(
            logger,
            "round_local_command_batch_executed",
            level=logging.DEBUG,
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            command_count=len(payload.commands),
            command_names=[command.tool_name for command in payload.commands],
            overall_status=result_status,
        )
        executions = await self._build_execution_records(
            payload=payload,
            results=results,
            middleware_manager=middleware_manager,
            agent_state=agent_state,
        )
        return results, executions

    async def _execute_commands(
        self, *, payload: CommandRequestPayload, agent: object
    ) -> list[CommandExecutionResultRecord]:
        """Keep owned local callbacks on the harness, preserving batch order.

        Only current ToolContract projections explicitly placed at LOCAL_HANDLER
        qualify. Missing route metadata alone never authorizes local execution.
        Consecutive device commands retain their existing batched transport.
        """

        tools = getattr(getattr(agent, "config", None), "tools", [])
        local_tools = {
            tool.name: tool
            for tool in tools
            if tool_contract_projection_is_current(tool)
            and tool_contract_projection_placement(tool) == ToolPlacement.LOCAL_HANDLER.value
            and not tool_route_server_name(tool)
            and callable(getattr(tool, "implementation", None))
        }
        if not local_tools:
            reply = await self._device_executor.execute_command_request(payload)
            return list(reply.results)
        results: list[CommandExecutionResultRecord] = []
        queued: list[CommandRequestRecord] = []

        async def flush_device_commands() -> None:
            if queued:
                batch = payload.model_copy(update={"commands": list(queued)})
                reply = await self._device_executor.execute_command_request(batch)
                results.extend(reply.results)
                queued.clear()

        for command in payload.commands:
            tool = local_tools.get(command.tool_name) if not command.server_name else None
            if tool is None:
                queued.append(command)
                continue
            await flush_device_commands()
            try:
                handler = tool.implementation
                if inspect.iscoroutinefunction(handler):
                    output = await handler(**command.parameters)
                else:
                    output = await asyncio.to_thread(handler, **command.parameters)
                if inspect.isawaitable(output):
                    output = await output
                row = CommandExecutionResultRecord(
                    call_id=command.call_id,
                    status=ResultStatus.SUCCESS,
                    result=self._normalize_tool_output(output),
                )
            except Exception as exc:
                row = CommandExecutionResultRecord(
                    call_id=command.call_id, status=ResultStatus.FAILURE, error=f"{type(exc).__name__}: {exc}"
                )
            results.append(row)
        await flush_device_commands()
        return results

    async def _build_command_payload(
        self,
        *,
        session_id: str,
        round_num: int,
        round_step: int,
        pending_tool_calls: list[JsonObject],
        middleware_manager,
        agent_state: object,
    ) -> CommandRequestPayload:
        """Apply before-tool middleware and build the runtime command payload."""

        payload = CommandRequestPayload(
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            commands=[],
        )
        for call in pending_tool_calls:
            call_id = str(call.get("id") or "")
            tool_name = str(call.get("name") or "")
            raw_parameters = call.get("input")
            parameters: JsonObject = (
                {str(key): value for key, value in raw_parameters.items()} if isinstance(raw_parameters, dict) else {}
            )
            if tool_name == "keypress" and parameters.get("key") == "None" and isinstance(parameters.get("keys"), list):
                parameters["key"] = None
            if middleware_manager is not None and agent_state is not None:
                # NexAU middleware may rewrite inputs; the command payload must
                # reflect the post-middleware parameters seen by the runtime.
                before_tool_input = BeforeToolHookInput(
                    agent_state=agent_state,  # type: ignore[arg-type]
                    sandbox=None,
                    tool_name=tool_name,
                    tool_call_id=call_id,
                    tool_input=parameters,
                )
                parameters = cast(
                    JsonObject,
                    await asyncio.to_thread(
                        middleware_manager.run_before_tool,
                        before_tool_input,
                    ),
                )
            payload.commands.append(
                CommandRequestRecord(
                    call_id=call_id,
                    tool_name=tool_name,
                    server_name=str(call.get("server_name") or ""),
                    parameters=parameters,
                    tool_type="action",
                )
            )
        return payload

    async def _build_execution_records(
        self,
        *,
        payload: CommandRequestPayload,
        results: list[CommandExecutionResultRecord],
        middleware_manager,
        agent_state: object,
    ) -> list[ToolExecutionRecord]:
        """Run after-tool middleware and synthesize trace execution records."""

        results_by_call_id = {result.call_id: result for result in results}
        executions: list[ToolExecutionRecord] = []
        for command in payload.commands:
            result = results_by_call_id.get(command.call_id)
            if result is None:
                continue
            tool_output = self._execution_output(result)
            llm_tool_output = tool_output
            if middleware_manager is not None and agent_state is not None:
                # Run after-tool middleware before writing trace rows so
                # llm_tool_output mirrors what the model would receive.
                after_tool_input = AfterToolHookInput(
                    agent_state=agent_state,  # type: ignore[arg-type]
                    sandbox=None,
                    tool_name=command.tool_name,
                    tool_call_id=command.call_id,
                    tool_input={str(key): value for key, value in command.parameters.items()},
                    tool_output=tool_output,
                    llm_tool_output=llm_tool_output,
                )
                tool_output, llm_tool_output = await asyncio.to_thread(
                    middleware_manager.run_after_tool,
                    after_tool_input,
                    tool_output,
                    llm_tool_output,
                )
            # Trace persistence and resume replay expect JSON-like payloads;
            # stringify runtime-specific objects without changing command status.
            normalized_tool_output = self._normalize_tool_output(tool_output)
            normalized_llm_tool_output = self._normalize_tool_output(llm_tool_output)
            result.result = normalized_tool_output
            executions.append(
                ToolExecutionRecord(
                    tool_call_id=command.call_id,
                    tool_name=command.tool_name,
                    tool_input=dict(command.parameters),
                    tool_output=normalized_tool_output,
                    llm_tool_output=normalized_llm_tool_output,
                    is_error=result.status == ResultStatus.FAILURE,
                )
            )
        return executions

    @staticmethod
    def _execution_output(result: CommandExecutionResultRecord) -> JsonObject | str | None:
        """Return the trace/model-facing output for one command result."""

        if result.result is not None:
            return result.result
        if result.status == ResultStatus.FAILURE:
            return {
                "status": "error",
                "error": result.error or "Tool execution failed before returning a result.",
            }
        return None

    @staticmethod
    def _normalize_tool_output(value: object) -> JsonObject | str | None:
        """Return a JSON-friendly tool output without changing semantic status."""

        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        return value if isinstance(value, str) or value is None else str(value)


class RemoteExternalCommandExecutor:
    """Command executor adapter that sends desktop commands to a remote device."""

    def __init__(self, dispatch_external_calls) -> None:
        """Initialize the remote command executor.

        Args:
            dispatch_external_calls: Controller/device dispatch callback used by
                the remote desktop runner.
        """

        self._dispatch_external_calls = dispatch_external_calls

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Dispatch a desktop command batch through the remote data plane.

        Args:
            payload: Batched desktop command request created by the shared
                external-tool dispatcher.

        Returns:
            Command result payload with raw command rows attached by the remote
            dispatcher. Missing rows are represented as failures so callers do
            not silently persist incomplete tool results.
        """

        pending = [
            {
                "id": command.call_id,
                "name": command.tool_name,
                "server_name": command.server_name,
                "input": dict(command.parameters),
            }
            for command in payload.commands
        ]
        dispatch_result = coerce_remote_tool_dispatch_result(await self._dispatch_external_calls(pending))
        command_results = _command_results_from_raw_batch(payload=payload, dispatch_result=dispatch_result)
        status = (
            ResultStatus.SUCCESS
            if all(result.status == ResultStatus.SUCCESS for result in command_results)
            else ResultStatus.FAILURE
        )
        return CommandResultPayload(session_id=payload.session_id, status=status, results=command_results)


def _command_results_from_raw_batch(
    *,
    payload: CommandRequestPayload,
    dispatch_result: RemoteToolDispatchResult,
) -> list[CommandExecutionResultRecord]:
    """Normalize raw remote command rows and fill gaps with failures."""

    results_by_call_id = {result.call_id: result for result in dispatch_result.command_results}
    resolved: list[CommandExecutionResultRecord] = []
    for command in payload.commands:
        result = results_by_call_id.get(command.call_id)
        if result is not None:
            resolved.append(result)
            continue
        resolved.append(
            CommandExecutionResultRecord(
                call_id=command.call_id,
                status=ResultStatus.FAILURE,
                error="Remote dispatcher did not return a raw command result.",
                result={
                    "status": "error",
                    "error": "Remote dispatcher did not return a raw command result.",
                },
            )
        )
    return resolved
