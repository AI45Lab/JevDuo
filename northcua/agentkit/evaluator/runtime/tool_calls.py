"""Evaluator tool-call execution helpers."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass

from nexau.archs.tool.formatters import ToolFormatterContext
from nexau.archs.tool.tool import Tool

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tools import contract_to_nexau_tool
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.core.models import AssistantToolCallRecord, ToolExecutionRecord

from ..routing import EvaluatorClientToolDispatcher, LocalEvaluatorClientToolDispatcher, PlacementResolution
from ..spec import EvaluatorToolRef
from .model_tool_output import EvaluatorModelToolOutputProjector

logger = logging.getLogger(__name__)
_MODEL_OUTPUT_PROJECTOR = EvaluatorModelToolOutputProjector()


def format_tool_output(context: ToolFormatterContext) -> str:
    """Format evaluator tool output for model-visible tool results.

    Args:
        context: NexAU tool formatter context.

    Returns:
        String representation shown to the evaluator model.
    """

    output = context.tool_output
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, indent=2)


@dataclass(slots=True)
class EvaluatorClientToolBuilder:
    """Build client-side evaluator tools through a placement dispatcher."""

    def build(
        self,
        *,
        placement: PlacementResolution,
        remote: bool,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | None,
    ) -> list[Tool]:
        """Build probe/mutating client tools for one evaluator run.

        Args:
            placement: Resolved placement partition.
            remote: Whether the evaluated target is remote.
            client_tool_dispatcher: Optional explicit remote/local dispatcher.

        Returns:
            NexAU tools backed by the selected dispatcher.

        Raises:
            ValueError: If a remote client tool lacks an explicit dispatcher.
        """

        dispatcher = client_tool_dispatcher
        if dispatcher is None:
            if remote:
                raise ValueError("Remote evaluator client tools require a client_tool_dispatcher.")
            dispatcher = LocalEvaluatorClientToolDispatcher()
        tools: list[Tool] = []
        for tool_spec in (*placement.client_probe_tools, *placement.client_mutating_tools):
            contract = tool_spec.contract
            if contract is None:
                raise ValueError(f"Evaluator client tool {tool_spec.name!r} must be backed by a ToolContract.")
            tools.append(
                contract_to_nexau_tool(
                    contract,
                    implementation=self._implementation(tool_spec=tool_spec, dispatcher=dispatcher, remote=remote),
                    formatter=format_tool_output,
                    disable_parallel=True,
                )
            )
        return tools

    @staticmethod
    def _implementation(
        *,
        tool_spec: EvaluatorToolRef,
        dispatcher: EvaluatorClientToolDispatcher,
        remote: bool,
    ):
        async def _run(**parameters: object) -> object:
            normalized_parameters: JsonObject = {str(key): value for key, value in parameters.items()}
            result = dispatcher.dispatch(tool=tool_spec, parameters=normalized_parameters, remote=remote)
            if inspect.isawaitable(result):
                return await result
            return result

        return _run


@dataclass(slots=True)
class EvaluatorToolCallExecutor:
    """Execute model-emitted evaluator tool calls against a toolset."""

    async def execute(
        self,
        *,
        pending_tool_calls: list[JsonObject],
        trace_state: TraceCaptureState,
        tools: tuple[Tool, ...],
    ) -> list[ToolExecutionRecord]:
        """Execute pending evaluator tool calls and update trace state.

        Args:
            pending_tool_calls: Provider-normalized tool calls.
            trace_state: Captured model trace for the current evaluator step.
            tools: Model-visible evaluator tools.

        Returns:
            Trace-compatible tool execution records.
        """

        tools_by_name = {tool.name: tool for tool in tools}
        executions: list[ToolExecutionRecord] = []
        fallback_calls: list[AssistantToolCallRecord] = []
        for index, call in enumerate(pending_tool_calls, start=1):
            execution, fallback = await self._execute_one(call=call, index=index, tools_by_name=tools_by_name)
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
                "error": f"Unknown evaluator tool: {tool_name}",
                "error_type": "UnknownEvaluatorTool",
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
            output = await tool.execute_async(**tool_input)
        except Exception as exc:
            logger.warning(
                "Evaluator tool execution failed",
                extra={"tool_name": tool_name, "call_id": call_id},
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

    @staticmethod
    def _record(
        *,
        call_id: str,
        tool_name: str,
        tool_input: JsonObject,
        output: JsonObject,
        is_error: bool,
    ) -> ToolExecutionRecord:
        return ToolExecutionRecord(
            tool_call_id=call_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=output,
            llm_tool_output=_MODEL_OUTPUT_PROJECTOR.project(output),
            is_error=is_error,
        )

    @staticmethod
    def _is_error(output: object) -> bool:
        if not isinstance(output, dict):
            return False
        status = str(output.get("status") or "").lower()
        return bool(output.get("error")) or status in {"failure", "error", "failed"}


def pending_tool_calls_from_meta(meta: JsonObject) -> list[JsonObject]:
    """Extract provider-normalized pending tool calls from step metadata.

    Args:
        meta: Metadata returned by the model step executor.

    Returns:
        Normalized pending tool calls, or an empty list.
    """

    raw_pending = meta.get("pending_tool_calls")
    if not isinstance(raw_pending, list):
        return []
    return [dict(call) for call in raw_pending if isinstance(call, dict)]


__all__ = [
    "EvaluatorClientToolBuilder",
    "EvaluatorToolCallExecutor",
    "format_tool_output",
    "pending_tool_calls_from_meta",
]
