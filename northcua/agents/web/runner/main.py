"""Main step loop for the generic NorthCUA web agent."""

from __future__ import annotations

import logging
from pathlib import Path

from nexau.core.messages import Message

from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkit.external_tools import AgentKitExternalToolDispatcher, pending_tool_calls_from_meta
from northcua.app.logging import log_runtime_event
from northcua.core.config import WebAgentConfig
from northcua.core.json_types import JsonObject
from northcua.transport.ws.remote_tools import use_remote_tool_artifact_sink

from ..backend import WebActionBackend, WebActionBackendError, build_playwright_web_backend
from ..domain.actions import WebAgentActionType, WebWaitParams
from ..domain.evaluation_artifacts import WebObservationArtifact, WebToolResultArtifact
from ..domain.observations import WebObservationContext
from ..domain.panel_models import WebStepPanelRecord
from ..domain.results import WebAgentResult, WebStepRecord
from ..mcp.action_service import WebAgentMCPActionService
from ..mcp.registration import WEB_AGENT_MCP_REGISTRATION_HOOK
from ..observation.builder import WebObservationBuilder
from ..prompt import WebAgentPromptBuilder
from ..prompt.visual_window import WebPromptVisualHistoryTurn
from ..security import WebNavigationPolicy
from ..session import WebAgentSessionRegistry
from .action_resolution import ResolvedWebAgentAction, WebAgentActionResolver
from .artifact_persistence import WebAgentArtifactPersistence
from .model_step import WebAgentModelStepComponents, WebAgentModelStepExecutor, WebAgentModelStepResult
from .models import WebAgentRunnerCallbacks, WebAgentRunnerComponents, WebAgentRunnerRunSpec
from .observation_recovery import WebObservationRecoveryService
from .remote_artifacts import WebRemoteToolArtifactSink
from .run_loop import run_web_agent_loop
from .step_records import WebAgentStepRecordProjector

logger = logging.getLogger(__name__)


