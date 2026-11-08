"""Top-level web-agent run loop orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING, cast

from nexau.core.messages import Message

from northcua.agentkernel.tools.surface import normalize_allowed_tool_names
from northcua.agentkit.runtime import (
    AgentRoundRuntime,
    AgentRoundRuntimeSpec,
    AgentRuntimeCounterState,
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    AgentSessionRuntime,
    AgentStepRuntime,
    SessionResourcePort,
)
from northcua.agentkit.tool_trace import ResolvedToolExecution, compact_tool_outputs, tool_output_text
from northcua.app.logging import log_runtime_event
from northcua.core.budget import AgentStepBudget
from northcua.core.json_types import JsonObject
from northcua.core.terminal import AgentTerminalReason, TerminalDecisionPolicy

from northcua.agentkernel.working_memory import WorkingMemoryStore

from ..backend import WebActionBackend
from ..domain.evaluation_artifacts import WebToolResultArtifact
from ..domain.observations import WebObservationContext, WebScreenshotObservation
from ..domain.results import WebAgentResult, WebRunStatus, WebStepRecord, WebTerminalStatus
from ..prompt.visual_window import WebPromptVisualHistoryFactory, WebPromptVisualHistoryTurn
from ..session import RegisteredWebAgentSession
from .action_resolution import ResolvedWebAgentAction
from .model_step import WebAgentModelStepResult
from .models import WebAgentRunnerCallbacks, WebAgentRunnerRunSpec

if TYPE_CHECKING:
    from .main import WebAgentRunner

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _WebBrowserSessionResource(SessionResourcePort):
    """Runtime-managed browser client resource for one web-agent session."""

    runner: "WebAgentRunner"
    session_id: str
    url: str
    headed: bool | None = None
    client: WebActionBackend | None = None

    async def start(self) -> None:
        """Create, register, and prepare the browser client once."""

        if self.client is not None:
            return
        web_config = self.runner._web_config_for_run(self.headed)
        client = self.runner._build_client(self.session_id, headed=self.headed)
        self.client = client
        try:
            self.runner.session_registry.register(
                RegisteredWebAgentSession(
                    session_id=self.session_id,
                    client=client,
                    artifact_store=self.runner.artifact_store,
                    settings=web_config,
                )
            )
        except BaseException:
            self.client = None
            await self.runner._close_client(client)
            raise
        await self.runner._prepare_browser_session(client=client, url=self.url)

    async def close(self) -> None:
        """Unregister and close the browser client idempotently."""

        if self.client is None:
            return
        client = self.client
        self.client = None
        self.runner.session_registry.unregister(self.session_id)
        await self.runner._close_client(client)

    def require_client(self) -> WebActionBackend:
        """Return the active browser client.

        Returns:
            Started browser client registered for the active web-agent session.

        Raises:
            RuntimeError: If the resource has not been started.
        """

        if self.client is None:
            raise RuntimeError("Web browser session resource is not started.")
        return self.client


@dataclass(slots=True)
class _WebAgentLoopExecution:
    """Mutable per-step execution state for one web-agent run."""

    runner: "WebAgentRunner"
    spec: WebAgentRunnerRunSpec
    callbacks: WebAgentRunnerCallbacks
    result: WebAgentResult
    resource: _WebBrowserSessionResource
    session_id: str
    prior_messages: list[Message]
    prior_visual_history_turns: list[WebPromptVisualHistoryTurn] = field(default_factory=list)
    visual_history_factory: WebPromptVisualHistoryFactory = field(default_factory=WebPromptVisualHistoryFactory)
    recent_action_result: JsonObject | None = None
    current_observation: WebObservationContext | None = None
    recent_post_action_observation: WebObservationContext | None = None

    async def execute_step(
        self,
        context: AgentRuntimeStepContext[object, object],
    ) -> AgentRuntimeStepOutcome[object]:
        """Execute one model/browser step.

        Args:
            context: Runtime cursor and step metadata.

        Returns:
            Outcome used by the shared AgentKit runtime to continue or stop.
        """

        step_index = context.cursor.round_step
        session_step = context.cursor.session_step
        artifact_step_index = session_step or step_index
        client = self.resource.require_client()
        observation = await self._current_observation()
        artifact_paths = await self.runner.artifact_persistence.persist_observation_artifacts(
            client=client,
            step_index=artifact_step_index,
            observation=observation,
        )
        observation_artifact = self.runner.artifact_persistence.build_observation_artifact(
            observation=observation,
            paths=artifact_paths,
        )
        self._append_screenshot_paths(
            artifact_paths.get("screenshot_path"),
            artifact_paths.get("annotated_screenshot_path"),
        )
        before_action_screenshot_paths = set(self.runner.artifact_persistence.iter_screenshot_artifact_paths())
        observation.recent_action_result = self.recent_action_result
        registered = self.runner.session_registry.get_registered_session(self.session_id)
        registered.clear_tool_image_artifact_events()
        step_result = await self.runner._run_model_step(
            user_query=self.spec.user_query,
            session_id=self.session_id,
            step_index=step_index,
            session_step=session_step,
            round_num=context.cursor.round_num,
            runtime_cursor=context.cursor.to_json(),
            step_budget=cast(JsonObject | None, context.metadata.get("step_budget")),
            tool_choice_policy=str(context.metadata.get("tool_choice_policy", "auto")),
            allowed_tool_names=normalize_allowed_tool_names(context.metadata.get("allowed_tool_names")),
            observation=observation,
            prior_messages=self.prior_messages,
            visual_history_turns=self.prior_visual_history_turns,
        )
        resolved_actions = await self.runner._resolve_step_actions(
            session_id=self.session_id,
            round_num=context.cursor.round_num,
            step_index=step_index,
            step_result=step_result,
        )
        WorkingMemoryStore(self.runner.artifact_store.root).ingest(
            step_result.trace_state.tool_executions,
            round_num=context.cursor.round_num,
            session_step=session_step,
        )
        post_action_observation = self._latest_post_action_observation(resolved_actions)
        recovery_result = await self.runner.observation_recovery_service.recover(
            session=registered,
            trace_state=step_result.trace_state,
            actions=resolved_actions,
        )
        if recovery_result.recovered or recovery_result.error:
            resolved_actions = self.runner.action_resolver.resolve_all(step_result.trace_state.tool_executions)
            post_action_observation = self._latest_post_action_observation(resolved_actions)
        if recovery_result.refs_invalidated:
            self.current_observation = self.runner.observation_recovery_service.without_actionable_refs(observation)
            post_action_observation = None
        action_screenshot_paths = self._new_action_screenshot_paths(before_action_screenshot_paths)
        if action_screenshot_paths:
            self._append_screenshot_paths(*action_screenshot_paths)
        invocation_events = list(registered.tool_invocation_artifact_events)
        artifact_events = list(registered.tool_image_artifact_events)
        tool_artifacts = self.runner.artifact_persistence.build_tool_result_artifacts(
            actions=resolved_actions,
            action_screenshot_paths=action_screenshot_paths,
            artifact_invocations=invocation_events,
            artifact_events=artifact_events,
            session_step=artifact_step_index,
        )
        step_record = self.runner._build_step_record(
            step_index=step_index,
            round_num=context.cursor.round_num,
            session_step=session_step,
            runtime_cursor=context.cursor.to_json(),
            observation=observation,
            step_result=step_result,
            resolved_actions=resolved_actions,
            artifact_paths=artifact_paths,
            observation_artifact=observation_artifact,
            tool_artifacts=tool_artifacts,
        )
        self.result.steps.append(step_record)
        self.runner.artifact_persistence.persist_step(step_record)
        self.runner.artifact_persistence.persist_llm_trace(
            step_result.trace_state,
            session_id=self.session_id,
            session_step=session_step,
            round_num=context.cursor.round_num,
            round_step=step_index,
            request=self.spec.user_query,
            runtime_cursor=context.cursor.to_json(),
        )
        if self.callbacks.on_step is not None:
            self.callbacks.on_step(self.runner._build_panel_record(step_record, observation))
        if not resolved_actions:
            final_answer = self._final_answer_from_step(step_result)
            if final_answer:
                return self._finish_final_answer(context, step_record, final_answer)
            return self._finish_missing_tool(context, step_record, step_result)
        self.recent_action_result = compact_tool_outputs(cast(list[ResolvedToolExecution], resolved_actions))
        self.recent_post_action_observation = post_action_observation
        if self.recent_post_action_observation is not None and tool_artifacts:
            self._attach_post_action_tool_artifact_paths(
                observation=self.recent_post_action_observation,
                tool_artifacts=tool_artifacts,
            )
        if self.recent_post_action_observation is not None:
            self.current_observation = self.recent_post_action_observation
        self.prior_messages = self.runner.prompt_builder.append_step_history(
            envelope=step_result.envelope,
            prior_messages=self.prior_messages,
            trace_state=step_result.trace_state,
        )
        visual_turn = self.visual_history_factory.build_turn(
            session_step=session_step,
            round_num=context.cursor.round_num,
            tool_artifacts=tool_artifacts,
        )
        if visual_turn is not None:
            self.prior_visual_history_turns.append(visual_turn)
        errored_action = next(
            (action for action in resolved_actions if action.is_error and not action.is_retryable_error),
            None,
        )
        if errored_action is not None:
            return self._finish_tool_error(context, step_record, step_result, errored_action)
        return AgentRuntimeStepOutcome(
            cursor=context.cursor,
            result=step_record,
            terminal_decision=self.result.to_terminal_decision(),
            summary="The web agent needs another step.",
        )

    async def _current_observation(self) -> WebObservationContext:
        if self.recent_post_action_observation is not None:
            observation = self.recent_post_action_observation
            self.recent_post_action_observation = None
            self.current_observation = observation
            return observation
        if self.current_observation is not None:
            return self.current_observation
        registered = self.runner.session_registry.get_registered_session(self.session_id)
        mode = self.runner.app_config.web.initial_observation_mode
        if mode == "none":
            mode = "snapshot"
        observation = await self.runner.action_service.collect_current_observation(
            registered,
            mode=mode,
            observation_id=f"initial-{self.session_id}-{time_ns()}",
            validate_url=False,
        )
        self.current_observation = observation
        return observation

    def _latest_post_action_observation(
        self,
        resolved_actions: list[ResolvedWebAgentAction],
    ) -> WebObservationContext | None:
        for action in reversed(resolved_actions):
            if not isinstance(action.tool_output, dict):
                continue
            observation_error = action.tool_output.get("observation_error")
            if isinstance(observation_error, dict) and observation_error.get("recovered") is not True:
                continue
            observation = self.runner.action_service.observation_projector.context_from_payload(
                action.tool_output.get("observation"),
                screenshot_url=str(action.tool_output.get("screenshot_url") or ""),
            )
            if observation is not None:
                return observation
        return None

    def _attach_post_action_tool_artifact_paths(
        self,
        *,
        observation: WebObservationContext,
        tool_artifacts: list[WebToolResultArtifact],
    ) -> None:
        raw_path: Path | None = None
        som_path: Path | None = None
        selected_path = ""
        for artifact in reversed(tool_artifacts):
            selected_path = artifact.selected_image_path or artifact.som_image_path or artifact.raw_image_path
            if not selected_path:
                continue
            raw_path = Path(artifact.raw_image_path) if artifact.raw_image_path else None
            som_path = Path(artifact.som_image_path) if artifact.som_image_path else None
            break
        if not selected_path:
            return
        if observation.screenshot is None:
            observation.screenshot = WebScreenshotObservation(
                path=selected_path,
                raw={"source": "web_tool_result_artifact"},
            )
            return
        self._attach_annotation_artifact_paths(
            observation=observation,
            raw_path=raw_path,
            som_path=som_path,
        )
        if observation.screenshot.path or observation.screenshot.data_url:
            return
        observation.screenshot.path = selected_path
        raw = dict(observation.screenshot.raw)
        raw.setdefault("source", "web_tool_result_artifact")
        observation.screenshot.raw = raw

    def _new_action_screenshot_paths(self, before_action_screenshot_paths: set[Path]) -> list[Path]:
        current_paths = self.runner.artifact_persistence.iter_screenshot_artifact_paths()
        return [path for path in current_paths if path not in before_action_screenshot_paths]

    @staticmethod
    def _attach_annotation_artifact_paths(
        *,
        observation: WebObservationContext,
        raw_path: Path | None,
        som_path: Path | None,
    ) -> None:
        screenshot = observation.screenshot
        if screenshot is None or screenshot.annotation is None:
            return
        annotation = screenshot.annotation
        if raw_path is not None and not annotation.source_screenshot_path:
            annotation.source_screenshot_path = str(raw_path)
        if som_path is not None and not annotation.overlay_screenshot_path:
            annotation.overlay_screenshot_path = str(som_path)

    def _append_screenshot_paths(self, *paths: object) -> None:
        for path in paths:
            if not path:
                continue
            path_text = str(path)
            if path_text and path_text not in self.result.screenshots:
                self.result.screenshots.append(path_text)

    def _finish_missing_tool(
        self,
        context: AgentRuntimeStepContext[object, object],
        step_record: WebStepRecord,
        step_result: WebAgentModelStepResult,
    ) -> AgentRuntimeStepOutcome[object]:
        """Record terminal state when the model produced no web tool call."""

        self.result.tool_errors.append("Model produced neither a web tool call nor a final answer.")
        self.result.status = WebRunStatus.ERROR
        self.result.terminal_status = WebTerminalStatus.NO_TOOL_CALL
        self.result.final_answer = self.runner._assistant_text_from_step(step_result) or step_result.response_text
        return AgentRuntimeStepOutcome(
            cursor=context.cursor,
            result=step_record,
            terminal_decision=self.result.to_terminal_decision(),
            summary=self.result.final_answer,
        )

    def _final_answer_from_step(self, step_result: WebAgentModelStepResult) -> str:
        """Return non-empty assistant final text for a no-tool terminal step."""

        return (self.runner._assistant_text_from_step(step_result) or step_result.response_text or "").strip()

    def _finish_final_answer(
        self,
        context: AgentRuntimeStepContext[object, object],
        step_record: WebStepRecord,
        final_answer: str,
    ) -> AgentRuntimeStepOutcome[object]:
        """Record successful desktop-style terminal state from plain final text."""

        is_infeasible = (
            TerminalDecisionPolicy().failure_reason_from_text(final_answer) == AgentTerminalReason.INFEASIBLE
        )
        self.result.success = not is_infeasible
        self.result.final_answer = final_answer
        self.result.status = WebRunStatus.FAILED if is_infeasible else WebRunStatus.SUCCESS
        self.result.terminal_status = WebTerminalStatus.COMPLETED
        return AgentRuntimeStepOutcome(
            cursor=context.cursor,
            result=step_record,
            terminal_decision=self.result.to_terminal_decision(),
            summary=self.result.final_answer,
        )

    def _finish_tool_error(
        self,
        context: AgentRuntimeStepContext[object, object],
        step_record: WebStepRecord,
        step_result: WebAgentModelStepResult,
        errored_action: ResolvedToolExecution,
    ) -> AgentRuntimeStepOutcome[object]:
        """Record terminal state when a selected tool failed."""

        self.result.tool_errors.append(tool_output_text(errored_action.tool_output))
        self.result.status = WebRunStatus.ERROR
        self.result.terminal_status = WebTerminalStatus.TOOL_ERROR
        self.result.final_answer = self.runner._assistant_text_from_step(step_result) or step_result.response_text
        return AgentRuntimeStepOutcome(
            cursor=context.cursor,
            result=step_record,
            terminal_decision=self.result.to_terminal_decision(),
            summary=self.result.final_answer,
        )


async def run_web_agent_loop(
    runner: "WebAgentRunner", spec: WebAgentRunnerRunSpec, callbacks: WebAgentRunnerCallbacks
) -> WebAgentResult:
    """Run one web-agent task using dependencies owned by ``runner``.

    Args:
        runner: ``WebAgentRunner`` instance that owns model, prompt, browser,
            and artifact collaborators.
        spec: User-facing run spec with query, initial URL, and step cap.
        callbacks: Presentation/server callbacks for step progress.

    Returns:
        Structured web-agent result with step records and artifact paths.
    """

    session_id = runner.artifact_store.session_id
    max_steps = max(1, int(spec.max_steps or runner.app_config.web.max_steps))
    budget = AgentStepBudget(max_round_steps=max_steps, max_session_steps=spec.max_session_steps)
    session_step_offset = runner.artifact_persistence.count_persisted_steps()
    round_num_offset = runner.artifact_persistence.max_persisted_round_num()
    result = WebAgentResult(
        status=WebRunStatus.RUNNING,
        terminal_status=WebTerminalStatus.RUNNING,
        session_id=session_id,
        user_query=spec.user_query,
        start_url=spec.url,
        round_num=round_num_offset + 1,
        trace_path=runner.artifact_store.trace_path,
        result_path=runner.artifact_store.result_path,
    )
    resource = _WebBrowserSessionResource(
        runner=runner,
        session_id=session_id,
        url=spec.url,
        headed=spec.headed,
    )
    execution = _WebAgentLoopExecution(
        runner=runner,
        spec=spec,
        callbacks=callbacks,
        result=result,
        resource=resource,
        session_id=session_id,
        prior_messages=[],
    )

    try:
        round_runtime = AgentRoundRuntime(
            session_runtime=AgentSessionRuntime(
                counters=AgentRuntimeCounterState(
                    session_id=session_id,
                    next_session_step=session_step_offset + 1,
                    current_round_num=round_num_offset,
                ),
                resource=resource,
            ),
            step_runtime=cast(
                AgentStepRuntime[object, object, object],
                AgentStepRuntime(step_executor=execution.execute_step),
            ),
        )
        round_result = await round_runtime.run(
            AgentRoundRuntimeSpec(
                query=spec.user_query,
                budget=budget,
                fallback_round_steps=max_steps,
                metadata={
                    "tool_choice_policy": spec.tool_choice_policy,
                    "allowed_tool_names": list(spec.allowed_tool_names),
                },
            ),
            on_before_step=callbacks.on_before_step,
        )
        if result.status == WebRunStatus.RUNNING:
            result.status = WebRunStatus.MAX_STEPS
            result.terminal_status = WebTerminalStatus.MAX_STEPS
            result.final_answer = round_result.terminal_decision.final_answer or (
                "Stopped after reaching the configured max step limit."
            )
            result.terminal_metadata = dict(round_result.terminal_decision.metadata)
        return result
    except Exception as exc:
        result.status = WebRunStatus.ERROR
        result.terminal_status = WebTerminalStatus.RUNTIME_ERROR
        result.final_answer = str(exc)
        result.tool_errors.append(str(exc))
        log_runtime_event(
            logger,
            "web_agent_runtime_error",
            level=logging.ERROR,
            session_id=session_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return result
    finally:
        await _finalize_web_agent_loop(
            runner=runner,
            result=result,
            resource=resource,
            session_id=session_id,
            callbacks=callbacks,
        )


async def _finalize_web_agent_loop(
    *,
    runner: WebAgentRunner,
    result: WebAgentResult,
    resource: _WebBrowserSessionResource,
    session_id: str,
    callbacks: WebAgentRunnerCallbacks | None = None,
) -> None:
    """Save actor evidence, evaluate the active session, and always release it."""
    try:
        if result.status == WebRunStatus.RUNNING:
            result.status = WebRunStatus.ERROR
            result.terminal_status = WebTerminalStatus.INTERRUPTED
        await _attach_terminal_verification(
            runner=runner,
            result=result,
            resource=resource,
            session_id=session_id,
        )
        runner.artifact_persistence.persist_result(result)
        if (
            callbacks is not None
            and callbacks.on_finalize is not None
            and result.steps
            and result.terminal_status != WebTerminalStatus.INTERRUPTED
        ):
            try:
                await callbacks.on_finalize(result)
            finally:
                runner.artifact_persistence.persist_result(result)
    finally:
        try:
            await resource.close()
        except Exception as close_exc:
            log_runtime_event(
                logger,
                "web_agent_session_cleanup_failed",
                level=logging.WARNING,
                session_id=session_id,
                error=str(close_exc),
                error_type=type(close_exc).__name__,
            )
            _apply_cleanup_failure(result=result, close_exc=close_exc)
            runner.artifact_persistence.persist_result(result)


async def _attach_terminal_verification(
    *,
    runner: WebAgentRunner,
    result: WebAgentResult,
    resource: _WebBrowserSessionResource,
    session_id: str,
) -> None:
    if not result.steps:
        return
    try:
        client = resource.require_client()
        registered = runner.session_registry.get_registered_session(session_id)
        observation = await runner.action_service.collect_current_observation(
            registered,
            mode="snapshot",
            observation_id=f"terminal-{session_id}-{time_ns()}",
            validate_url=False,
        )
        await runner.artifact_persistence.attach_terminal_verification(
            client=client,
            step_record=result.steps[-1],
            observation=observation,
            reason=result.terminal_status.value,
            runtime_mode="local",
        )
        runner.artifact_persistence.replace_step_trace(result.steps)
        if result.steps[-1].screenshot_path:
            path_text = result.steps[-1].screenshot_path
            if path_text not in result.screenshots:
                result.screenshots.append(path_text)
    except Exception as exc:
        if result.steps:
            runner.artifact_persistence.mark_terminal_verification_failed(
                step_record=result.steps[-1],
                reason=result.terminal_status.value,
                error=exc,
                runtime_mode="local",
            )
            runner.artifact_persistence.replace_step_trace(result.steps)
        log_runtime_event(
            logger,
            "web_agent_terminal_verification_attach_failed",
            level=logging.WARNING,
            session_id=session_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _apply_cleanup_failure(*, result: WebAgentResult, close_exc: Exception) -> None:
    cleanup_error = f"cleanup_failed: {close_exc}"
    result.cleanup_errors.append(cleanup_error)
    result.tool_errors.append(cleanup_error)
    if result.status != WebRunStatus.SUCCESS:
        return
    result.status = WebRunStatus.ERROR
    result.terminal_status = WebTerminalStatus.RUNTIME_ERROR
    result.success = False
    result.final_answer = (
        f"{result.final_answer}\n\nCleanup failed: {close_exc}".strip()
        if result.final_answer
        else f"Cleanup failed: {close_exc}"
    )
