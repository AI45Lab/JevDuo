"""Resolve web-agent tool calls from captured NexAU traces."""

from __future__ import annotations

from dataclasses import dataclass

from northcua.agentkit.tool_trace import ResolvedToolExecution, resolve_tool_executions
from northcua.core.json_types import JsonObject
from northcua.core.models import ToolExecutionRecord

from ..domain.actions import WebAgentActionType, action_from_tool_name
from ..domain.results import WebToolRecord
from ..mcp.error_classification import classify_web_tool_error


@dataclass(slots=True, frozen=True)
class ResolvedWebAgentAction(ResolvedToolExecution):
    """One model-visible web tool execution resolved from a trace.

    Attributes:
        action: Internal action type derived from the model-visible tool name.
        tool_name: Original tool name emitted by the provider.
        tool_input: Validated JSON-like input captured for the tool call.
        tool_output: Tool result payload captured after dispatch.
        is_error: Whether the tool layer reported the execution as failed.
        tool_call_id: Provider tool-call identifier, when available.
    """

    action: WebAgentActionType
    tool_name: str
    tool_input: JsonObject
    tool_output: JsonObject | str | None
    is_error: bool = False
    tool_call_id: str = ""

    @property
    def is_retryable_error(self) -> bool:
        """Return whether this error should be fed back to the model."""

        return self.is_error and isinstance(self.tool_output, dict) and self.tool_output.get("retryable") is True


class WebAgentActionResolver:
    """Resolve web-agent tool executions from a captured step trace."""

    def resolve_all(self, tool_executions: list[ToolExecutionRecord]) -> list[ResolvedWebAgentAction]:
        """Return all web-agent tool executions in provider execution order.

        Args:
            tool_executions: Tool records captured by the shared model-step
                executor for the current provider response.

        Returns:
            Web-agent actions only, preserving the provider execution order.
        """

        def build(action: WebAgentActionType, execution: ToolExecutionRecord) -> ResolvedWebAgentAction:
            """Convert one captured execution into a web-agent action record.

            Args:
                action: Internal action type resolved from the tool name.
                execution: Captured tool execution record.

            Returns:
                Resolved action with original input/output preserved.
            """

            return ResolvedWebAgentAction(
                action=action,
                tool_name=execution.tool_name,
                tool_input=execution.tool_input,
                tool_output=self._normalize_error_output(
                    action=action,
                    tool_output=execution.tool_output,
                    is_error=execution.is_error,
                ),
                is_error=execution.is_error,
                tool_call_id=execution.tool_call_id,
            )

        return resolve_tool_executions(
            tool_executions,
            resolve_action=action_from_tool_name,
            build=build,
        )

    @staticmethod
    def _normalize_error_output(
        *,
        action: WebAgentActionType,
        tool_output: JsonObject | str | None,
        is_error: bool,
    ) -> JsonObject | str | None:
        """Add retry metadata to framework-level web tool errors."""

        if not is_error or not isinstance(tool_output, dict) or tool_output.get("retryable") is not None:
            return tool_output
        message = str(tool_output.get("error") or tool_output.get("message") or tool_output.get("text") or "")
        if not message:
            return tool_output
        classification = classify_web_tool_error(RuntimeError(message))
        if not classification.retryable and classification.code == "web_tool_error":
            return tool_output
        normalized = dict(tool_output)
        normalized.setdefault("status", "error")
        normalized.setdefault("action", action.value)
        normalized.setdefault("code", classification.code)
        normalized.setdefault("retryable", classification.retryable)
        if classification.hint:
            normalized.setdefault("hint", classification.hint)
        return normalized

    def resolve(self, tool_executions: list[ToolExecutionRecord]) -> ResolvedWebAgentAction | None:
        """Return the last web-agent tool execution, if any.

        Args:
            tool_executions: Tool records captured for the current model step.

        Returns:
            Last resolved web action, or ``None`` when no web tool was called.
        """

        resolved = self.resolve_all(tool_executions)
        return resolved[-1] if resolved else None

    @staticmethod
    def to_record(action: ResolvedWebAgentAction) -> WebToolRecord:
        """Convert a resolved dataclass into a persisted Pydantic record.

        Args:
            action: Resolved action produced by this resolver.

        Returns:
            Pydantic record suitable for trace/artifact serialization.
        """

        return WebToolRecord(
            action=action.action,
            tool_name=action.tool_name,
            tool_input=action.tool_input,
            tool_output=action.tool_output,
            is_error=action.is_error,
            tool_call_id=action.tool_call_id,
        )
