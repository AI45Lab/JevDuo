"""Factory for desktop shared-runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from northcua.agentkit.runtime import (
    AgentRoundRuntime,
    AgentRuntimeCounterState,
    AgentSessionRuntime,
    AgentStepRuntime,
)
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore

from .contracts import DesktopRuntimeOptions
from .model import DesktopModelStepRunner
from .observation import DesktopObservationProvider
from .prompt import DesktopPromptProjector
from .state import DesktopRuntimeStateStore
from .step import DesktopStepExecutor
from .terminal import DesktopTerminalPersistenceFacade, DesktopTerminalProjectionPort

if TYPE_CHECKING:
    from northcua.agents.desktop.round_execution.service import RoundExecutionService


@dataclass(slots=True)
class DesktopRuntimeFactory:
    """Build shared runtime instances from desktop facade/service components.

    Attributes:
        artifact_store: Artifact writer used by desktop step persistence.
        round_coordinator: Desktop queue/state facade for the active session.
        round_execution_service: Service bundle that owns existing adapters.
        terminal_facade: Facade used to persist max-step terminal projections.
    """

    artifact_store: NorthCUAArtifactStore
    round_coordinator: NorthCUARoundCoordinator
    round_execution_service: RoundExecutionService
    terminal_facade: DesktopTerminalPersistenceFacade

    def build_round_runtime(
        self,
        *,
        options: DesktopRuntimeOptions,
    ) -> AgentRoundRuntime[object, object, StepLogRecord]:
        """Build a complete shared round runtime for one desktop query.

        Args:
            options: Local/remote runtime strategies for this invocation.

        Returns:
            Shared round runtime wired to desktop observation, model,
            persistence, evaluation, and terminal-projection adapters.
        """

        service = self.round_execution_service
        counters = AgentRuntimeCounterState(session_id=self.round_coordinator.state.session_id)
        state_store = DesktopRuntimeStateStore(
            counters=counters,
            round_coordinator=self.round_coordinator,
            state_store=service.state_store,
            artifact_store=self.artifact_store,
        )
        session_runtime = AgentSessionRuntime(counters=counters, state_store=state_store)
        observation_provider = DesktopObservationProvider(
            observation_service=service.observation_service,
            platform_runtime=service.platform_runtime,
        )
        prompt_projector = DesktopPromptProjector(
            app_config=service.app_config,
            prompt_builder=service.prompt_builder,
            history_replay_service=service.history_replay_service,
            memory_root=self.artifact_store.root,
        )
        model_runner = DesktopModelStepRunner(
            app_config=service.app_config,
            user_id=service.user_id,
            transport_capabilities=service.transport_capabilities,
            agent_preparer=service.agent_preparer,
            trace_capture_service=service.trace_capture_service,
            tool_surface_service=service.tool_surface_service,
            tool_result_service=service.tool_result_service,
            local_tool_dispatcher=service.local_tool_dispatcher,
            round_coordinator=self.round_coordinator,
            execution_transport=service.execution_transport,
            local_device_executor=service.local_device_executor,
        )
        step_executor = DesktopStepExecutor(
            app_config=service.app_config,
            artifact_store=self.artifact_store,
            round_coordinator=self.round_coordinator,
            state_hydrator=service.state_hydrator,
            observation_provider=observation_provider,
            prompt_projector=prompt_projector,
            model_runner=model_runner,
            tool_result_service=service.tool_result_service,
            stop_policy_service=service.stop_policy_service,
            step_persister=service.step_persister,
            evaluation_scheduler=service.evaluation_scheduler,
            verification_capture=service.verification_capture,
            options=options,
        )
        return AgentRoundRuntime(
            session_runtime=session_runtime,
            step_runtime=AgentStepRuntime(step_executor=step_executor),
            terminal_projection=DesktopTerminalProjectionPort(
                self.terminal_facade,
                remote_tool_dispatcher=options.remote_tool_dispatcher,
            ),
        )


__all__ = ["DesktopRuntimeFactory"]
