"""Desktop step transaction executor for the shared runtime."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from northcua.agentkernel.tools.surface import normalize_allowed_tool_names
from northcua.agentkit.runtime import AgentRuntimeStepContext, AgentRuntimeStepOutcome
from northcua.agents.desktop.evaluation.terminal_status import (
    EVALUATION_PROJECTION_STANDARD,
    EVALUATION_STATUS_OK,
    EVALUATION_STATUS_PENDING,
    attach_evaluation_metadata_to_step,
    evaluation_metadata,
)
from northcua.agents.desktop.round_execution.evaluation_dispatch import RoundEvaluationScheduler
from northcua.agents.desktop.round_execution.state_hydration import RoundStateHydrator
from northcua.agents.desktop.round_execution.step_persistence import RoundStepPersister
from northcua.agents.desktop.round_execution.verification import RoundEndVerificationCapture
from northcua.agents.desktop.services.memory_compaction import (
    compact_replay_payloads_enabled,
    retained_step_payload_window,
)
from northcua.agents.desktop.services.stop_policy import StopPolicyService
from northcua.agents.desktop.services.tool_results import ToolResultService
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.app.logging import log_runtime_event, runtime_logging_context
from northcua.agentkernel.working_memory import WorkingMemoryStore
from northcua.core.config import NorthCUAAppConfig
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.memory import release_unused_process_memory, worker_memory_trim_enabled

from .contracts import DesktopRuntimeEventName, DesktopRuntimeOptions
from .model import DesktopModelStepRunner
from .observation import DesktopObservationProvider
from .prompt import DesktopPromptProjector
from .step_records import (
    build_step_log,
    build_trace_context_metadata,
    log_snapshot_ready,
    log_stop_decision_made,
    log_trace_record_built,
)

logger = logging.getLogger(__name__)
_RUNTIME_PROGRESS_TIMEOUT_SECONDS = 0.25


@dataclass(slots=True)
class DesktopStepExecutor:
    """Compose the desktop observation, prompt, model, persistence, and eval ports.

    Attributes:
        app_config: Desktop app configuration.
        artifact_store: Artifact writer for runtime, trace, and step logs.
        round_coordinator: Desktop queue/state facade for the active session.
        state_hydrator: Session and RoundState hydration service.
        observation_provider: Adapter that resolves local/remote observations.
        prompt_projector: Adapter that builds history and model prompts.
        model_runner: Adapter that executes LLM fallback and tool pauses.
        tool_result_service: Service that builds tool result artifacts.
        stop_policy_service: Service that maps trace state to terminal decisions.
        step_persister: Service that persists traces, steps, and legacy eval rows.
        evaluation_scheduler: Desktop round evaluator scheduler.
        verification_capture: Best-effort terminal screenshot capture service.
        options: Per-run local/remote runtime strategies.
    """

    app_config: NorthCUAAppConfig
    artifact_store: NorthCUAArtifactStore
    round_coordinator: NorthCUARoundCoordinator
    state_hydrator: RoundStateHydrator
    observation_provider: DesktopObservationProvider
    prompt_projector: DesktopPromptProjector
    model_runner: DesktopModelStepRunner
    tool_result_service: ToolResultService
    stop_policy_service: StopPolicyService
    step_persister: RoundStepPersister
    evaluation_scheduler: RoundEvaluationScheduler
    verification_capture: RoundEndVerificationCapture
    options: DesktopRuntimeOptions

    async def __call__(
        self,
        context: AgentRuntimeStepContext[object, object],
    ) -> AgentRuntimeStepOutcome[StepLogRecord]:
        """Execute and persist one desktop shared-runtime step."""

        with runtime_logging_context(
            log_path=self.artifact_store.runtime_log_path,
            session_id=self.round_coordinator.state.session_id,
        ):
            round_started_at = time.perf_counter()
            await self.state_hydrator.ensure_session_models()
            cursor = context.cursor
            session_id = self.round_coordinator.state.session_id
            session_step = cursor.session_step
            round_step = cursor.round_step
            round_num = max(cursor.round_num, 1)
            self.artifact_store.set_query_index(round_num)
            log_runtime_event(
                logger,
                DesktopRuntimeEventName.ROUND_STARTED.value,
                session_id=session_id,
                session_step=session_step,
                runtime_cursor=cursor.to_json(),
                round_num=round_num,
                round_step=round_step,
                request=context.query,
                existing_step_logs=len(self.round_coordinator.state.step_logs),
                pending_round_inputs=len(self.round_coordinator.state.pending_round_inputs),
                pending_resume_results=len(self.round_coordinator.state.pending_resume_results),
                runtime_mode=self.app_config.websocket.runtime_mode,
                max_agent_iterations_per_round=self.app_config.execution.max_agent_iterations_per_round,
            )
            await _emit_runtime_progress(
                self.options,
                phase="step_started",
                session_id=session_id,
                session_step=session_step,
                round_num=round_num,
                round_step=round_step,
            )
            resume_results = list(self.round_coordinator.state.pending_resume_results)
            if resume_results:
                log_runtime_event(
                    logger,
                    DesktopRuntimeEventName.RESUME_RESULTS_REPLAYED.value,
                    session_id=session_id,
                    session_step=session_step,
                    result_count=len(resume_results),
                    call_ids=[result.call_id for result in resume_results],
                )
                self.round_coordinator.state.pending_resume_results.clear()
                resume_results.clear()

            observation = await self.observation_provider.collect(
                session_step=session_step,
                previous_steps=self.round_coordinator.state.step_logs,
                options=self.options,
            )
            log_snapshot_ready(session_id=session_id, session_step=session_step, observation=observation)
            history_turns = list(
                self.prompt_projector.build_history_turns(
                    session_id=session_id,
                    session_step=session_step,
                    previous_steps=self.round_coordinator.state.step_logs,
                )
            )
            trace_context_metadata = build_trace_context_metadata(
                app_config=self.app_config,
                artifact_store=self.artifact_store,
                query=context.query,
                session_id=session_id,
                round_num=round_num,
                round_step=round_step,
                session_step=session_step,
            )
            await _emit_runtime_progress(
                self.options,
                phase="llm_started",
                session_id=session_id,
                session_step=session_step,
                round_num=round_num,
                round_step=round_step,
            )
            try:
                model_result = await self.model_runner.run(
                    task=context.query,
                    session_id=session_id,
                    round_num=round_num,
                    round_step=round_step,
                    session_step=session_step,
                    step_budget=context.metadata.get("step_budget"),
                    tool_choice_policy=str(context.metadata.get("tool_choice_policy", "auto")),
                    allowed_tool_names=normalize_allowed_tool_names(context.metadata.get("allowed_tool_names")),
                    observation=observation,
                    history_turns=history_turns,
                    prompt_projector=self.prompt_projector,
                    trace_context_metadata=trace_context_metadata,
                    remote_tool_dispatcher=self.options.remote_tool_dispatcher,
                )
            except Exception:
                await _emit_runtime_progress(
                    self.options,
                    phase="llm_failed",
                    session_id=session_id,
                    session_step=session_step,
                    round_num=round_num,
                    round_step=round_step,
                )
                raise
            await _emit_runtime_progress(
                self.options,
                phase="llm_finished",
                session_id=session_id,
                session_step=session_step,
                round_num=round_num,
                round_step=round_step,
            )
            history_turns.clear()
            self.step_persister.persist_llm_trace(
                session_id=session_id,
                session_step=session_step,
                round_num=round_num,
                round_step=round_step,
                request=context.query,
                llm_trace=model_result.llm_trace,
            )
            model_result.release_persisted_provider_payloads()
            log_trace_record_built(session_id=session_id, session_step=session_step, model_result=model_result)
            WorkingMemoryStore(self.artifact_store.root).ingest(
                model_result.trace_state.tool_executions,
                round_num=round_num,
                session_step=session_step,
            )
            tool_results = self.tool_result_service.build(
                executions=list(model_result.trace_state.tool_executions),
                session_step=session_step,
                pre_action_screenshot_path=observation.snapshot.clean_screenshot_path,
                is_initial_turn=not self.round_coordinator.state.step_logs,
            )
            decision = self.stop_policy_service.decide(
                assistant_text=model_result.trace_state.assistant_text,
                assistant_tool_calls=model_result.trace_state.assistant_tool_calls,
                tool_results=tool_results,
                step_logs=self.round_coordinator.state.step_logs,
            )
            log_stop_decision_made(
                session_id=session_id,
                session_step=session_step,
                model_result=model_result,
                decision=decision,
                tool_result_count=len(tool_results),
            )
            step_log = build_step_log(
                artifact_store=self.artifact_store,
                query=context.query,
                session_id=session_id,
                round_num=round_num,
                round_step=round_step,
                session_step=session_step,
                observation=observation,
                model_result=model_result,
                decision=decision,
                tool_results=tool_results,
            )
            await self.verification_capture.capture_for_terminal_step(
                step_log=step_log,
                runtime_mode=self.app_config.websocket.runtime_mode,
                remote_tool_dispatcher=self.options.remote_tool_dispatcher,
                previous_steps=self.round_coordinator.state.step_logs,
            )
            await self.step_persister.persist_step(
                step_log=step_log,
                snapshot=observation.snapshot,
                duration_ms=(time.perf_counter() - round_started_at) * 1000,
                terminal_summary=decision.summary,
                advance_counters=False,
                save_state=False,
            )
            for result in tool_results:
                result.release_image_payloads()
            observation.snapshot.release_image_payloads()
            observation.context.current_clean_screenshot_url = ""
            observation.context.selected_history_image_url = ""
            for artifact in observation.context.current_artifacts:
                artifact.release_image_payloads()
            model_result.release_runtime_payloads()
            retained_history_steps = retained_step_payload_window()
            self.round_coordinator.state.release_retained_payloads(
                retain_recent_steps=retained_history_steps,
                compact_replay_payloads=compact_replay_payloads_enabled(),
            )
            if worker_memory_trim_enabled():
                await asyncio.to_thread(release_unused_process_memory)
            try:
                evaluation_completed = await self.evaluation_scheduler.maybe_run_round_evaluation(
                    step_log,
                    list(self.round_coordinator.state.step_logs),
                    remote_tool_dispatcher=self.options.remote_tool_dispatcher,
                )
                if evaluation_completed:
                    _attach_terminal_evaluation_status(step_log, status=EVALUATION_STATUS_OK)
                if evaluation_completed and _has_terminal_evaluation_metadata(step_log):
                    await self.step_persister.rewrite_step_logs_after_terminal_metadata(
                        session_id=session_id,
                        session_step=session_step,
                    )
            except Exception:
                logger.exception(
                    "desktop round evaluation failed after step persistence; saving advanced state before re-raise",
                    extra={
                        "session_id": session_id,
                        "session_step": session_step,
                        "round_num": round_num,
                        "round_step": round_step,
                    },
                )
                _attach_terminal_evaluation_status(step_log, status=EVALUATION_STATUS_PENDING)
                if _has_terminal_evaluation_metadata(step_log):
                    await self.step_persister.rewrite_step_logs_after_terminal_metadata(
                        session_id=session_id,
                        session_step=session_step,
                    )
                await self.step_persister.save_shared_runtime_failure_state(cursor)
                raise
            return AgentRuntimeStepOutcome(
                cursor=cursor,
                result=step_log,
                terminal_decision=decision.terminal_decision,
                summary=decision.summary,
            )


def _attach_terminal_evaluation_status(step_log: StepLogRecord, *, status: str) -> None:
    metadata = step_log.terminal_decision.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("evaluation"), dict):
        return
    attach_evaluation_metadata_to_step(
        step_log,
        evaluation_metadata(
            status=status,
            attempts=1,
            projection_level=EVALUATION_PROJECTION_STANDARD,
        ),
    )


def _has_terminal_evaluation_metadata(step_log: StepLogRecord) -> bool:
    metadata = step_log.terminal_decision.get("metadata")
    return isinstance(metadata, dict) and isinstance(metadata.get("evaluation"), dict)


async def _emit_runtime_progress(
    options: DesktopRuntimeOptions,
    *,
    phase: str,
    session_id: str,
    session_step: int,
    round_num: int,
    round_step: int,
) -> None:
    if options.runtime_progress_sink is None:
        return
    payload = {
        "event_type": "desktop_worker_runtime_phase",
        "phase": phase,
        "session_id": session_id,
        "step_index": session_step,
        "session_step": session_step,
        "round_num": round_num,
        "round_step": round_step,
    }
    try:
        await asyncio.wait_for(
            options.runtime_progress_sink(payload),
            timeout=_RUNTIME_PROGRESS_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.debug(
            "desktop_runtime_progress_send_timed_out",
            extra={
                "session_id": session_id,
                "phase": phase,
                "session_step": session_step,
                "timeout_seconds": _RUNTIME_PROGRESS_TIMEOUT_SECONDS,
            },
        )
    except Exception:
        logger.debug(
            "desktop_runtime_progress_send_failed",
            exc_info=True,
            extra={"session_id": session_id, "phase": phase, "session_step": session_step},
        )


__all__ = ["DesktopStepExecutor"]
