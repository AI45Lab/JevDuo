# pyright: reportMissingImports=false
"""Generic pending tool-call execution for AgentKit model steps."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass

from nexau.archs.tool.tool import Tool

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.core.models import AssistantToolCallRecord, ToolExecutionRecord

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentKitToolCallExecutor:
    """Execute provider-normalized pending tool calls against NexAU tools."""

    async def execute(
        self,
        *,
        pending_tool_calls: list[JsonObject],
        trace_state: TraceCaptureState,
        tools: tuple[Tool, ...],
        tool_context: dict[str, object] | None = None,
        tool_context_tool_names: frozenset[str] = frozenset(),
    ) -> list[ToolExecutionRecord]:
        """Execute pending tool calls and append trace-compatible results.

        Args:
            pending_tool_calls: Provider-normalized calls with ``id``,
                ``name``, and ``input``.
            trace_state: Captured model trace state to update.
            tools: Tool objects visible to the model.
            tool_context: Optional runtime-only parameters. A context value is
                passed only when the target tool implementation explicitly
                declares a compatible keyword parameter, and it is never
                recorded as a model argument.
            tool_context_tool_names: Tool names that should receive the full
                runtime context because their provider runtime owns reserved
                parameter compatibility.

        Returns:
            Executed tool records.
        """

        pending_tool_calls = _pending_tool_calls_needing_dispatch(pending_tool_calls, trace_state)
        tools_by_name = {tool.name: tool for tool in tools}
        executions: list[ToolExecutionRecord] = []
        fallback_calls: list[AssistantToolCallRecord] = []
        scheduled_calls = _serial_tool_schedule(pending_tool_calls, tools_by_name=tools_by_name)
        for index, scheduled in enumerate(scheduled_calls, start=1):
            call = scheduled.call
            if scheduled.skip_reason:
                execution, fallback = self._skipped_one(call=call, index=index, skip_reason=scheduled.skip_reason)
            else:
                execution, fallback = await self._execute_one(
                    call=call,
                    index=index,
                    tools_by_name=tools_by_name,
                    tool_context=tool_context or {},
                    tool_context_tool_names=tool_context_tool_names,
                )
            executions.append(execution)
            fallback_calls.append(fallback)
        if not trace_state.assistant_tool_calls:
            trace_state.assistant_tool_calls = fallback_calls
        trace_state.tool_executions.extend(executions)
        return executions

    async def _execute_one(
        self,
        *,
        call: JsonObject,
        index: int,
        tools_by_name: dict[str, Tool],
        tool_context: dict[str, object],
        tool_context_tool_names: frozenset[str],
    ) -> tuple[ToolExecutionRecord, AssistantToolCallRecord]:
        call_id = str(call.get("id") or f"tool_call_{index}")
        tool_name = str(call.get("name") or "")
        raw_input = call.get("input")
        tool_input: JsonObject = (
            {str(key): value for key, value in raw_input.items()} if isinstance(raw_input, dict) else {}
        )
        fallback = AssistantToolCallRecord(
            id=call_id,
            name=tool_name,
            arguments=json.dumps(tool_input, ensure_ascii=False),
        )
        tool = tools_by_name.get(tool_name)
        if tool is None:
            output: JsonObject = {
                "status": "failure",
                "error": f"Unknown AgentKit tool: {tool_name}",
                "error_type": "UnknownAgentKitTool",
            }
            return (
                self._record(
                    call_id=call_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    output=output,
                    is_error=True,
                ),
                fallback,
            )
        try:
            output = await tool.execute_async(
                **tool_input,
                **(
                    tool_context
                    if tool_name in tool_context_tool_names
                    else _compatible_tool_context(tool=tool, tool_context=tool_context)
                ),
            )
        except Exception as exc:
            logger.warning(
                "AgentKit tool execution failed",
                extra={"tool_name": tool_name, "tool_call_id": call_id},
                exc_info=True,
            )
            output = {
                "status": "failure",
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        return (
            self._record(
                call_id=call_id,
                tool_name=tool_name,
                tool_input=tool_input,
                output=output,
                is_error=self._is_error(output),
            ),
            fallback,
        )

    def _skipped_one(
        self,
        *,
        call: JsonObject,
        index: int,
        skip_reason: str,
    ) -> tuple[ToolExecutionRecord, AssistantToolCallRecord]:
        call_id = str(call.get("id") or f"tool_call_{index}")
        tool_name = str(call.get("name") or "")
        raw_input = call.get("input")
        tool_input: JsonObject = (
            {str(key): value for key, value in raw_input.items()} if isinstance(raw_input, dict) else {}
        )
        fallback = AssistantToolCallRecord(
            id=call_id,
            name=tool_name,
            arguments=json.dumps(tool_input, ensure_ascii=False),
        )
        return (
            self._record(
                call_id=call_id,
                tool_name=tool_name,
                tool_input=tool_input,
                output={"status": "skipped", "summary": skip_reason},
                is_error=False,
            ),
            fallback,
        )

    @staticmethod
    def _record(
        *,
        call_id: str,
        tool_name: str,
        tool_input: JsonObject,
        output: object,
        is_error: bool,
    ) -> ToolExecutionRecord:
        return ToolExecutionRecord(
            tool_call_id=call_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=output if isinstance(output, (dict, str)) else str(output),
            llm_tool_output=output if isinstance(output, (dict, str)) else str(output),
            is_error=is_error,
        )

    @staticmethod
    def _is_error(output: object) -> bool:
        if not isinstance(output, dict):
            return False
        status = str(output.get("status") or "").lower()
        return bool(output.get("error")) or status in {"failure", "error", "failed"}


@dataclass(frozen=True, slots=True)
class _ScheduledToolCall:
    call: JsonObject
    skip_reason: str = ""


def _serial_tool_schedule(
    pending_tool_calls: list[JsonObject], *, tools_by_name: dict[str, Tool]
) -> list[_ScheduledToolCall]:
    scheduled: list[_ScheduledToolCall] = []
    serial_tool_seen = ""
    for call in pending_tool_calls:
        if serial_tool_seen:
            scheduled.append(
                _ScheduledToolCall(
                    call=call,
                    skip_reason=f"Skipped because {serial_tool_seen} is a serial terminal tool.",
                )
            )
            continue
        scheduled.append(_ScheduledToolCall(call=call))
        tool = tools_by_name.get(str(call.get("name") or ""))
        if tool is not None and bool(getattr(tool, "disable_parallel", False)):
            serial_tool_seen = tool.name
    return scheduled


def _pending_tool_calls_needing_dispatch(
    pending_tool_calls: list[JsonObject], trace_state: TraceCaptureState
) -> list[JsonObject]:
    executed_ids = {execution.tool_call_id for execution in trace_state.tool_executions if execution.tool_call_id}
    if not executed_ids:
        return pending_tool_calls
    return [call for call in pending_tool_calls if str(call.get("id") or "") not in executed_ids]


def _compatible_tool_context(*, tool: Tool, tool_context: dict[str, object]) -> dict[str, object]:
    """Return runtime context keys accepted by one tool implementation.

    Args:
        tool: NexAU tool that will be dispatched.
        tool_context: Runtime-only values supplied by an agent adapter.

    Returns:
        Context filtered to keyword parameters that the tool explicitly
        declares. Tools with ``**kwargs`` do not receive the whole context by
        default; this keeps generic AgentKit tools from accidentally accepting
        private runtime state.
    """

    if not tool_context:
        return {}
    implementation = getattr(tool, "implementation", None)
    if implementation is None:
        return {}
    try:
        signature = inspect.signature(implementation)
    except (TypeError, ValueError):
        return {}
    accepted: dict[str, object] = {}
    for name, value in tool_context.items():
        parameter = signature.parameters.get(name)
        if parameter is None:
            continue
        if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}:
            accepted[name] = value
    return accepted


def pending_tool_calls_from_meta(meta: JsonObject) -> list[JsonObject]:
    """Extract provider-normalized pending tool calls from model metadata.

    Args:
        meta: Metadata returned by the shared model step executor.

    Returns:
        Pending tool calls ready for local or remote dispatch.
    """

    raw_pending = meta.get("pending_tool_calls")
    if not isinstance(raw_pending, list):
        return []
    return [dict(call) for call in raw_pending if isinstance(call, dict)]


def pending_tool_calls_from_trace(trace_state: TraceCaptureState) -> list[JsonObject]:
    """Fallback pending calls from captured assistant tool-call records.

    Args:
        trace_state: Captured model response state.

    Returns:
        Tool calls reconstructed from assistant tool-call records.
    """

    calls: list[JsonObject] = []
    for tool_call in trace_state.assistant_tool_calls:
        try:
            parsed = json.loads(tool_call.arguments) if tool_call.arguments else {}
        except json.JSONDecodeError:
            parsed = {}
        calls.append(
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "input": parsed if isinstance(parsed, dict) else {},
            }
        )
    return calls


__all__ = [
    "AgentKitToolCallExecutor",
    "pending_tool_calls_from_meta",
    "pending_tool_calls_from_trace",
]
