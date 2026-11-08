"""Typed contracts for the desktop shared-runtime adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from nexau.core.messages import Message

from northcua.agentkernel.tracing.capture import TraceCaptureState
from northcua.agents.desktop.prompt import PromptEnvelope
from northcua.core.json_types import JsonObject
from northcua.core.models import DesktopStateSnapshot, LLMTraceRecord, ObservationContext, StepLogRecord
from northcua.transport.ws.remote_round import ExternalToolDispatcher


class DesktopObservationSource(str, Enum):
    """Source used to resolve a desktop observation for a runtime step."""

    PROVIDED_CONTEXT = "provided_context"
    PROVIDED_SNAPSHOT = "provided_snapshot"
    PLATFORM_RUNTIME = "platform_runtime"
    LOCAL_SERVICE = "local_service"
    REMOTE_PROVIDER = "remote_provider"


class DesktopModelStopReason(str, Enum):
    """Stable model stop reasons observed at the desktop adapter boundary."""

    EXTERNAL_TOOL_CALL = "EXTERNAL_TOOL_CALL"
    OTHER = "OTHER"


class DesktopRuntimeEventName(str, Enum):
    """Runtime log event names owned by the desktop shared runtime."""

    ROUND_STARTED = "round_started"
    RESUME_RESULTS_REPLAYED = "round_resume_results_replayed"
    SNAPSHOT_READY = "round_snapshot_ready"
    HISTORY_REPLAY_BUILT = "round_history_replay_built"
    TOOL_SURFACE_READY = "round_tool_surface_ready"
    PROMPT_ENVELOPE_BUILT = "round_prompt_envelope_built"
    TRACER_SNAPSHOT = "round_tracer_snapshot"
    TRACE_RECORD_BUILT = "round_trace_record_built"
    STOP_DECISION_MADE = "round_stop_decision_made"
    EXTERNAL_TOOL_PAUSE_RECEIVED = "round_external_tool_pause_received"
    LOCAL_EXTERNAL_TOOLS_COMPLETED = "round_local_external_tools_completed"
    REMOTE_EXTERNAL_TOOLS_COMPLETED = "round_remote_external_tools_completed"
    LLM_FALLBACK_ATTEMPT_STARTED = "llm_fallback_attempt_started"
    LLM_FALLBACK_ATTEMPT_FAILED = "llm_fallback_attempt_failed"
    LLM_FALLBACK_ATTEMPT_SELECTED = "llm_fallback_attempt_selected"


DesktopRuntimeProgressSink = Callable[[JsonObject], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DesktopStepObservation:
    """Observation payload resolved for one desktop runtime step.

    Attributes:
        context: Full observation context consumed by prompts and artifacts.
        snapshot: Desktop snapshot used for step log fields and screenshots.
        source: Source strategy that produced the observation.
    """

    context: ObservationContext
    snapshot: DesktopStateSnapshot
    source: DesktopObservationSource


@dataclass(frozen=True, slots=True)
class DesktopPromptBundle:
    """Prompt payload passed to the desktop model runner.

    Attributes:
        envelope: Prompt envelope with rendered text, images, tools, and replay.
        messages: Provider-facing NexAU messages derived from the envelope.
        trace_context_metadata: JSON metadata attached to model/tool traces.
    """

    envelope: PromptEnvelope
    messages: list[Message]
    trace_context_metadata: JsonObject
    shadow_messages: list[Message] | None = None


@dataclass(slots=True)
class DesktopModelStepResult:
    """Model and trace state selected after LLM fallback.

    Attributes:
        trace_state: Mutable trace capture state for the selected candidate.
        llm_trace: Persistable LLM trace record built from ``trace_state``.
        llm_candidate_name: Fallback candidate selected for this step.
    """

    trace_state: TraceCaptureState
    llm_trace: LLMTraceRecord
    llm_candidate_name: str

    def release_persisted_provider_payloads(self) -> None:
        """Release full provider payloads once their durable trace is written."""

        self.trace_state.release_provider_payloads()
        self.llm_trace.release_provider_payloads()

    def release_runtime_payloads(self) -> None:
        """Release bulky runtime payloads after step artifacts are built."""

        self.trace_state.release_runtime_payloads()
        self.llm_trace.release_runtime_payloads()


class DesktopObservationContextProvider:
    """Callable remote/local observation provider boundary."""

    def __init__(
        self,
        collector: Callable[[int, Sequence[StepLogRecord]], Awaitable[ObservationContext]],
    ) -> None:
        self._collector = collector

    async def collect(
        self,
        *,
        session_step: int,
        previous_steps: Sequence[StepLogRecord],
    ) -> ObservationContext:
        """Collect a fresh observation context for one step.

        Args:
            session_step: One-based main-agent session step to observe for.
            previous_steps: Prior desktop step logs used for contextual probes.

        Returns:
            Observation context from the configured collector.
        """

        return await self._collector(session_step, previous_steps)


@dataclass(frozen=True, slots=True)
class DesktopRuntimeOptions:
    """Runtime strategies that can vary between local and remote desktop runs.

    Attributes:
        snapshot: Pre-collected snapshot, when the caller already has one.
        observation_context: Pre-collected full observation context.
        observation_provider: Deferred observation provider, used by remote runs.
        remote_tool_dispatcher: Remote tool/command dispatcher callback.
        runtime_progress_sink: Optional best-effort progress sink used by
            process workers to refresh liveness telemetry without changing
            runtime behavior.
    """

    snapshot: DesktopStateSnapshot | None = None
    observation_context: ObservationContext | None = None
    observation_provider: DesktopObservationContextProvider | None = None
    remote_tool_dispatcher: ExternalToolDispatcher | None = None
    runtime_progress_sink: DesktopRuntimeProgressSink | None = None


__all__ = [
    "DesktopModelStopReason",
    "DesktopModelStepResult",
    "DesktopObservationContextProvider",
    "DesktopObservationSource",
    "DesktopPromptBundle",
    "DesktopRuntimeProgressSink",
    "DesktopRuntimeEventName",
    "DesktopRuntimeOptions",
    "DesktopStepObservation",
]
