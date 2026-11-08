"""Prompt projection helpers for the desktop shared runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence
from pathlib import Path
from northcua.agentkernel.working_memory import WorkingMemoryStore

from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.agentkernel.json_types import ProviderToolChoice, ProviderToolSpecs
from northcua.agents.desktop.prompt import PromptEnvelopeBuilder
from northcua.agents.desktop.prompt.envelope import HistoryTurn
from northcua.agents.desktop.services.history_replay import HistoryReplayService
from northcua.app.logging import log_runtime_event
from northcua.core.config import NorthCUAAppConfig
from northcua.core.json_types import JsonObject
from northcua.core.models import StepLogRecord

from .contracts import DesktopPromptBundle, DesktopRuntimeEventName, DesktopStepObservation

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DesktopPromptProjector:
    """Build desktop prompt history and model messages for one step.

    Attributes:
        app_config: Desktop app configuration that controls history windows.
        prompt_builder: Prompt envelope builder for desktop templates.
        history_replay_service: Service that turns prior steps into replay turns.
    """

    app_config: NorthCUAAppConfig
    prompt_builder: PromptEnvelopeBuilder
    history_replay_service: HistoryReplayService
    memory_root: Path | None = None

    def build_history_turns(
        self,
        *,
        session_id: str,
        session_step: int,
        previous_steps: list[StepLogRecord],
    ) -> Sequence[HistoryTurn]:
        """Build replay turns and log compact observability metadata.

        Args:
            session_id: Active desktop session id.
            session_step: One-based session step being prompted.
            previous_steps: Prior step logs to replay into the prompt.

        Returns:
            History turns selected by the configured replay policy.
        """

        configured_window = self.app_config.prompt.history_window_rounds
        context_mode = self.app_config.context_management.resolve("desktop").mode
        history_window = len(previous_steps) if context_mode == ContextManagementMode.ENFORCE else configured_window
        if context_mode == ContextManagementMode.SHADOW:
            try:
                history_turns = self.history_replay_service.build_turns(previous_steps, len(previous_steps))
            except Exception:
                logger.warning("Shadow desktop history replay failed open", exc_info=True)
                history_turns = self.history_replay_service.build_turns(previous_steps, configured_window)
        else:
            history_turns = self.history_replay_service.build_turns(previous_steps, history_window)
        log_runtime_event(
            logger,
            DesktopRuntimeEventName.HISTORY_REPLAY_BUILT.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            history_turn_count=len(history_turns),
            history_tool_result_count=sum(len(turn.tool_results) for turn in history_turns),
        )
        return history_turns

    def project(
        self,
        *,
        task: str,
        session_id: str,
        session_step: int,
        step_budget: JsonObject | None,
        observation: DesktopStepObservation,
        history_turns: Sequence[HistoryTurn],
        tools: ProviderToolSpecs,
        tool_choice: ProviderToolChoice,
        trace_context_metadata: JsonObject,
        llm_candidate_name: str,
    ) -> DesktopPromptBundle:
        """Project one prompt bundle for a prepared tool surface.

        Args:
            task: User task text.
            session_id: Active desktop session id.
            session_step: One-based session step being prompted.
            step_budget: Prompt-safe budget metadata for the current step.
            observation: Typed observation for this step.
            history_turns: Replay turns from prior steps.
            tools: Provider-facing tool specs exposed to the model.
            tool_choice: Provider-facing tool choice policy.
            trace_context_metadata: JSON metadata attached to traces.
            llm_candidate_name: Active fallback candidate name.

        Returns:
            Prompt envelope, provider messages, and trace metadata.
        """

        observation_globals = self.prompt_builder.observation_builder.environment.globals
        previous_observation_metadata = observation_globals.get("observation_metadata")
        template_observation_metadata = dict(observation.context.observation_metadata)
        observation_globals["observation_metadata"] = template_observation_metadata
        context_mode = self.app_config.context_management.resolve("desktop").mode
        provider_history_turns = (
            history_turns[-self.app_config.prompt.history_window_rounds :]
            if context_mode == ContextManagementMode.SHADOW and self.app_config.prompt.history_window_rounds > 0
            else history_turns
        )
        if context_mode == ContextManagementMode.SHADOW and self.app_config.prompt.history_window_rounds == 0:
            provider_history_turns = ()
        shadow_envelope = None
        try:
            envelope = self.prompt_builder.build(
                task=task,
                platform_name=observation.snapshot.platform_name,
                current_window=observation.snapshot.current_window,
                history_turns=provider_history_turns,
                observation_context=observation.context,
                step_budget=step_budget,
                tools=tools,
                tool_choice=tool_choice,
            )
            if context_mode == ContextManagementMode.SHADOW:
                try:
                    shadow_envelope = self.prompt_builder.build(
                        task=task,
                        platform_name=observation.snapshot.platform_name,
                        current_window=observation.snapshot.current_window,
                        history_turns=history_turns,
                        observation_context=observation.context,
                        step_budget=step_budget,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                except Exception:
                    logger.warning("Shadow desktop prompt projection failed open", exc_info=True)
        finally:
            if previous_observation_metadata is None:
                observation_globals.pop("observation_metadata", None)
            else:
                observation_globals["observation_metadata"] = previous_observation_metadata
        messages = envelope.to_messages()
        shadow_messages = shadow_envelope.to_messages() if shadow_envelope is not None else None
        if self.memory_root is not None:
            memory = WorkingMemoryStore(self.memory_root)
            round_num = int(trace_context_metadata.get("round_num", 1))
            messages = memory.inject(messages, round_num=round_num)
            if shadow_messages is not None:
                shadow_messages = memory.inject(shadow_messages, round_num=round_num)
        log_runtime_event(
            logger,
            DesktopRuntimeEventName.PROMPT_ENVELOPE_BUILT.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            observation_image_count=len(envelope.observation_images),
            history_message_count=len(envelope.history_messages),
            tool_count=len(envelope.tools),
            total_message_count=len(messages),
            observation_text_length=len(envelope.observation_user_text),
            persistent_text_length=len(envelope.persistent_user_text),
            llm_candidate=llm_candidate_name,
        )
        return DesktopPromptBundle(
            envelope=envelope,
            messages=messages,
            trace_context_metadata=trace_context_metadata,
            shadow_messages=shadow_messages,
        )


__all__ = ["DesktopPromptProjector"]
