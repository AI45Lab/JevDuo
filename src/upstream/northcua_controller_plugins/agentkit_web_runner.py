"""AgentKit remote runner factory for the bundled web agent."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from time import perf_counter
from typing import cast

from pydantic import BaseModel

from northcua.agentkit.controller_runner import (
    AgentKitRemoteRunnerConfig,
    AgentKitRemoteTaskRunnerBase,
    metadata_int,
    trace_artifacts,
)
from northcua.agentkit.spec import AgentSpec
from northcua.agentkernel.tools.surface import normalize_allowed_tool_names
from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.processes.transport import CommandTransport
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua.controller.execution.sdk import ControllerSDK
from northcua.core.enums import RuntimeMode
from northcua.core.json_types import JsonObject
from northcua.evaluation import disable_agentic_evaluator_for_external_script
from northcua.policy_routes import PolicyRouteResolver, apply_task_policy_route
from northcua_controller_plugins.agentkit_remote_device_executor import RemoteAgentKitDeviceCommandExecutor
from northcua_controller_plugins.agentkit_web_remote_dispatch import RemoteWebActionBackend
from northcua_controller_plugins.web_runner_evaluation_helpers import (
    persist_web_evaluation_result,
    run_web_round_evaluator,
    web_evaluation_enabled,
    web_evaluation_fail_open,
)
from northcua_protocol.payloads import TaskFinished, WebAgentRuntimeSettings
from northcua_protocol.state import TaskState
from northcua_runtime_shared.validation import ToolParameterValidationError

logger = logging.getLogger(__name__)
_APP_STOP_TIMEOUT_SECONDS = 10.0


def _web_parameter_validation_error_payload(
    tool_name: str,
    exc: ToolParameterValidationError,
) -> JsonObject | None:
    """Project controller-side schema rejection into the Web tool contract."""

    from northcua.agents.web.domain.actions import action_from_tool_name
    from northcua.agents.web.mcp.error_classification import build_web_tool_error_payload

    action = action_from_tool_name(tool_name)
    if action is None:
        return None
    payload = cast(JsonObject, build_web_tool_error_payload(action, exc))
    if action.value == "wait":
        payload["error"] = "web_wait arguments must contain exactly one wait target."
    else:
        payload["error"] = f"{tool_name} arguments do not match the tool schema."
        payload["hint"] = (
            f"Fix the {tool_name} arguments according to its schema, remove unsupported fields, omit unused null "
            "placeholders, and retry."
        )
    return payload


def _metadata_bool(value: object) -> bool | None:
    """Parse optional boolean task metadata without accepting ambiguous values."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


