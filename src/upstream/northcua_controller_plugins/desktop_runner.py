"""Controller-owned desktop remote harness executor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from northcua.agentinfra.runtime.protocol import DesktopStepEvent
from northcua.agents.desktop.application import DesktopApplication
from northcua.agents.desktop.runtime import (
    DesktopObservationContextProvider,
    DesktopRuntimeOptions,
    DesktopTaskRuntime,
)
from northcua.app.logging import runtime_logging_context
from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.processes.transport import CommandTransport
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua.controller.execution.runner_plugins import ControllerRunnerRegistrationContext
from northcua.controller.execution.runner_registry import RunnerRegistry
from northcua.controller.execution.sdk import ControllerSDK
from northcua.core.coordinates import (
    CoordinatePolicy,
    CoordinateTarget,
    coordinate_policy_context_values,
    policy_from_config,
)
from northcua.core.enums import CoordinateSpace, RuntimeMode
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua.evaluation import disable_agentic_evaluator_for_external_script
from northcua.policy_routes import PolicyRouteResolver, apply_task_policy_route
from northcua.telemetry.progress import RuntimeProgressEventType
from northcua.telemetry.contracts import FailureCategory
from northcua_controller_plugins.desktop_artifacts import DesktopRemoteArtifactCollector
from northcua_controller_plugins.desktop_evaluation_progress import drain_evaluators_with_progress
from northcua_controller_plugins.desktop_runner_helpers import DesktopRemoteTaskRunnerHelpers
from northcua_controller_plugins.desktop_runner_lifecycle import (
    finalize_payload_with_artifacts,
    log_lifecycle,
    stop_app_best_effort,
)
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import TaskFinished, TelemetryUpdate
from northcua_protocol.state import CommandState, TaskState
from northcua_runtime_shared.execution import RuntimeExecutionError

logger = logging.getLogger("northcua.controller_plugins.desktop_runner")


def _observe_background_step_progress(task: asyncio.Task[None]) -> None:
    """Consume background progress task exceptions so they do not leak."""

    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.debug("desktop_remote_step_progress_task_failed", exc_info=True)


@dataclass(slots=True, frozen=True)
class DesktopRemoteRunnerConfig:
    """Configuration for controller-owned desktop remote execution.

    Attributes:
        app_config_path: Path to the desktop app config used to bootstrap the
            normal local desktop runtime inside the controller.
        artifact_root_dir_override: Optional artifact root for controller-side
            run logs and traces.
        max_steps_per_task: Controller safety budget for one remote desktop
            task before it is failed with a terminal override.
        command_timeout_seconds: Per-command timeout passed to the remote
            command dispatcher for observation probes and external tool calls.
    """

    app_config_path: str
    artifact_root_dir_override: str | None = None
    max_steps_per_task: int = 100
    command_timeout_seconds: float = 30.0


class DesktopRemoteTaskRunner(DesktopRemoteTaskRunnerHelpers):
    """Run the desktop agent in-process while delegating tools to a leased device."""

    def __init__(
        self,
        *,
        config: DesktopRemoteRunnerConfig,
        sdk: ControllerSDK | None = None,
        command_transport_factory: (
            Callable[[ControllerExecutionContext, Mapping[str, object]], CommandTransport] | None
        ) = None,
        startup_phase_sink: Callable[[str], None] | None = None,
        runtime_progress_sink_enabled: bool = False,
        policy_route_resolver: PolicyRouteResolver | None = None,
    ) -> None:
        self.config = config
        self.sdk = sdk
        self.command_transport_factory = command_transport_factory
        self.startup_phase_sink = startup_phase_sink
        self.runtime_progress_sink_enabled = runtime_progress_sink_enabled
        self.policy_route_resolver = policy_route_resolver
        self.artifact_collector = DesktopRemoteArtifactCollector()

    async def run(self, context: ControllerExecutionContext) -> TaskFinished:
        """Execute one controller task through the desktop round lifecycle.

        Args:
            context: Controller execution context containing the accepted task,
                sticky session id, and active device lease.

        Returns:
            Terminal task payload with controller-side trace artifacts attached.
        """

        phase_callback = self._startup_phase_callback(context)
        app = await asyncio.to_thread(
            DesktopApplication.from_app_config,
            self.config.app_config_path,
            session_id=context.session_id,
            artifact_root_dir_override=self.config.artifact_root_dir_override,
            runtime_mode_override=RuntimeMode.REMOTE,
            bootstrap_phase_callback=phase_callback,
        )
        disable_agentic_evaluator_for_external_script(app, context.request.metadata)
        apply_task_policy_route(app, context.request.metadata, resolver=self.policy_route_resolver)
        phase_callback("runner_started")
        with runtime_logging_context(log_path=app.artifact_store.runtime_log_path, session_id=context.session_id):
            try:
                payload = await self._execute_with_app(app=app, context=context)
            except Exception as exc:  # pragma: no cover - defensive controller boundary
                log_lifecycle(
                    context,
                    "desktop_remote_runner_failed_before_terminal_payload",
                    level=logging.ERROR,
                    error=str(exc),
                )
                logger.exception(
                    "Desktop remote runner failed before reaching a terminal task payload",
                    extra={"task_id": context.request.task_id, "session_id": context.session_id},
                )
                command_cancelled = isinstance(exc, RuntimeExecutionError) and exc.status == CommandState.CANCELLED
                payload = self._build_terminal_payload(
                    context,
                    state=TaskState.INTERRUPTED if command_cancelled else TaskState.FAILED,
                    error=str(exc),
                    terminal_decision=(
                        AgentTerminalDecision.error(
                            reason=AgentTerminalReason.INTERRUPTED,
                            final_answer=str(exc),
                            retryable=False,
                        ).to_json()
                        if command_cancelled
                        else None
                    ),
                )
            finally:
                await stop_app_best_effort(app=app, context=context)
            return finalize_payload_with_artifacts(
                context=context,
                payload=payload,
                artifact_store=app.artifact_store,
                artifact_collector=self.artifact_collector,
                attach_latest_evaluation=self._with_latest_evaluation,
            )

    def _startup_phase_callback(self, context: ControllerExecutionContext) -> Callable[[str], None]:
        """Build a thread-safe startup phase reporter for blocking bootstrap work."""

        def _report(phase: str) -> None:
            log_lifecycle(context, f"desktop_remote_startup_{phase}", phase=phase)
            if self.startup_phase_sink is not None:
                try:
                    self.startup_phase_sink(phase)
                except Exception:
                    logger.debug(
                        "desktop_remote_startup_phase_sink_failed",
                        exc_info=True,
                        extra={"task_id": context.request.task_id, "session_id": context.session_id, "phase": phase},
                    )

        return _report

    async def _execute_with_app(self, *, app: DesktopApplication, context: ControllerExecutionContext) -> TaskFinished:
        """Execute the desktop round once the controller app has started."""

        log_lifecycle(context, "desktop_remote_runner_started")
        facade = app.facade
        task_query = context.request.request
        await facade.enqueue_initial_request(task_query, metadata=context.request.metadata)
        round_input = await facade.next_round()
        if round_input is None:
            return self._build_terminal_payload(
                context,
                state=TaskState.INTERRUPTED,
                error="controller executor found no pending desktop round",
            )
        metadata_overrides = _desktop_coordinate_metadata(app.app_config)
        if self.command_transport_factory is not None:
            dispatcher = RemoteCommandDispatcher(
                transport=self.command_transport_factory(context, metadata_overrides),
            )
        else:
            if self.sdk is None:
                raise RuntimeError("desktop remote runner requires sdk or command_transport_factory")
            dispatcher = RemoteCommandDispatcher(
                sdk=self.sdk,
                lease=context.lease,
                progress_sink=context.progress_sink,
                metadata_overrides=metadata_overrides,
            )
        step_log = await self._run_desktop_task_runtime(
            facade=facade,
            round_input=round_input,
            dispatcher=dispatcher,
            budget=self._budget_for_request(context),
            context=context,
        )
        try:
            log_lifecycle(context, "desktop_remote_evaluator_drain_started")
            await drain_evaluators_with_progress(
                facade=facade,
                context=context,
                drain=self._drain_evaluators_before_finish,
            )
        except Exception as exc:
            log_lifecycle(
                context,
                "desktop_remote_evaluator_drain_failed",
                level=logging.ERROR,
                error=str(exc),
            )
            logger.exception(
                "Desktop remote runner evaluator drain failed",
                extra={"task_id": context.request.task_id, "session_id": context.session_id},
            )
            return self._build_terminal_payload(
                context,
                state=TaskState.FAILED,
                error=str(exc),
                terminal_decision=AgentTerminalDecision.error(
                    reason=AgentTerminalReason.RUNTIME_ERROR,
                    final_answer=str(exc),
                    summary="The desktop evaluator failed before producing required evaluation artifacts.",
                    retryable=True,
                    metadata={
                        "failure_category": FailureCategory.EVALUATOR.value,
                        "failure_phase": "evaluator",
                    },
                ).to_json(),
            )
        log_lifecycle(context, "desktop_remote_evaluator_drain_completed")
        payload = self._task_finished_from_step(context, step_log.status, step_log=step_log)
        log_lifecycle(
            context,
            "desktop_remote_terminal_payload_built",
            state=payload.state.value,
            has_error=payload.error is not None,
        )
        return payload

    async def _run_desktop_task_runtime(
        self,
        *,
        facade,
        round_input,
        dispatcher: RemoteCommandDispatcher,
        budget,
        context: ControllerExecutionContext,
    ):
        """Run a remote desktop task through the shared desktop runtime."""

        async def _collect_observation(session_step, previous_steps):
            del session_step, previous_steps
            return await self._collect_remote_observation_context(facade=facade, dispatcher=dispatcher)

        async def _emit_runtime_progress(event: dict[str, object]) -> None:
            if context.progress_sink is None:
                return
            payload_event = dict(event)
            payload_event.update(
                {
                    "task_id": context.request.task_id,
                    "trace_id": context.request.trace_id,
                    "session_id": context.session_id,
                    "worker_heartbeat_only": True,
                }
            )
            try:
                await context.progress_sink(
                    MessageType.TELEMETRY_UPDATE,
                    TelemetryUpdate(events=[payload_event]),
                )
            except Exception:
                logger.debug(
                    "desktop_remote_runtime_progress_send_failed",
                    exc_info=True,
                    extra={
                        "task_id": context.request.task_id,
                        "session_id": context.session_id,
                        "phase": payload_event.get("phase"),
                    },
                )

        def _emit_step_progress(step_log) -> None:
            if context.progress_sink is None:
                return
            step_event = DesktopStepEvent.from_step_log(step_log)
            payload = TelemetryUpdate(
                events=[
                    {
                        "event_type": RuntimeProgressEventType.DESKTOP_STEP.value,
                        "task_id": context.request.task_id,
                        "trace_id": context.request.trace_id,
                        "session_id": context.session_id,
                        "step": {
                            "step_index": step_event.step_index,
                            "status": step_event.status,
                            "summary": step_event.summary,
                            "assistant_text": step_event.assistant_text,
                            "thought": step_event.thought,
                            "tool_count": step_event.tool_count,
                        },
                    }
                ]
            )

            async def _send_step_progress() -> None:
                assert context.progress_sink is not None
                try:
                    await context.progress_sink(MessageType.TELEMETRY_UPDATE, payload)
                except Exception:
                    logger.debug(
                        "desktop_remote_step_progress_send_failed",
                        exc_info=True,
                        extra={"task_id": context.request.task_id, "session_id": context.session_id},
                    )

            task = asyncio.create_task(_send_step_progress())
            task.add_done_callback(_observe_background_step_progress)

        task_result = await DesktopTaskRuntime(facade).run_round_input(
            round_input,
            max_steps=budget.max_round_steps or self.config.max_steps_per_task,
            max_session_steps=budget.max_session_steps,
            options=DesktopRuntimeOptions(
                observation_provider=DesktopObservationContextProvider(_collect_observation),
                remote_tool_dispatcher=lambda pending_tool_calls: self._dispatch_external_tool_calls(  # noqa: E731
                    dispatcher=dispatcher,
                    pending_tool_calls=pending_tool_calls,
                ),
                runtime_progress_sink=_emit_runtime_progress if self.runtime_progress_sink_enabled else None,
            ),
            drain_background_tasks=False,
            on_step=_emit_step_progress,
        )
        if not task_result.steps:
            raise RuntimeError("desktop remote harness did not execute any steps")
        return task_result.steps[-1]


def _desktop_coordinate_metadata(app_config: object) -> dict[str, str]:
    desktop_config = getattr(app_config, "desktop", None)
    policy_config = getattr(desktop_config, "coordinate_policy", None)
    policy = (
        policy_from_config(policy_config)
        if policy_config is not None
        else CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS)
    )
    return coordinate_policy_context_values(CoordinateTarget.DESKTOP, policy)


def register_controller_runner(
    runner_registry: RunnerRegistry,
    sdk: ControllerSDK,
    context: ControllerRunnerRegistrationContext,
) -> None:
    """Register the desktop controller-owned remote runner plugin.

    Args:
        runner_registry: Controller runner registry to populate.
        sdk: Controller SDK used by the remote command dispatcher.
        context: Startup-time controller execution context.
    """

    runner_registry.register(
        "desktop",
        DesktopRemoteTaskRunner(
            config=DesktopRemoteRunnerConfig(
                app_config_path=context.app_config_path,
                artifact_root_dir_override=context.artifact_root_dir_override,
                command_timeout_seconds=context.command_timeout_seconds,
            ),
            sdk=sdk,
        ).run,
    )
