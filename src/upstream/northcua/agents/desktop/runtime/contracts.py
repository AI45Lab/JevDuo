"""Typed contracts for the desktop shared-runtime adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum


from northcua.core.json_types import JsonObject
from northcua.core.models import DesktopStateSnapshot, ObservationContext, StepLogRecord
from northcua.transport.ws.remote_round import ExternalToolDispatcher


class DesktopObservationSource(str, Enum):
    """Source used to resolve a desktop observation for a runtime step."""

    PROVIDED_CONTEXT = "provided_context"
    PROVIDED_SNAPSHOT = "provided_snapshot"
    PLATFORM_RUNTIME = "platform_runtime"
    LOCAL_SERVICE = "local_service"
    REMOTE_PROVIDER = "remote_provider"


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
    "DesktopObservationContextProvider",
    "DesktopObservationSource",
    "DesktopRuntimeProgressSink",
    "DesktopRuntimeOptions",
    "DesktopStepObservation",
]
