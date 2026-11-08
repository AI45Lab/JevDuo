"""Step record builders and telemetry helpers for desktop runtime steps."""

from __future__ import annotations

import logging

from northcua.agents.desktop.services.stop_policy import StopDecision
from northcua.app.logging import log_runtime_event
from northcua.core.config import NorthCUAAppConfig
from northcua.core.models import ScreenResolution, StepLogRecord, ToolResultArtifact
from northcua.runtime.artifacts.store import NorthCUAArtifactStore

from .contracts import DesktopRuntimeEventName, DesktopStepObservation
from .model import DesktopModelStepResult

logger = logging.getLogger(__name__)


def build_trace_context_metadata(
    *,
    app_config: NorthCUAAppConfig,
    artifact_store: NorthCUAArtifactStore,
    query: str,
    session_id: str,
    round_num: int,
    round_step: int,
    session_step: int,
) -> dict[str, object]:
    """Build JSON metadata passed through model and tool trace contexts.

    Args:
        app_config: Application configuration carrying optional experiment metadata.
        artifact_store: Artifact store used to derive stable per-round paths.
        query: User query for the current round.
        session_id: Runtime session identifier.
        round_num: One-based round number.
        round_step: One-based step number within the round.
        session_step: One-based step number within the session.

    Returns:
        JSON-compatible metadata shared by model tracing and tool execution.
    """

    metadata: dict[str, object] = {
        "agent_kind": "desktop",
        "artifact_dir": str(artifact_store.query_dir(round_num)),
        "northcua_session_id": session_id,
        "query_index": round_num,
        "round_num": round_num,
        "round_step": round_step,
        "session_id": session_id,
        "session_step": session_step,
        "user_query": query,
    }
    experiment_id = app_config.execution.experiment_id.strip()
    if experiment_id:
        metadata["experiment_id"] = experiment_id
    return metadata


def build_step_log(
    *,
    artifact_store: NorthCUAArtifactStore,
    query: str,
    session_id: str,
    round_num: int,
    round_step: int,
    session_step: int,
    observation: DesktopStepObservation,
    model_result: DesktopModelStepResult,
    decision: StopDecision,
    tool_results: list[ToolResultArtifact],
) -> StepLogRecord:
    """Build the desktop ``StepLogRecord`` for one completed runtime step.

    Args:
        artifact_store: Artifact store used to locate trace logs.
        query: User query for this round.
        session_id: Runtime session identifier.
        round_num: One-based round number.
        round_step: One-based step number within the round.
        session_step: One-based step number within the session.
        observation: Typed desktop observation captured before the model call.
        model_result: Typed model/fallback result for the step.
        decision: Stop-policy result derived after tool dispatch.
        tool_results: Persistable tool-result artifacts for this step.

    Returns:
        A complete step log record ready for artifact persistence.
    """

    return StepLogRecord(
        session_id=session_id,
        session_step=session_step,
        round_step=round_step,
        round_num=round_num,
        status=decision.status,
        request=query,
        result=decision.result,
        llm_cost=model_result.llm_trace.cost,
        platform_name=observation.snapshot.platform_name,
        screen_resolution=ScreenResolution.from_metadata(observation.snapshot.screenshot_metadata),
        current_window=observation.snapshot.current_window,
        clean_screenshot_path=observation.snapshot.clean_screenshot_path,
        llm_trace_path=str(artifact_store.llm_trace_log_path),
        llm_reasoning=model_result.llm_trace.reasoning,
        llm_thinking=model_result.llm_trace.thinking,
        llm_reasoning_encrypted_content=model_result.llm_trace.reasoning_encrypted_content,
        llm_trace=model_result.llm_trace,
        assistant_text=model_result.trace_state.assistant_text,
        assistant_tool_calls=model_result.trace_state.assistant_tool_calls,
        tool_results=tool_results,
        terminal_decision=decision.terminal_decision.to_json(),
        accessibility=observation.context.accessibility,
        observation_artifacts=list(observation.context.current_artifacts),
        observation_metadata=dict(observation.context.observation_metadata),
    )


def log_snapshot_ready(*, session_id: str, session_step: int, observation: DesktopStepObservation) -> None:
    """Log desktop observation metadata for runtime replay and diagnostics.

    Args:
        session_id: Runtime session identifier.
        session_step: One-based session step being recorded.
        observation: Typed desktop observation whose compact metadata is logged.
    """

    log_runtime_event(
        logger,
        DesktopRuntimeEventName.SNAPSHOT_READY.value,
        level=logging.DEBUG,
        session_id=session_id,
        session_step=session_step,
        platform_name=observation.snapshot.platform_name,
        current_window=observation.snapshot.current_window,
        control_count=len(observation.snapshot.current_controls),
        clean_screenshot_path=observation.snapshot.clean_screenshot_path,
        screen_resolution=ScreenResolution.from_metadata(observation.snapshot.screenshot_metadata),
        screenshot_metadata=observation.snapshot.screenshot_metadata,
        observation_source=observation.source.value,
        observation_artifact_count=len(observation.context.current_artifacts),
    )


def log_trace_record_built(*, session_id: str, session_step: int, model_result: DesktopModelStepResult) -> None:
    """Log compact LLM trace metadata after trace persistence.

    Args:
        session_id: Runtime session identifier.
        session_step: One-based session step being recorded.
        model_result: Typed model result containing the persisted trace summary.
    """

    log_runtime_event(
        logger,
        DesktopRuntimeEventName.TRACE_RECORD_BUILT.value,
        level=logging.DEBUG,
        session_id=session_id,
        session_step=session_step,
        assistant_tool_call_count=len(model_result.llm_trace.assistant_tool_calls),
        tool_execution_count=len(model_result.llm_trace.tool_executions),
        stop_reason=model_result.llm_trace.stop_reason,
        usage=model_result.llm_trace.usage,
        cost=model_result.llm_trace.cost,
    )


def log_stop_decision_made(
    *,
    session_id: str,
    session_step: int,
    model_result: DesktopModelStepResult,
    decision: StopDecision,
    tool_result_count: int,
) -> None:
    """Log the stop-policy decision selected for this desktop step.

    Args:
        session_id: Runtime session identifier.
        session_step: One-based session step being recorded.
        model_result: Typed model result whose tool-call count is summarized.
        decision: Stop-policy decision selected for this step.
        tool_result_count: Number of tool-result artifacts generated by dispatch.
    """

    log_runtime_event(
        logger,
        DesktopRuntimeEventName.STOP_DECISION_MADE.value,
        session_id=session_id,
        session_step=session_step,
        status=decision.status,
        terminal_action=decision.terminal_decision.action.value,
        terminal_reason=decision.terminal_decision.reason.value,
        result=decision.result,
        summary=decision.summary,
        assistant_tool_call_count=len(model_result.trace_state.assistant_tool_calls),
        tool_result_count=tool_result_count,
    )


__all__ = [
    "build_step_log",
    "build_trace_context_metadata",
    "log_snapshot_ready",
    "log_stop_decision_made",
    "log_trace_record_built",
]
