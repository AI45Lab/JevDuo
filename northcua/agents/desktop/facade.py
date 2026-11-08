"""NexAU-backed NorthCUA agent facade."""

from __future__ import annotations

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager

from northcua.agentinfra.runtime.contracts import BeforeStepCallback, StepEventCallback
from northcua.agentinfra.runtime.facade import SessionScopedAgentFacade
from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities, DesktopRunRequest, DesktopRunResult
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import TaskStatus
from northcua.core.models import (
    CommandExecutionResultRecord,
    DesktopStateSnapshot,
    EvaluationLogRecord,
    ObservationContext,
    RoundInput,
    StepLogRecord,
)
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua.platforms.desktop import DesktopPlatformRuntime
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.transport.ws.remote_round import ExternalToolDispatcher
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from .round_execution import RoundExecutionService
from .runtime import DesktopRuntimeFactory, DesktopTaskRuntime
from .services.session_state import RoundStateStore
from .services.step_log_rewrite import rewrite_step_logs_preserving_existing
from .session import NorthCUARoundCoordinator


class NorthCUAAgentFacade(SessionScopedAgentFacade[NorthCUAArtifactStore]):
    """Session-scoped public API for desktop CUA execution.

    The facade owns session hydration, round queue mutation, and artifact-backed
    result persistence. It intentionally delegates the mechanics of one CUA
    round to :class:`RoundExecutionService` so callers can use the same public
    methods from the CLI, websocket server, and typed application facade.
    """

    def __init__(
        self,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        artifact_store: NorthCUAArtifactStore,
        session_manager: SessionManager,
        execution_transport: NorthCUAWebSocketTransport,
        user_id: str,
        transport_capabilities: AgentTransportCapabilities | None = None,
        platform_runtime: DesktopPlatformRuntime | None = None,
    ) -> None:
        """Initialize one desktop facade bound to an artifact-backed session.

        Args:
            app_config: NorthCUA application config.
            agent_config: NexAU agent config used for desktop rounds.
            artifact_store: Session-scoped artifact store.
            session_manager: Session persistence backend.
            execution_transport: Runtime transport used to execute tools.
            user_id: Runtime user identifier.
            transport_capabilities: Optional transport feature flags.
            platform_runtime: Optional platform runtime whose observation and
                action services should be reused by round execution.
        """
        super().__init__(
            app_config=app_config,
            agent_config=agent_config,
            session_manager=session_manager,
            execution_transport=execution_transport,
            artifact_store=artifact_store,
            user_id=user_id,
            transport_capabilities=transport_capabilities,
        )
        self.rounds = NorthCUARoundCoordinator(session_id=artifact_store.session_id)
        self.state_store = RoundStateStore(session_manager, user_id=user_id)
        self.round_execution_service = RoundExecutionService(
            app_config=app_config,
            agent_config=agent_config,
            artifact_store=artifact_store,
            round_coordinator=self.rounds,
            session_manager=session_manager,
            execution_transport=execution_transport,
            user_id=user_id,
            transport_capabilities=self.transport_capabilities,
            platform_runtime=platform_runtime,
        )
        self.runtime_factory = DesktopRuntimeFactory(
            artifact_store=artifact_store,
            round_coordinator=self.rounds,
            round_execution_service=self.round_execution_service,
            terminal_facade=self,
        )

    async def run_round(
        self,
        round_input: RoundInput,
        *,
        snapshot: DesktopStateSnapshot | None = None,
        observation_context: ObservationContext | None = None,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> StepLogRecord:
        """Run a single queued desktop CUA round.

        Args:
            round_input: User request and metadata for the round.
            snapshot: Optional pre-collected desktop snapshot.
            observation_context: Optional structured observation context.
            remote_tool_dispatcher: Optional remote callback for externally
                dispatched desktop tool calls.

        Returns:
            Persisted step log record for the completed round.
        """
        await self.hydrate_state()
        return await self.round_execution_service.run_round(
            round_input,
            snapshot=snapshot,
            observation_context=observation_context,
            remote_tool_dispatcher=remote_tool_dispatcher,
        )

    async def hydrate_state(self) -> None:
        """Refresh in-memory round state from the session manager.

        The state store is canonical across facade instances spawned for the
        same session, so public mutating helpers hydrate before touching the
        coordinator.
        """
        state = await self.state_store.load(
            self.rounds.state.session_id,
            response_log_path=self.artifact_store.response_log_path,
            evaluation_log_path=self.artifact_store.evaluation_log_path,
        )
        self.rounds.replace_state(state)

    async def enqueue_initial_request(self, query: str, metadata: dict[str, object] | None = None) -> None:
        """Persist the first user request for this session.

        Args:
            query: User task text to seed the round queue.
            metadata: Optional JSON-compatible metadata stored with the round
                input.
        """
        await self.hydrate_state()
        self.rounds.enqueue_initial_request(query, metadata=metadata)
        await self.state_store.save(self.rounds.state)

    async def enqueue_followup(self, query: str, metadata: dict[str, object] | None = None) -> None:
        """Persist a follow-up request for a later round.

        Args:
            query: User task text for the follow-up round.
            metadata: Optional JSON-compatible metadata stored with the round
                input.
        """
        await self.hydrate_state()
        self.rounds.enqueue_followup(query, metadata=metadata)
        await self.state_store.save(self.rounds.state)

    async def next_round(self) -> RoundInput | None:
        """Pop and persist the next round input.

        Returns:
            Next queued round input, a synthetic resume input when command
            results are pending, or ``None`` when no work remains.
        """
        await self.hydrate_state()
        round_input = self.rounds.next_round()
        await self.state_store.save(self.rounds.state)
        return round_input

    async def record_command_results(self, results: list[CommandExecutionResultRecord]) -> None:
        """Persist incoming remote device command results.

        Args:
            results: Command results received from the device/runtime.
        """
        state = await self.state_store.record_command_results(
            self.rounds.state.session_id,
            results,
            response_log_path=self.artifact_store.response_log_path,
            evaluation_log_path=self.artifact_store.evaluation_log_path,
        )
        self.rounds.replace_state(state)

    async def consume_command_results(self, call_ids: list[str]) -> list[CommandExecutionResultRecord] | None:
        """Consume persisted command results in requested call-id order.

        Args:
            call_ids: Tool call identifiers required by the waiting round.

        Returns:
            Matching command results when all are present, otherwise ``None``.
        """
        results = await self.state_store.consume_command_results(
            self.rounds.state.session_id,
            call_ids,
            response_log_path=self.artifact_store.response_log_path,
            evaluation_log_path=self.artifact_store.evaluation_log_path,
        )
        if results is None:
            return None
        await self.hydrate_state()
        return results

    async def drain_background_tasks(self) -> None:
        """Wait for facade-owned background evaluator work to finish."""
        await self.round_execution_service.drain_background_tasks()

    async def capture_step_limit_verification(
        self,
        step_log: StepLogRecord,
        *,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Persist a final screenshot for a task stopped by the step limit.

        This keeps max-step exits reviewable without changing their existing
        ``continue`` step status semantics until the caller applies a terminal
        override.

        Args:
            step_log: Last persisted step log before the step limit was hit.
            remote_tool_dispatcher: Optional remote dispatcher used when the
                active desktop lives behind the websocket controller.
        """
        if self.rounds.state.step_logs and self.rounds.state.step_logs[-1].session_step == step_log.session_step:
            self.rounds.state.step_logs[-1] = step_log
        await self.round_execution_service.capture_step_limit_verification(
            step_log,
            remote_tool_dispatcher=remote_tool_dispatcher,
        )
        await self._rewrite_step_logs()

    async def evaluate_step_limit(
        self,
        step_log: StepLogRecord,
        *,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Schedule final evaluator work for a task stopped by max steps.

        Args:
            step_log: Last persisted step log before the step limit was hit.
            remote_tool_dispatcher: Optional remote dispatcher used for
                evaluator client-side probe tools.
        """
        await self.hydrate_state()
        if self.rounds.state.step_logs and self.rounds.state.step_logs[-1].session_step == step_log.session_step:
            self.rounds.state.step_logs[-1] = step_log
        await self.round_execution_service.evaluate_step_limit(
            step_log,
            remote_tool_dispatcher=remote_tool_dispatcher,
        )

    async def persist_step_limit_terminal(
        self,
        *,
        request: str,
        step_log: StepLogRecord,
        decision: AgentTerminalDecision | None = None,
        advance_counters: bool = True,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Persist, verify, and evaluate a server-enforced max-step terminal.

        Args:
            request: User request associated with the terminal override.
            step_log: Synthetic or rewritten terminal step log.
            decision: Optional terminal decision used to normalize the step
                status/result before persistence.
            advance_counters: Whether legacy desktop counters should advance
                inside the facade. Shared runtime callers pass ``False``.
            remote_tool_dispatcher: Optional remote dispatcher used for final
                verification and evaluator client tools.
        """
        if decision is not None:
            step_log.status = TaskStatus.FAIL
            step_log.result = decision.final_answer
            step_log.terminal_decision = decision.to_json()
        await self.capture_step_limit_verification(step_log, remote_tool_dispatcher=remote_tool_dispatcher)
        await self.persist_terminal_override(request=request, step_log=step_log, advance_counters=advance_counters)
        await self.evaluate_step_limit(step_log, remote_tool_dispatcher=remote_tool_dispatcher)

    async def run_query(
        self,
        request: DesktopRunRequest,
        *,
        on_before_step: BeforeStepCallback | None = None,
        on_step: StepEventCallback[StepLogRecord] | None = None,
    ) -> DesktopRunResult:
        """Run one typed desktop query through this session facade.

        Args:
            request: Typed desktop run request for this session.
            on_before_step: Optional callback invoked with the one-based local
                loop step before each round starts.
            on_step: Optional callback receiving each persisted step log.

        Returns:
            Typed run result with terminal status, step count, and artifact
            paths.

        Raises:
            ValueError: If the request session does not match this facade.
        """
        if request.session_id != self.session_id:
            raise ValueError(f"Desktop facade session mismatch: expected {self.session_id}, got {request.session_id}")
        runner = DesktopTaskRuntime(self)
        result = await runner.run(
            request.query,
            max_steps=request.max_steps,
            max_session_steps=request.max_session_steps,
            metadata=dict(request.metadata),
            on_before_step=on_before_step,
            on_step=on_step,
        )
        terminal_decision = result.steps[-1].terminal_decision if result.steps else {}
        if result.hit_step_limit:
            terminal_decision = (
                result.steps[-1].terminal_decision
                if result.steps
                else AgentTerminalDecision.fail(
                    reason=AgentTerminalReason.MAX_STEPS,
                    final_answer="Task reached the configured step limit before reaching a terminal state.",
                    summary="The desktop task reached the max step limit.",
                ).to_json()
            )
        return DesktopRunResult(
            session_id=self.session_id,
            terminal_status=result.terminal_status.value if result.terminal_status is not None else None,
            hit_step_limit=result.hit_step_limit,
            primary_result_path=self.artifact_store.primary_trace_path,
            terminal_decision=terminal_decision,
            step_count=len(result.steps),
            step_log_path=self.artifact_store.step_log_path,
            runtime_log_path=self.artifact_store.runtime_log_path,
            evaluation_log_path=self.artifact_store.evaluation_log_path,
        )

    async def persist_terminal_override(
        self, *, request: str, step_log: StepLogRecord, advance_counters: bool = True
    ) -> None:
        """Persist a server-enforced terminal override into canonical state.

        Args:
            request: User request associated with the terminal override.
            step_log: Replacement step log for the latest completed step.
            advance_counters: Whether this method should advance legacy
                counters when appending a synthetic terminal step.
        """
        if self.rounds.state.step_logs and self.rounds.state.step_logs[-1].session_step == step_log.session_step:
            self.rounds.state.step_logs[-1] = step_log
        else:
            self.rounds.state.step_logs.append(step_log)
            if advance_counters:
                self.rounds.state.session_step += 1
                self.rounds.state.round_step += 1
        await self._rewrite_step_logs()
        evaluation_record = EvaluationLogRecord(
            session_id=step_log.session_id,
            round_num=step_log.round_num,
            session_step=step_log.session_step,
            status=step_log.status,
            request=request,
            result=step_log.result,
            summary=step_log.result,
            terminal_decision=dict(step_log.terminal_decision),
        )
        self.artifact_store.append_jsonl(
            self.artifact_store.evaluation_log_path, evaluation_record.model_dump(mode="json")
        )

    async def _rewrite_step_logs(self) -> None:
        """Persist in-memory step logs to session state and JSONL artifacts."""
        await self.state_store.save(self.rounds.state)
        rewrite_step_logs_preserving_existing(self.rounds.state.step_logs, self.artifact_store.step_log_path)

    def _spawn_with_artifact_store(self, artifact_store: NorthCUAArtifactStore) -> "NorthCUAAgentFacade":
        """Create an isolated facade for another session.

        Args:
            artifact_store: Artifact store for the target session.

        Returns:
            New desktop facade with cloned agent config and shared services.
        """
        facade = NorthCUAAgentFacade(
            app_config=self.app_config,
            agent_config=self.clone_agent_config(self.agent_config),
            artifact_store=artifact_store,
            session_manager=self.session_manager,
            execution_transport=self.execution_transport,
            user_id=self.user_id,
            transport_capabilities=self.transport_capabilities,
        )
        return facade
