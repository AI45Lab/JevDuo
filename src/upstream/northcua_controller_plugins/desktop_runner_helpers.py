"""Helper mixin for controller-owned desktop remote runner execution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, cast

from northcua.agentkit.controller_dispatch import command_result_record as _generic_command_result_record
from northcua.agentkit.controller_dispatch import failed_command_result_record as _generic_failed_command_result_record
from northcua.agents.desktop.facade import NorthCUAAgentFacade
from northcua.agents.desktop.observation import ObservationCoordinator, RemoteInternalProbeExecutor
from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua.core.budget import AgentStepBudget
from northcua.core.enums import ResultStatus, TaskStatus, WebSocketMessageType
from northcua.core.models import CommandExecutionResultRecord, ObservationContext
from northcua.core.protocol import CommandRequestPayload, NorthCUAWebSocketEnvelope
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua.transport.ws.result_blocks import RemoteToolDispatchResult, command_result_to_tool_result_block
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.payloads import CommandResult, TaskFinished
from northcua_protocol.state import CommandState, TaskState
from northcua_runtime_shared.execution import RuntimeExecutionError, raise_for_execution_failure

logger = logging.getLogger(__name__)


from northcua_controller_plugins.desktop_runner_evaluation_helpers import DesktopRemoteEvaluationPayloadMixin


class _DesktopRunnerCommandConfig(Protocol):
    @property
    def app_config_path(self) -> str:
        """Return the desktop app config path.

        Returns:
            Path to the app configuration used by the remote runner.
        """
        ...

    @property
    def artifact_root_dir_override(self) -> str | None:
        """Return the optional artifact root override.

        Returns:
            Controller-provided artifact root directory, if configured.
        """
        ...

    @property
    def command_timeout_seconds(self) -> float:
        """Return the per-command timeout for remote worker dispatch.

        Returns:
            Timeout in seconds for each remote command execution.
        """
        ...

    @property
    def max_steps_per_task(self) -> int:
        """Return the maximum controller steps per task.

        Returns:
            Configured round-level remote execution step cap.
        """
        ...


class DesktopRemoteTaskRunnerHelpers(DesktopRemoteEvaluationPayloadMixin):
    """Budget, observation, dispatch, and terminal-payload helpers."""

    config: _DesktopRunnerCommandConfig

    async def _collect_remote_observation_context(
        self,
        *,
        facade: NorthCUAAgentFacade,
        dispatcher: RemoteCommandDispatcher,
    ) -> ObservationContext:
        pending_results: dict[str, CommandExecutionResultRecord] = {}

        async def _send_payload(envelope: NorthCUAWebSocketEnvelope) -> None:
            if envelope.type != WebSocketMessageType.COMMAND_REQUEST:
                raise ValueError(f"Unsupported probe envelope type: {envelope.type}")
            payload = envelope.payload
            if not isinstance(payload, CommandRequestPayload):
                payload = CommandRequestPayload.model_validate(payload)
            for command in payload.commands:
                result = await dispatcher.dispatch(
                    tool_id=command.tool_name,
                    parameters=command.parameters,
                    command_id=command.call_id,
                    timeout_seconds=self.config.command_timeout_seconds,
                )
                pending_results[command.call_id] = _command_result_record(command.call_id, result)

        async def _await_results(call_ids: list[str]) -> list[CommandExecutionResultRecord]:
            return [pending_results.pop(call_id) for call_id in call_ids if call_id in pending_results]

        coordinator = ObservationCoordinator(
            app_config=facade.app_config,
            agent_config=facade.agent_config,
            artifact_store=facade.artifact_store,
            probe_executor=RemoteInternalProbeExecutor(
                session_id=facade.rounds.state.session_id,
                round_num=max(facade.rounds.state.round_num, 1),
                round_step=facade.rounds.state.round_step,
                send_payload=_send_payload,
                await_results=_await_results,
            ),
            platform_name="remote-controller",
        )
        return await coordinator.collect(
            session_step=facade.rounds.state.session_step,
            previous_steps=facade.rounds.state.step_logs,
        )

    async def _dispatch_external_tool_calls(
        self,
        *,
        dispatcher: RemoteCommandDispatcher,
        pending_tool_calls: list[JsonObject],
    ) -> RemoteToolDispatchResult:
        tool_results = []
        command_results: list[CommandExecutionResultRecord] = []
        for call in pending_tool_calls:
            call_id = str(call.get("id") or "")
            tool_name = str(call.get("name") or "")
            parameters = _json_object(call.get("input"))
            try:
                result = await dispatcher.dispatch(
                    tool_id=tool_name,
                    parameters=parameters,
                    command_id=call_id,
                    timeout_seconds=self.config.command_timeout_seconds,
                )
                command_result = _command_result_record(call_id, result)
            except RuntimeExecutionError:
                raise
            except Exception as exc:
                logger.warning(
                    "desktop_remote_tool_dispatch_failed",
                    extra={
                        "tool_name": tool_name,
                        "call_id": call_id,
                        "parameter_keys": sorted(parameters),
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                command_result = _failed_command_result_record(call_id=call_id, error=str(exc))
            command_results.append(command_result)
            tool_results.append(command_result_to_tool_result_block(command_result))
        return RemoteToolDispatchResult(tool_blocks=tool_results, command_results=command_results)

    @staticmethod
    def _task_finished_from_step(
        context: ControllerExecutionContext,
        status: TaskStatus,
        *,
        step_log,
    ) -> TaskFinished:
        if status == TaskStatus.FINISH:
            return DesktopRemoteTaskRunnerHelpers._build_terminal_payload(
                context,
                state=TaskState.FINISHED,
                result=step_log.result,
                terminal_decision=step_log.terminal_decision,
            )
        return DesktopRemoteTaskRunnerHelpers._build_terminal_payload(
            context,
            state=TaskState.FAILED,
            error=str(step_log.result or "desktop remote task failed"),
            terminal_decision=step_log.terminal_decision,
        )

    @staticmethod
    def _build_terminal_payload(
        context: ControllerExecutionContext,
        *,
        state: TaskState,
        result: object | None = None,
        error: str | None = None,
        terminal_decision: JsonObject | None = None,
    ) -> TaskFinished:
        return TaskFinished(
            task_id=context.request.task_id,
            trace_id=context.request.trace_id,
            session_id=context.session_id,
            device_id=context.lease.device_id,
            lease_id=context.lease.lease_id,
            state=state,
            result=_protocol_result(result) if error is None else None,
            error=error,
            terminal_decision=terminal_decision
            or (
                AgentTerminalDecision.error(
                    reason=AgentTerminalReason.RUNTIME_ERROR,
                    final_answer=error,
                    summary="The desktop remote runner failed before producing a step result.",
                    retryable=True,
                ).to_json()
                if error
                else {}
            ),
        )

    @staticmethod
    def _path_has_bytes(path: Path) -> bool:
        """Return whether a controller log artifact exists and is non-empty."""

        return path.exists() and path.is_file() and path.stat().st_size > 0

    def _budget_for_request(self, context: ControllerExecutionContext) -> AgentStepBudget:
        """Resolve controller-side round/session step budgets for one task."""

        metadata_max_steps = context.request.metadata.get("max_steps")
        if isinstance(metadata_max_steps, int) and metadata_max_steps > 0:
            max_steps = min(metadata_max_steps, self.config.max_steps_per_task)
        elif isinstance(metadata_max_steps, str) and metadata_max_steps.isdigit():
            parsed = int(metadata_max_steps)
            max_steps = min(parsed, self.config.max_steps_per_task) if parsed > 0 else self.config.max_steps_per_task
        else:
            max_steps = self.config.max_steps_per_task

        metadata_max_session_steps = context.request.metadata.get("max_session_steps")
        if isinstance(metadata_max_session_steps, int) and metadata_max_session_steps > 0:
            max_session_steps = metadata_max_session_steps
        elif isinstance(metadata_max_session_steps, str) and metadata_max_session_steps.isdigit():
            max_session_steps = int(metadata_max_session_steps)
        else:
            max_session_steps = 0
        return AgentStepBudget(max_round_steps=max_steps, max_session_steps=max_session_steps)

    @staticmethod
    async def _drain_evaluators_before_finish(facade: NorthCUAAgentFacade) -> None:
        """Drain facade-owned evaluator work before task terminal emission."""

        drain = getattr(facade, "drain_background_tasks", None)
        if callable(drain):
            await cast(Any, drain)()


def _command_result_record(call_id: str, result: CommandResult) -> CommandExecutionResultRecord:
    # Only the Desktop owner has adopted the typed terminal semantics. Shared
    # AgentKit/Web conversion retains its original result-returning contract.
    raise_for_execution_failure(result)
    return _generic_command_result_record(call_id, result)


def _failed_command_result_record(*, call_id: str, error: str) -> CommandExecutionResultRecord:
    """Build a failed tool result when controller-side dispatch rejects input.

    Model-generated tool arguments can fail controller descriptor validation
    before they reach the device. Returning a normal failed tool result keeps
    the agent loop alive so the next step can correct the call.
    """

    return _generic_failed_command_result_record(call_id=call_id, error=error)


def _json_object(value: object) -> JsonObject:
    """Normalize an external tool-call payload into a JSON object."""

    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _protocol_result(value: object | None) -> JsonObject | str | None:
    """Coerce controller terminal output into the protocol result boundary."""

    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return str(value)
