"""Common helpers for projecting NexAU tool traces into agent records."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from northcua.core.json_types import JsonObject
from northcua.core.models import ToolExecutionRecord

ActionT = TypeVar("ActionT")
ResolvedExecutionT = TypeVar("ResolvedExecutionT", bound="ResolvedToolExecution")


@dataclass(slots=True, frozen=True)
class ResolvedToolExecution:
    """One model-visible tool execution resolved from a NexAU trace.

    Attributes:
        action: Agent-specific action object resolved from the tool name.
        tool_name: Provider-facing tool name emitted by the model.
        tool_input: JSON-compatible arguments sent to the tool.
        tool_output: Tool result payload after dispatch and normalization.
        is_error: Whether the result represents a failed tool execution.
    """

    action: object
    tool_name: str
    tool_input: JsonObject
    tool_output: JsonObject | str | None
    is_error: bool = False


@dataclass(slots=True, frozen=True)
class ToolExecutionResolver(Generic[ActionT, ResolvedExecutionT]):
    """Resolve captured provider tool executions into agent-owned actions.

    Attributes:
        resolve_action: Strategy mapping tool names to agent-owned actions.
        build: Strategy converting an action and trace record into output.
    """

    resolve_action: Callable[[str], ActionT | None]
    build: Callable[[ActionT, ToolExecutionRecord], ResolvedExecutionT]

    def resolve(self, executions: Sequence[ToolExecutionRecord]) -> list[ResolvedExecutionT]:
        """Resolve all agent-owned tools in provider execution order.

        Args:
            executions: Captured tool execution records from one model step.

        Returns:
            Resolved executions for tool names owned by the current agent.
        """

        resolved: list[ResolvedExecutionT] = []
        for execution in executions:
            action = self.resolve_action(execution.tool_name)
            if action is None:
                continue
            resolved.append(self.build(action, execution))
        return resolved


class ToolOutputProjector:
    """Project resolved tool outputs into compact prompt and terminal summaries."""

    compact_keys = (
        "status",
        "text",
        "action",
        "answer",
        "success",
        "code",
        "error",
        "error_type",
        "retryable",
        "hint",
    )

    def compact_output(self, output: JsonObject | str | None) -> JsonObject:
        """Return a compact prompt-safe summary for one tool output.

        Args:
            output: Raw JSON, text, or missing tool output.

        Returns:
            Small JSON object containing fields useful for history replay.
        """

        if isinstance(output, dict):
            compact: JsonObject = {}
            for key in self.compact_keys:
                if key in output:
                    compact[key] = output[key]
            observation = output.get("observation")
            if isinstance(observation, dict):
                compact["observation"] = self._compact_observation(observation)
            return compact
        if isinstance(output, str):
            return {"text": output}
        return {}

    def _compact_observation(self, observation: JsonObject) -> JsonObject:
        page = observation.get("page")
        snapshot = observation.get("snapshot")
        screenshot = observation.get("screenshot")
        compact: JsonObject = {
            "observation_id": observation.get("observation_id", ""),
            "settled": bool(observation.get("settled", True)),
            "settle_reason": str(observation.get("settle_reason", "")),
        }
        if isinstance(page, dict):
            compact["page"] = {
                "url": str(page.get("url") or ""),
                "title": str(page.get("title") or ""),
            }
        if isinstance(snapshot, dict):
            compact["snapshot"] = {
                "ref_count": self._safe_int(snapshot.get("ref_count")),
            }
        if isinstance(screenshot, dict):
            compact["screenshot"] = {
                "kind": str(screenshot.get("kind") or ""),
                "image_attached": bool(screenshot.get("image_attached", False)),
                "image_omitted_reason": str(screenshot.get("image_omitted_reason") or ""),
            }
        return compact

    def _safe_int(self, value: object) -> int:
        if isinstance(value, bool) or value is None:
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return 0
        return 0

    def output_text(self, output: JsonObject | str | None) -> str:
        """Extract a human-readable line from a tool output payload.

        Args:
            output: Raw JSON, text, or missing tool output.

        Returns:
            Best-effort text suitable for terminal and prompt summaries.
        """

        if isinstance(output, str):
            return output
        if isinstance(output, dict):
            for key in ("error", "text", "answer"):
                value = output.get(key)
                if value:
                    return str(value)
        return ""

    def compact_outputs(self, actions: Sequence[ResolvedToolExecution]) -> JsonObject:
        """Return a compact summary for one or more tool executions.

        Args:
            actions: Resolved executions from a single model step.

        Returns:
            Single-tool summary or batched ``tools`` summary.
        """

        if len(actions) == 1:
            return self.compact_output(actions[0].tool_output)
        return {
            "tools": [
                {
                    "tool_name": action.tool_name,
                    "is_error": action.is_error,
                    "result": self.compact_output(action.tool_output),
                }
                for action in actions
            ]
        }

    def history_name(self, actions: Sequence[ResolvedToolExecution]) -> str:
        """Return a compact name for a single or batched tool step.

        Args:
            actions: Resolved executions from a single model step.

        Returns:
            Empty string, one tool name, or a comma-separated batch name.
        """

        if not actions:
            return ""
        if len(actions) == 1:
            return actions[0].tool_name
        return ", ".join(action.tool_name for action in actions)

    def history_text(self, actions: Sequence[ResolvedToolExecution]) -> str:
        """Return readable history text for a single or batched tool step.

        Args:
            actions: Resolved executions from a single model step.

        Returns:
            Newline-delimited tool status lines for prompt history.
        """

        lines: list[str] = []
        for action in actions:
            status = "error" if action.is_error else "ok"
            text = self.output_text(action.tool_output) or "(no text)"
            lines.append(f"{action.tool_name} [{status}]: {text}")
        return "\n".join(lines)


_DEFAULT_OUTPUT_PROJECTOR = ToolOutputProjector()


def resolve_tool_executions(
    executions: Sequence[ToolExecutionRecord],
    *,
    resolve_action: Callable[[str], ActionT | None],
    build: Callable[[ActionT, ToolExecutionRecord], ResolvedExecutionT],
) -> list[ResolvedExecutionT]:
    """Resolve all agent-owned tools in provider execution order.

    Args:
        executions: Captured tool execution records from the model step.
        resolve_action: Function mapping tool names to agent-owned actions.
        build: Function that converts an action and trace record into output.

    Returns:
        Resolved executions for tools owned by the current agent.
    """

    return ToolExecutionResolver(resolve_action=resolve_action, build=build).resolve(executions)


def compact_tool_output(output: JsonObject | str | None) -> JsonObject:
    """Return a compact prompt-safe summary for one tool output.

    Args:
        output: Raw JSON, text, or missing tool output.

    Returns:
        Small JSON object containing only fields useful for history replay.
    """

    return _DEFAULT_OUTPUT_PROJECTOR.compact_output(output)


def tool_output_text(output: JsonObject | str | None) -> str:
    """Extract a human-readable line from a tool output payload.

    Args:
        output: Raw JSON, text, or missing tool output.

    Returns:
        Best-effort text suitable for terminal and prompt summaries.
    """

    return _DEFAULT_OUTPUT_PROJECTOR.output_text(output)


def compact_tool_outputs(actions: Sequence[ResolvedToolExecution]) -> JsonObject:
    """Return a compact summary for one or more tool executions.

    Args:
        actions: Resolved executions from a single model step.

    Returns:
        Single-tool summary or batched ``tools`` summary.
    """

    return _DEFAULT_OUTPUT_PROJECTOR.compact_outputs(actions)


def tool_history_name(actions: Sequence[ResolvedToolExecution]) -> str:
    """Return a compact name for a single or batched tool step.

    Args:
        actions: Resolved executions from a single model step.

    Returns:
        Empty string, one tool name, or a comma-separated batch name.
    """

    return _DEFAULT_OUTPUT_PROJECTOR.history_name(actions)


def tool_history_text(actions: Sequence[ResolvedToolExecution]) -> str:
    """Return readable history text for a single or batched tool step.

    Args:
        actions: Resolved executions from a single model step.

    Returns:
        Newline-delimited tool status lines for prompt history.
    """

    return _DEFAULT_OUTPUT_PROJECTOR.history_text(actions)