class WebAgentRunner:
    """DOM-first web-agent round loop backed by a configurable browser backend."""

    def __init__(self, components: WebAgentRunnerComponents) -> None:
        self.app_config = components.app_config
        self.agent_config = components.agent_config
        self.artifact_store = components.artifact_store
        self.execution_transport = components.execution_transport
        self.session_manager = components.session_manager
        self.user_id = components.user_id
        self.transport_capabilities = components.transport_capabilities
        self.client_factory = components.client_factory
        self.session_registry = WebAgentSessionRegistry()
        self.action_service = WebAgentMCPActionService(registry=self.session_registry)
        self.observation_recovery_service = WebObservationRecoveryService(
            action_service=self.action_service,
        )
        self.observation_builder = WebObservationBuilder()
        context_mode = self.app_config.context_management.resolve("web").mode
        history_window_rounds = self.app_config.prompt.history_window_rounds
        if context_mode == ContextManagementMode.ENFORCE:
            history_window_rounds = 2**31 - 1
        self.prompt_builder = WebAgentPromptBuilder(
            history_window_rounds=history_window_rounds,
            history_strategy=self.app_config.web.prompt_history_strategy,
            screenshot_window_images=self.app_config.prompt.screenshot_window_images,
            ref_snapshot_window_observations=self.app_config.prompt.ref_snapshot_window_observations,
            include_initial_observation_image=self.app_config.prompt.include_initial_observation_image,
            include_subsequent_observation_images=self.app_config.prompt.include_subsequent_observation_images,
        )
        self.shadow_prompt_builder = (
            WebAgentPromptBuilder(
                history_window_rounds=2**31 - 1,
                history_strategy=self.app_config.web.prompt_history_strategy,
                screenshot_window_images=self.app_config.prompt.screenshot_window_images,
                ref_snapshot_window_observations=self.app_config.prompt.ref_snapshot_window_observations,
                include_initial_observation_image=self.app_config.prompt.include_initial_observation_image,
                include_subsequent_observation_images=self.app_config.prompt.include_subsequent_observation_images,
            )
            if context_mode == ContextManagementMode.SHADOW
            else None
        )
        self.tool_surface_service = ToolSurfaceService()
        self.action_resolver = WebAgentActionResolver()
        self.model_step_executor = WebAgentModelStepExecutor(
            WebAgentModelStepComponents(
                app_config=self.app_config,
                agent_config=self.agent_config,
                execution_transport=self.execution_transport,
                user_id=self.user_id,
                transport_capabilities=self.transport_capabilities,
                prompt_builder=self.prompt_builder,
                tool_surface_service=self.tool_surface_service,
                shadow_prompt_builder=self.shadow_prompt_builder,
                memory_root=self.artifact_store.root,
            )
        )
        self.artifact_persistence = WebAgentArtifactPersistence(
            artifact_store=self.artifact_store,
            config=self.app_config.web,
        )
        self.step_record_projector = WebAgentStepRecordProjector(action_resolver=self.action_resolver)
        self.navigation_policy = WebNavigationPolicy(self.app_config.web)
        self.external_tool_dispatcher = AgentKitExternalToolDispatcher(
            server_name="WebAgentActionExecutor",
            registration_hook=WEB_AGENT_MCP_REGISTRATION_HOOK,
            tool_type="web_action",
            call_id_prefix="web-agent",
            device_executor=components.external_tool_device_executor,
        )

    async def run(
        self,
        spec: WebAgentRunnerRunSpec,
        *,
        callbacks: WebAgentRunnerCallbacks | None = None,
    ) -> WebAgentResult:
        """Run one web-agent task to completion, error, or step limit.

        Args:
            spec: User-facing run spec with query, initial URL, and optional
                step cap.
            callbacks: Optional hooks used by CLI/server presentation.

        Returns:
            Structured web-agent result with persisted step records.
        """

        callbacks = callbacks or WebAgentRunnerCallbacks()
        return await run_web_agent_loop(self, spec, callbacks)

    async def _prepare_browser_session(self, *, client: WebActionBackend, url: str) -> None:
        if not url:
            return
        validated_url = self.navigation_policy.validate_url(url)
        log_runtime_event(
            logger,
            "web_agent_initial_open",
            session_id=self.artifact_store.session_id,
            url=validated_url,
        )
        (await client.open(validated_url)).require_success()
        if self.app_config.web.post_action_wait_ms > 0:
            (await client.wait(WebWaitParams(duration_ms=self.app_config.web.post_action_wait_ms))).require_success()

    async def _run_model_step(
        self,
        *,
        user_query: str,
        session_id: str,
        step_index: int,
        session_step: int,
        round_num: int = 1,
        runtime_cursor: JsonObject | None = None,
        step_budget: JsonObject | None = None,
        tool_choice_policy: str = "auto",
        allowed_tool_names: tuple[str, ...] = (),
        observation: WebObservationContext,
        prior_messages: list[Message],
        visual_history_turns: list[WebPromptVisualHistoryTurn] | None = None,
    ) -> WebAgentModelStepResult:
        artifact_sink = WebRemoteToolArtifactSink(
            artifact_store=self.artifact_store,
            config=self.app_config.web,
            session_step=session_step,
        )
        with use_remote_tool_artifact_sink(artifact_sink):
            return await self.model_step_executor.run_step(
                user_query=user_query,
                session_id=session_id,
                step_index=step_index,
                session_step=session_step,
                round_num=round_num,
                runtime_cursor=runtime_cursor,
                step_budget=step_budget,
                tool_choice_policy=tool_choice_policy,
                allowed_tool_names=allowed_tool_names,
                observation=observation,
                prior_messages=prior_messages,
                visual_history_turns=visual_history_turns,
            )

    async def _resolve_step_actions(
        self,
        *,
        session_id: str,
        round_num: int = 1,
        step_index: int,
        step_result: WebAgentModelStepResult,
    ) -> list[ResolvedWebAgentAction]:
        """Resolve tool actions from local traces or paused external dispatch."""

        resolved_actions = self.action_resolver.resolve_all(step_result.trace_state.tool_executions)
        if resolved_actions:
            return resolved_actions
        pending_tool_calls = pending_tool_calls_from_meta(step_result.meta)
        if step_result.meta.get("stop_reason") != "EXTERNAL_TOOL_CALL" or not pending_tool_calls:
            return []
        dispatch_result = await self.external_tool_dispatcher.dispatch_pending_tools(
            session_id=session_id,
            round_num=round_num,
            round_step=step_index,
            pending_tool_calls=pending_tool_calls,
        )
        step_result.trace_state.tool_executions.extend(dispatch_result.tool_executions)
        log_runtime_event(
            logger,
            "web_agent_external_tools_completed",
            level=logging.DEBUG,
            session_id=session_id,
            step_index=step_index,
            pending_tool_call_count=len(pending_tool_calls),
            tool_execution_count=len(dispatch_result.tool_executions),
        )
        return self.action_resolver.resolve_all(step_result.trace_state.tool_executions)

    def _web_config_for_run(self, headed: bool | None = None) -> WebAgentConfig:
        """Return the web config effective for one run without mutating globals."""

        if headed is None:
            return self.app_config.web
        return self.app_config.web.model_copy(update={"headed": headed})

    def _build_client(self, session_id: str, *, headed: bool | None = None) -> WebActionBackend:
        if self.client_factory is not None:
            return self.client_factory(session_id)
        return build_playwright_web_backend(config=self._web_config_for_run(headed), session_id=session_id)

    def _build_step_record(
        self,
        *,
        step_index: int,
        round_num: int = 1,
        session_step: int | None = None,
        runtime_cursor: JsonObject | None = None,
        observation: WebObservationContext,
        step_result: WebAgentModelStepResult,
        resolved_actions: list[ResolvedWebAgentAction],
        artifact_paths: dict[str, Path],
        observation_artifact: WebObservationArtifact | None = None,
        tool_artifacts: list[WebToolResultArtifact] | None = None,
    ) -> WebStepRecord:
        return self.step_record_projector.build_step_record(
            session_id=self.artifact_store.session_id,
            step_index=step_index,
            round_num=round_num,
            session_step=session_step or step_index,
            runtime_cursor=dict(runtime_cursor or {}),
            observation=observation,
            step_result=step_result,
            resolved_actions=resolved_actions,
            artifact_paths=artifact_paths,
            observation_artifact=observation_artifact,
            tool_artifacts=tool_artifacts,
        )

    def _build_panel_record(
        self,
        step_record: WebStepRecord,
        observation: WebObservationContext,
    ) -> WebStepPanelRecord:
        return self.step_record_projector.build_panel_record(step_record, observation)

    async def _close_client(self, client: WebActionBackend) -> None:
        (await client.close()).require_success()

    def _assistant_text_from_step(self, step_result: WebAgentModelStepResult) -> str:
        return self.step_record_projector.assistant_text_from_step(step_result)
