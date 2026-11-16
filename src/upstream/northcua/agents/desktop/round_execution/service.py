"""Round-execution service composition for desktop tasks."""

from __future__ import annotations


from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentinfra.tracing.log_writer import LLMTraceLogWriter
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkernel.tracing.capture import TraceCaptureService
from northcua.agents.desktop.evaluation.service import RoundEvaluatorService
from northcua.agents.desktop.observation import ObservationCoordinator
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import ResultStatus, RuntimeMode
from northcua.core.models import (
    CommandExecutionResultRecord,
    DesktopStateSnapshot,
    ObservationContext,
    RoundInput,
    StepLogRecord,
)
from northcua.core.protocol import CommandRequestPayload, CommandResultPayload
from northcua.platforms.desktop import DesktopPlatformRuntime
from northcua.platforms.dispatch import PlatformCommandExecutor
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.desktop import DesktopObservationService
from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor
from northcua.transport.ws.remote_round import ExternalToolDispatcher
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from ..services.session_state import RoundStateStore
from .evaluation_dispatch import RoundEvaluationScheduler
from .step_persistence import RoundStepPersister
from .verification import RoundEndVerificationCapture


class _RemoteModeLocalCommandExecutor:
    """Fail clearly if remote controller code tries to execute local desktop tools."""

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Return tool-level failures instead of touching the controller desktop.

        Args:
            payload: Local command payload that should have been routed to a
                remote device-side dispatcher.

        Returns:
            Failure payload preserving every command call id for traceability.
        """

        message = (
            "Remote desktop runtime attempted local command execution on the controller; "
            "the remote tool dispatcher was not wired for this step."
        )
        return CommandResultPayload(
            session_id=payload.session_id,
            status=ResultStatus.FAILURE,
            results=[
                CommandExecutionResultRecord(
                    call_id=command.call_id,
                    status=ResultStatus.FAILURE,
                    error=message,
                )
                for command in payload.commands
            ],
        )


class RoundExecutionService:
    """Run one round end-to-end and persist the focused step log."""

    def __init__(
        self,
        *,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        artifact_store: NorthCUAArtifactStore,
        round_coordinator: NorthCUARoundCoordinator,
        session_manager: SessionManager,
        execution_transport: NorthCUAWebSocketTransport,
        user_id: str,
        transport_capabilities: AgentTransportCapabilities | None = None,
        platform_runtime: DesktopPlatformRuntime | None = None,
    ) -> None:
        self.app_config = app_config
        self.agent_config = agent_config
        self.artifact_store = artifact_store
        self.round_coordinator = round_coordinator
        self.session_manager = session_manager
        self.execution_transport = execution_transport
        self.user_id = user_id
        self.transport_capabilities = transport_capabilities or AgentTransportCapabilities()
        self.platform_runtime = platform_runtime

        if platform_runtime is not None and platform_runtime.observation_service is not None:
            self.observation_service = platform_runtime.observation_service
        else:
            observation_coordinator = ObservationCoordinator.for_local(
                app_config=app_config,
                agent_config=agent_config,
                artifact_store=artifact_store,
            )
            self.observation_service = DesktopObservationService(coordinator=observation_coordinator)
        self.state_store = RoundStateStore(session_manager, user_id=user_id)
        self.tool_surface_service = ToolSurfaceService()
        if agent_config.llm_config is None:
            raise ValueError("Desktop round execution requires agent_config.llm_config")
        self.trace_capture_service = TraceCaptureService(agent_config.llm_config)
        self.llm_trace_writer = LLMTraceLogWriter(artifact_store.llm_trace_log_path)
        if app_config.websocket.runtime_mode == RuntimeMode.REMOTE:
            self.local_device_executor = _RemoteModeLocalCommandExecutor()
        elif platform_runtime is not None and platform_runtime.action_executor is not None:
            self.local_device_executor = PlatformCommandExecutor(platform_runtime)
        else:
            self.local_device_executor = NorthCUADeviceCommandExecutor()
        self.round_evaluator_service = RoundEvaluatorService(
            app_config=app_config,
            agent_config=agent_config,
            artifact_store=artifact_store,
            execution_transport=execution_transport,
            session_manager=session_manager,
            user_id=user_id,
            transport_capabilities=self.transport_capabilities,
            tool_surface_service=self.tool_surface_service,
        )

        self.verification_capture = RoundEndVerificationCapture(
            desktop_policy=app_config.desktop,
            artifact_store=artifact_store,
            command_executor=self.local_device_executor,
        )
        self.step_persister = RoundStepPersister(
            artifact_store=artifact_store,
            state_store=self.state_store,
            round_coordinator=round_coordinator,
            session_manager=session_manager,
            user_id=user_id,
            llm_trace_writer=self.llm_trace_writer,
        )
        self.evaluation_scheduler = RoundEvaluationScheduler(
            round_evaluator_service=self.round_evaluator_service,
            app_config=app_config,
            artifact_store=artifact_store,
        )

    async def run_round(
        self,
        round_input: RoundInput,
        *,
        snapshot: DesktopStateSnapshot | None = None,
        observation_context: ObservationContext | None = None,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> StepLogRecord:
        """Reject the removed single-step entry; Desktop tasks use run_query."""

        raise ValueError("Desktop supports orchestration only; use run_query instead of run_round")

    async def capture_step_limit_verification(
        self,
        step_log: StepLogRecord,
        *,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Attach a final screenshot when a local task stops at max steps.

        The normal round-end hook only runs for model terminal decisions. Local
        max-step exits happen in the task runner after the last step is already
        persisted, so the facade calls this helper before rewriting
        ``steps.jsonl``.

        Args:
            step_log: Last persisted step log before the step limit was hit.
            remote_tool_dispatcher: Optional remote dispatcher used when the
                active desktop lives behind the websocket controller.
        """

        self.verification_capture.set_command_executor(self.local_device_executor)
        await self.verification_capture.capture_for_step_limit(
            step_log=step_log,
            runtime_mode=self.app_config.websocket.runtime_mode,
            remote_tool_dispatcher=remote_tool_dispatcher,
            previous_steps=self.round_coordinator.state.step_logs,
        )

    async def evaluate_step_limit(
        self,
        step_log: StepLogRecord,
        *,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Schedule a final evaluator pass for a max-step task exit.

        Args:
            step_log: Last persisted step log before the step limit was hit.
            remote_tool_dispatcher: Optional remote dispatcher used for
                evaluator client-side probe tools.
        """

        await self.evaluation_scheduler.run_step_limit_evaluation(
            step_log,
            list(self.round_coordinator.state.step_logs),
            remote_tool_dispatcher=remote_tool_dispatcher,
        )

    async def drain_background_tasks(self) -> None:
        """Drain background evaluator tasks before shutdown or CLI exit."""

        await self.evaluation_scheduler.drain()
