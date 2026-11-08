"""Step record projection helpers for the web-agent runner."""

from __future__ import annotations

from pathlib import Path

from northcua.agentkit.tool_trace import tool_history_name, tool_output_text
from northcua.core.json_types import JsonObject

from ..domain.evaluation_artifacts import WebObservationArtifact, WebToolResultArtifact
from ..domain.observations import WebObservationContext
from ..domain.panel_models import WebStepPanelRecord
from ..domain.results import WebStepRecord, WebStepStatus
from .action_resolution import ResolvedWebAgentAction, WebAgentActionResolver
from .model_step import WebAgentModelStepResult


class WebAgentStepRecordProjector:
    """Build persisted and terminal-facing step records."""

    def __init__(self, *, action_resolver: WebAgentActionResolver) -> None:
        self.action_resolver = action_resolver

    def build_step_record(
        self,
        *,
        session_id: str,
        step_index: int,
        round_num: int,
        session_step: int,
        runtime_cursor: JsonObject,
        observation: WebObservationContext,
        step_result: WebAgentModelStepResult,
        resolved_actions: list[ResolvedWebAgentAction],
        artifact_paths: dict[str, Path],
        observation_artifact: WebObservationArtifact | None = None,
        tool_artifacts: list[WebToolResultArtifact] | None = None,
    ) -> WebStepRecord:
        """Build the persisted step trace record for one web-agent step.

        Args:
            session_id: Controller/session identity for the persisted step.
            step_index: One-based step index inside the current round.
            round_num: One-based query/round number.
            session_step: One-based step index across the whole session.
            runtime_cursor: Shared runtime cursor payload for trace/audit.
            observation: Snapshot/screenshot context for this step.
            step_result: Captured model response and trace state.
            resolved_actions: Web tool actions executed for the step.
            artifact_paths: DOM/screenshot paths persisted before action
                resolution, plus optional action screenshot paths discovered
                after tool execution.

        Returns:
            Structured step record ready for JSONL persistence.
        """

        screenshot_path = str(artifact_paths.get("screenshot_path") or "")
        dom_snapshot_path = str(artifact_paths.get("dom_snapshot_path") or "")
        dom_snapshot_json_path = str(artifact_paths.get("dom_snapshot_json_path") or "")
        assistant_text = self.assistant_text_from_step(step_result)
        if not resolved_actions:
            is_final_answer_step = bool(assistant_text)
            return WebStepRecord(
                session_id=session_id,
                step_index=step_index,
                round_num=round_num,
                session_step=session_step,
                runtime_cursor=dict(runtime_cursor),
                status=WebStepStatus.OK if is_final_answer_step else WebStepStatus.ERROR,
                assistant_text=assistant_text,
                reasoning=step_result.trace_state.reasoning,
                assistant_tool_calls=list(step_result.trace_state.assistant_tool_calls),
                url=observation.page_url,
                observation=observation.snapshot,
                error_text="" if is_final_answer_step else "no web-agent tool call",
                screenshot_path=screenshot_path,
                dom_snapshot_path=dom_snapshot_path,
                dom_snapshot_json_path=dom_snapshot_json_path,
                observation_artifact=observation_artifact,
                observation_metadata=self._observation_metadata(observation_artifact),
            )
        status = WebStepStatus.ERROR if any(action.is_error for action in resolved_actions) else WebStepStatus.OK
        errored_action = next((action for action in resolved_actions if action.is_error), None)
        tool_summary = tool_history_name(resolved_actions)
        return WebStepRecord(
            session_id=session_id,
            step_index=step_index,
            round_num=round_num,
            session_step=session_step,
            runtime_cursor=dict(runtime_cursor),
            status=status,
            assistant_text=assistant_text,
            reasoning=step_result.trace_state.reasoning,
            assistant_tool_calls=list(step_result.trace_state.assistant_tool_calls),
            tool_summary=tool_summary,
            tools=[self.action_resolver.to_record(action) for action in resolved_actions],
            url=observation.page_url,
            observation=observation.snapshot,
            error_text=tool_output_text(errored_action.tool_output) if errored_action is not None else "",
            screenshot_path=screenshot_path,
            dom_snapshot_path=dom_snapshot_path,
            dom_snapshot_json_path=dom_snapshot_json_path,
            observation_artifact=observation_artifact,
            tool_artifacts=list(tool_artifacts or []),
            observation_metadata=self._observation_metadata(observation_artifact),
        )

    def build_panel_record(
        self,
        step_record: WebStepRecord,
        observation: WebObservationContext,
    ) -> WebStepPanelRecord:
        """Build the terminal-facing panel record for one persisted step.

        Args:
            step_record: Persisted step record.
            observation: Observation context used to summarize page state.

        Returns:
            Compact panel model consumed by the terminal renderer.
        """

        return WebStepPanelRecord(
            step=step_record.step_index,
            status=step_record.status,
            summary=step_record.tool_summary or step_record.error_text,
            url=step_record.url,
            tool_summary=step_record.tool_summary,
            tools=list(step_record.tools),
            assistant_text=step_record.assistant_text,
            reasoning=step_record.reasoning,
            assistant_tool_calls=list(step_record.assistant_tool_calls),
            observation_summary=observation.snapshot.summary,
            success=step_record.status == WebStepStatus.OK,
        )

    def assistant_text_from_step(self, step_result: WebAgentModelStepResult) -> str:
        """Return model text after filtering transport fallback errors.

        Args:
            step_result: Captured model response and trace state.

        Returns:
            Assistant text suitable for trace history and terminal panels.
        """

        captured_text = step_result.trace_state.assistant_text.strip()
        if captured_text and not self._is_transport_error_text(captured_text):
            return captured_text
        fallback_text = step_result.response_text.strip()
        if self._is_transport_error_text(fallback_text):
            return ""
        return fallback_text

    @staticmethod
    def _is_transport_error_text(text: str) -> bool:
        return text.strip().startswith("[Error:")

    def _observation_metadata(self, observation_artifact: WebObservationArtifact | None) -> JsonObject:
        if observation_artifact is None:
            return {}
        return {
            "web_current_observation_role": observation_artifact.role.value,
            "web_current_screenshot_path": observation_artifact.screenshot_path,
            "web_current_dom_snapshot_path": observation_artifact.dom_snapshot_path,
            "web_current_dom_snapshot_json_path": observation_artifact.dom_snapshot_json_path,
            "web_current_ref_count": observation_artifact.ref_count,
            "web_current_settled": observation_artifact.settled,
            "web_current_settle_reason": observation_artifact.settle_reason,
        }

    def _screenshot_path_from_actions(self, actions: list[ResolvedWebAgentAction]) -> str:
        for action in reversed(actions):
            path = self._screenshot_path_from_output(action.tool_output)
            if path:
                return path
        return ""

    @staticmethod
    def _screenshot_path_from_output(output: JsonObject | str | None) -> str:
        if not isinstance(output, dict):
            return ""
        result = output.get("result")
        if isinstance(result, dict):
            return str(result.get("path") or "")
        return ""