class WebAgentKitRemoteTaskRunner(AgentKitRemoteTaskRunnerBase):
    """Controller-owned web runner backed by browser device commands."""

    def __init__(
        self,
        *,
        config: AgentKitRemoteRunnerConfig,
        sdk: ControllerSDK | None = None,
        command_transport_factory: Callable[[ControllerExecutionContext, Mapping[str, object]], CommandTransport]
        | None = None,
        policy_route_resolver: PolicyRouteResolver | None = None,
    ) -> None:
        """Configure either in-process SDK or worker-process IPC dispatch."""

        if sdk is None and command_transport_factory is None:
            raise ValueError("WebAgentKitRemoteTaskRunner requires sdk or command_transport_factory")
        super().__init__(config=config, sdk=sdk)
        self.command_transport_factory = command_transport_factory
        self.policy_route_resolver = policy_route_resolver

    async def run(self, context: ControllerExecutionContext) -> TaskFinished:
        """Run a web task through a leased remote browser device.

        Args:
            context: Controller task context with lease, metadata, and request
                text.

        Returns:
            Terminal protocol event with result and trace artifacts.
        """

        from northcua.agents.web import WebAgentApplication
        from northcua.agents.web.security import WebNavigationPolicy

        app = WebAgentApplication.from_app_config(
            self.config.app_config_path,
            session_id=context.session_id,
            artifact_root_dir_override=self.config.artifact_root_dir_override,
        )
        disable_agentic_evaluator_for_external_script(app, context.request.metadata)
        apply_task_policy_route(app, context.request.metadata, resolver=self.policy_route_resolver)
        app.app_config.websocket = app.app_config.websocket.model_copy(update={"runtime_mode": RuntimeMode.REMOTE})
        try:
            dispatcher = self._build_dispatcher(context)
            headed = _metadata_bool(context.request.metadata.get("headed"))
            web_config = app.app_config.web
            if headed is not None:
                web_config = web_config.model_copy(update={"headed": headed})
            url = str(context.request.metadata.get("url") or "")
            if url:
                url = WebNavigationPolicy(web_config).validate_url(url)
            web_settings = _web_runtime_side_channel_settings(web_config)
            command_timeout_seconds = _effective_web_command_timeout(
                web_config=web_config,
                fallback=self.config.command_timeout_seconds,
            )
            action_device_executor = RemoteAgentKitDeviceCommandExecutor(
                dispatcher,
                namespace="web",
                timeout_seconds=command_timeout_seconds,
                side_channel_settings=web_settings,
                dynamic_side_channel_settings=lambda: _web_ref_alias_side_channel(context.session_id),
                runtime_sidecar_handler=lambda sidecar: _consume_web_runtime_sidecar(context.session_id, sidecar),
                parameter_validation_error_projector=_web_parameter_validation_error_payload,
            )
            max_steps = metadata_int(
                context.request.metadata.get("max_steps"),
                min(web_config.max_steps, self.config.max_steps_per_task),
                upper_bound=self.config.max_steps_per_task,
            )
            max_session_steps = metadata_int(
                context.request.metadata.get("max_session_steps"),
                0,
            )
            evaluator_artifacts = []

            async def evaluate_before_close(result):
                nonlocal evaluator_artifacts
                if not web_evaluation_enabled(app):
                    return
                try:
                    outcome = await run_web_round_evaluator(
                        app,
                        remote=True,
                        round_num=result.round_num,
                        task_id=context.request.task_id,
                        remote_dispatcher=dispatcher,
                        command_timeout_seconds=command_timeout_seconds,
                    )
                    result.evaluation_report = outcome.report
                    result.evaluation_report_path = outcome.report_path
                    evaluator_artifacts = list(outcome.artifacts)
                except Exception as exc:
                    result.evaluation_error = str(exc)
                    logger.exception("Web evaluator failed", extra={"task_id": context.request.task_id})

            result = await app.facade.run_web(
                user_query=context.request.request,
                url=url,
                max_steps=max_steps,
                max_session_steps=max_session_steps,
                tool_choice_policy=str(context.request.metadata.get("tool_choice_policy", "auto")),
                allowed_tool_names=normalize_allowed_tool_names(context.request.metadata.get("allowed_tool_names")),
                headed=headed,
                client_factory=lambda _session_id: RemoteWebActionBackend(
                    dispatcher,
                    timeout_seconds=command_timeout_seconds,
                    web_settings=web_settings,
                ),
                external_tool_device_executor=action_device_executor,
                on_finalize=evaluate_before_close,
            )
            terminal_payload: dict[str, object] = {
                "result_path": str(app.artifact_store.result_path),
                "answer": result.final_answer,
                "evaluation_report": result.evaluation_report,
                "evaluation_report_path": result.evaluation_report_path,
                "evaluation_error": result.evaluation_error,
            }
            if result.evaluation_error and not web_evaluation_fail_open(app):
                return self._terminal(
                    context,
                    state=TaskState.FAILED,
                    result=terminal_payload,
                    error=f"web evaluator failed: {result.evaluation_error}",
                    artifacts=trace_artifacts(context, app.artifact_store),
                    terminal_decision=result.to_terminal_decision().to_json(),
                )
            success = result.status.value == "success" and not result.hit_step_limit
            artifacts = [*trace_artifacts(context, app.artifact_store), *evaluator_artifacts]
            return self._terminal(
                context,
                state=TaskState.FINISHED if success else TaskState.FAILED,
                result=terminal_payload,
                error=None if success else "; ".join(result.tool_errors) or result.final_answer,
                artifacts=artifacts,
                terminal_decision=result.to_terminal_decision().to_json(),
            )
        except Exception as exc:  # pragma: no cover - defensive controller boundary
            logger.exception("AgentKit web remote runner failed", extra={"task_id": context.request.task_id})
            return self._terminal(context, state=TaskState.FAILED, error=str(exc))
        finally:
            await _stop_app_best_effort(app=app, task_id=context.request.task_id, session_id=context.session_id)

    def _build_dispatcher(self, context: ControllerExecutionContext) -> RemoteCommandDispatcher:
        """Build a dispatcher without coupling the runner to executor mode."""

        if self.command_transport_factory is not None:
            return RemoteCommandDispatcher(transport=self.command_transport_factory(context, {}))
        if self.sdk is None:  # pragma: no cover - constructor enforces this invariant
            raise RuntimeError("Web runner command authority is unavailable")
        return RemoteCommandDispatcher(
            sdk=self.sdk,
            lease=context.lease,
            progress_sink=context.progress_sink,
        )


