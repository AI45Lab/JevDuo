"""Facade wrapping one generic web-agent runtime session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager

from northcua.agentinfra.runtime.facade import SessionScopedAgentFacade
from northcua.agentinfra.runtime.protocol import (
    AgentTransportCapabilities,
    WebRunRequest,
    WebRunResult,
)
from northcua.agentkernel.tools.surface import normalize_allowed_tool_names
from northcua.agentkit.external_tools import AgentKitCommandExecutor
from northcua.core.config import NorthCUAAppConfig
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from .artifacts import WebArtifactStore
from .domain.panel_models import WebStepPanelRecord
from .domain.results import WebAgentResult
from .runner import (
    WebAgentClientFactory,
    WebAgentRunner,
    WebAgentRunnerCallbacks,
    WebAgentRunnerComponents,
    WebAgentRunnerRunSpec,
)


class WebAgentFacade(SessionScopedAgentFacade[WebArtifactStore]):
    """High-level wrapper around the generic web-agent runner."""

    def __init__(
        self,
        *,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        session_manager: SessionManager,
        execution_transport: NorthCUAWebSocketTransport,
        artifact_store: WebArtifactStore,
        user_id: str,
        transport_capabilities: AgentTransportCapabilities | None = None,
    ) -> None:
        super().__init__(
            app_config=app_config,
            agent_config=agent_config,
            session_manager=session_manager,
            execution_transport=execution_transport,
            artifact_store=artifact_store,
            user_id=user_id,
            transport_capabilities=transport_capabilities,
        )

    async def run_web(
        self,
        *,
        user_query: str,
        url: str = "",
        headed: bool | None = None,
        max_steps: int | None = None,
        max_session_steps: int = 0,
        tool_choice_policy: str = "auto",
        allowed_tool_names: tuple[str, ...] = (),
        client_factory: WebAgentClientFactory | None = None,
        external_tool_device_executor: AgentKitCommandExecutor | None = None,
        evaluate: bool | None = None,
        on_finalize: Callable[[WebAgentResult], Awaitable[None]] | None = None,
        on_before_step: Callable[[int], None] | None = None,
        on_step: Callable[[WebStepPanelRecord], None] | None = None,
    ) -> WebAgentResult:
        """Run one generic web-agent task through a configured runner.

        Args:
            user_query: User task to complete in the browser.
            url: Optional URL to open before the first observation.
            headed: Optional per-run override for showing the browser window.
            max_steps: Optional per-round step limit override.
            max_session_steps: Optional cumulative session step cap.
            allowed_tool_names: Exact model-visible web tool allowlist.
            client_factory: Optional browser client factory for tests or remote
                substrates.
            external_tool_device_executor: Optional MCP command executor used
                by remote runs to place model-visible web actions on the device.
            on_before_step: Optional terminal callback before each step.
            on_step: Optional terminal callback after each step.

        Returns:
            Full structured web-agent result with step records and artifacts.
        """

        runner = WebAgentRunner(
            WebAgentRunnerComponents(
                app_config=self.app_config,
                agent_config=self.agent_config,
                artifact_store=self.artifact_store,
                execution_transport=self.execution_transport,
                session_manager=self.session_manager,
                user_id=self.user_id,
                transport_capabilities=self.transport_capabilities,
                client_factory=client_factory,
                external_tool_device_executor=external_tool_device_executor,
            )
        )

        async def finalize(result: WebAgentResult) -> None:
            if on_finalize is not None:
                await on_finalize(result)
                return
            from .evaluation.lifecycle import evaluate_active_session

            await evaluate_active_session(self, runner=runner, result=result, enabled=evaluate)

        return await runner.run(
            WebAgentRunnerRunSpec(
                user_query=user_query,
                url=url,
                headed=headed,
                max_steps=max_steps,
                max_session_steps=max_session_steps,
                tool_choice_policy=tool_choice_policy,
                allowed_tool_names=allowed_tool_names,
            ),
            callbacks=WebAgentRunnerCallbacks(on_before_step=on_before_step, on_step=on_step, on_finalize=finalize),
        )

    async def run_query(
        self,
        request: object,
        **kwargs: object,
    ) -> WebRunResult:
        """Run one typed web-agent request inside this session-scoped facade.

        Args:
            request: Typed web run request supplied by local or websocket
                callers.
            **kwargs: Optional callbacks and browser client factory used by
                CLI/tests.

        Returns:
            Lightweight facade result used by generic CLI and server code.

        Raises:
            TypeError: If ``request`` is not a ``WebRunRequest``.
            ValueError: If the request session id does not match this facade.
        """

        if not isinstance(request, WebRunRequest):
            raise TypeError("WebAgentFacade.run_query expects a WebRunRequest.")
        on_before_step = cast(Callable[[int], None] | None, kwargs.get("on_before_step"))
        on_step = cast(Callable[[WebStepPanelRecord], None] | None, kwargs.get("on_step"))
        if request.session_id != self.session_id:
            raise ValueError(f"Web facade session mismatch: expected {self.session_id}, got {request.session_id}")
        result = await self.run_web(
            user_query=request.query,
            url=request.url,
            headed=request.headed,
            max_steps=request.max_steps or None,
            max_session_steps=request.max_session_steps,
            tool_choice_policy=str(request.metadata.get("tool_choice_policy", "auto")),
            allowed_tool_names=normalize_allowed_tool_names(request.metadata.get("allowed_tool_names")),
            client_factory=cast(WebAgentClientFactory | None, kwargs.get("client_factory")),
            external_tool_device_executor=cast(
                AgentKitCommandExecutor | None,
                kwargs.get("external_tool_device_executor"),
            ),
            evaluate=cast(bool | None, kwargs.get("evaluate")),
            on_before_step=on_before_step,
            on_step=on_step,
        )
        return WebRunResult(
            session_id=self.session_id,
            terminal_status=result.terminal_status.value,
            hit_step_limit=result.hit_step_limit,
            primary_result_path=self.artifact_store.result_path,
            terminal_decision=result.to_terminal_decision().to_json(),
            status=result.status.value,
            step_count=len(result.steps),
            final_answer=result.final_answer,
            success=result.success,
            trace_path=self.artifact_store.trace_path,
            result_path=self.artifact_store.result_path,
            tool_errors=tuple(result.tool_errors),
        )

    def _spawn_with_artifact_store(self, artifact_store: WebArtifactStore) -> "WebAgentFacade":
        """Create an isolated facade for another web-agent session."""

        return WebAgentFacade(
            app_config=self.app_config,
            agent_config=self.clone_agent_config(self.agent_config),
            session_manager=self.session_manager,
            execution_transport=self.execution_transport,
            artifact_store=artifact_store,
            user_id=self.user_id,
            transport_capabilities=self.transport_capabilities,
        )
