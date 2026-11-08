"""Desktop observation adapter for shared runtime steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from northcua.core.models import DesktopStateSnapshot, ObservationContext, StepLogRecord
from northcua.platforms.protocol import PlatformRuntime
from northcua.runtime.desktop import DesktopObservationService

from .contracts import DesktopObservationSource, DesktopRuntimeOptions, DesktopStepObservation


@dataclass(slots=True)
class DesktopObservationProvider:
    """Resolve a desktop observation from local, remote, or platform sources.

    Attributes:
        observation_service: Local service that can collect or adapt snapshots.
        platform_runtime: Optional typed platform runtime for local capture.
    """

    observation_service: DesktopObservationService
    platform_runtime: PlatformRuntime | None = None

    async def collect(
        self,
        *,
        session_step: int,
        previous_steps: list[StepLogRecord],
        options: DesktopRuntimeOptions,
    ) -> DesktopStepObservation:
        """Collect or adapt the observation for one shared runtime step.

        Args:
            session_step: One-based main-agent session step.
            previous_steps: Prior step logs used by observation probes.
            options: Local/remote observation strategies for this step.

        Returns:
            Typed desktop observation plus the source that produced it.

        Raises:
            RuntimeError: If no observation context can be resolved or a platform
                runtime returns an unsupported payload.
        """

        source = DesktopObservationSource.LOCAL_SERVICE
        observation_context: ObservationContext | None = None
        snapshot: DesktopStateSnapshot | None = None

        if options.observation_provider is not None:
            observation_context = await options.observation_provider.collect(
                session_step=session_step,
                previous_steps=previous_steps,
            )
            source = DesktopObservationSource.REMOTE_PROVIDER
        elif options.observation_context is not None:
            observation_context = options.observation_context
            source = DesktopObservationSource.PROVIDED_CONTEXT
        elif options.snapshot is not None:
            snapshot = options.snapshot
            observation_context = self.observation_service.context_from_snapshot(snapshot)
            source = DesktopObservationSource.PROVIDED_SNAPSHOT
        elif self.platform_runtime is not None:
            platform_observation = await self.platform_runtime.capture_observation(
                session_step=session_step,
                previous_steps=cast(list[object], list(previous_steps)),
            )
            if isinstance(platform_observation.raw, ObservationContext):
                observation_context = platform_observation.raw
            elif isinstance(platform_observation.raw, DesktopStateSnapshot):
                snapshot = platform_observation.raw
                observation_context = self.observation_service.context_from_snapshot(snapshot)
            else:
                raise RuntimeError(
                    "Desktop platform runtime returned an unsupported observation payload: "
                    f"{type(platform_observation.raw).__name__}"
                )
            source = DesktopObservationSource.PLATFORM_RUNTIME
        else:
            observation_context = await self.observation_service.collect_context(session_step, previous_steps)

        if observation_context is None:
            raise RuntimeError("Desktop observation context could not be resolved.")
        if snapshot is None:
            snapshot = self.observation_service.snapshot_from_context(observation_context)
        return DesktopStepObservation(context=observation_context, snapshot=snapshot, source=source)


__all__ = ["DesktopObservationProvider"]