def build_web_remote_task_runner(
    *,
    spec: AgentSpec,
    config: AgentKitRemoteRunnerConfig,
    sdk: ControllerSDK,
) -> WebAgentKitRemoteTaskRunner:
    """Build the web remote runner for an AgentKit spec.

    Args:
        spec: Agent spec requesting the runner; currently used for registry
            dispatch only.
        config: Controller runner configuration.
        sdk: Controller SDK used to dispatch device commands.

    Returns:
        Configured web remote task runner.
    """

    del spec
    return WebAgentKitRemoteTaskRunner(config=config, sdk=sdk)


def _web_runtime_side_channel_settings(web_config: object) -> dict[str, object]:
    """Project a full WebAgentConfig into the safe remote side-channel subset."""

    if isinstance(web_config, BaseModel):
        raw = web_config.model_dump(mode="json")
    elif callable(model_dump := getattr(web_config, "model_dump", None)):
        candidate = model_dump(mode="json")
        raw = candidate if isinstance(candidate, dict) else {}
    elif isinstance(web_config, dict):
        raw = dict(web_config)
    else:
        raw = {}
    allowed = set(WebAgentRuntimeSettings.model_fields)
    return {key: value for key, value in raw.items() if key in allowed and value is not None}


def _web_ref_alias_side_channel(session_id: str) -> dict[str, object]:
    """Return current model-visible ref aliases for remote device execution."""

    from northcua.agents.web.session import WebAgentSessionRegistry

    try:
        aliases = WebAgentSessionRegistry().get_registered_session(session_id).current_ref_aliases
    except LookupError:
        return {}
    if not aliases.aliases and not aliases.observation_id:
        return {}
    return {
        "ref_alias_observation_id": aliases.observation_id,
        "ref_alias_epoch": aliases.alias_epoch,
        "ref_aliases": [alias.model_dump(mode="json") for alias in aliases.aliases],
    }


def _consume_web_runtime_sidecar(session_id: str, sidecar: object) -> None:
    """Mirror device-owned web observation aliases back to controller state."""

    if not isinstance(sidecar, dict):
        return
    raw = sidecar.get("web_ref_aliases")
    if not isinstance(raw, dict):
        return
    from northcua.agents.web.observation.ref_alias import WebObservationRefAliases
    from northcua.agents.web.session import WebAgentSessionRegistry

    try:
        aliases = WebObservationRefAliases.model_validate(raw)
        session = WebAgentSessionRegistry().get_registered_session(session_id)
    except Exception:
        return
    session.apply_external_ref_aliases(aliases)


def _effective_web_command_timeout(*, web_config: object, fallback: float) -> float:
    """Return the command timeout that should govern remote web actions.

    The web backend owns browser action semantics, including navigation settle
    and post-action observation capture.  The controller-level timeout is only
    a safety fallback for generic AgentKit runners; remote web runs should honor
    the web runtime setting so local and E2B behavior stay aligned.
    """

    raw_timeout = getattr(web_config, "command_timeout_seconds", None)
    if not isinstance(raw_timeout, (int, float, str)):
        raw_timeout = 0.0
    try:
        parsed = float(raw_timeout)
    except (TypeError, ValueError):
        parsed = 0.0
    fallback = max(1.0, float(fallback))
    return parsed if parsed > 0 else fallback


async def _stop_app_best_effort(*, app: object, task_id: str, session_id: str) -> None:
    """Stop the web application without blocking terminal task emission."""

    stop = getattr(app, "stop", None)
    if not callable(stop):
        return
    started_at = perf_counter()
    try:
        stop_result = stop()
        if inspect.isawaitable(stop_result):
            await asyncio.wait_for(cast(Awaitable[object], stop_result), timeout=_APP_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "agentkit_web_remote_app_stop_timed_out",
            extra={
                "task_id": task_id,
                "session_id": session_id,
                "timeout_seconds": _APP_STOP_TIMEOUT_SECONDS,
                "elapsed_ms": int((perf_counter() - started_at) * 1000),
            },
        )
    except Exception:
        logger.warning(
            "agentkit_web_remote_app_stop_failed",
            extra={
                "task_id": task_id,
                "session_id": session_id,
                "elapsed_ms": int((perf_counter() - started_at) * 1000),
            },
            exc_info=True,
        )
