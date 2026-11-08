"""Runner support contracts for the generic web agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentkit.external_tools import AgentKitCommandExecutor
from northcua.core.config import NorthCUAAppConfig
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from ..artifacts import WebArtifactStore
from ..backend import WebBackendFactory
from ..domain.panel_models import WebStepPanelRecord
from ..domain.results import WebAgentResult


@dataclass(slots=True)
class WebAgentRunnerComponents:
    """Shared runtime dependencies required by the web-agent runner.

    Attributes:
        app_config: Resolved NorthCUA application config.
        agent_config: Model/agent configuration.
        artifact_store: Session-scoped artifact store.
        execution_transport: Transport used for model/tool execution.
        session_manager: NexAU session manager shared by the app.
        user_id: User id used for model session scoping.
        transport_capabilities: Local/remote tool execution capabilities.
        client_factory: Optional browser client factory override.
        external_tool_device_executor: Optional executor for model-visible web
            actions. Remote runners use this to execute WebAgentActionExecutor
            inside the device process that owns the browser.
    """

    app_config: NorthCUAAppConfig
    agent_config: AgentConfig
    artifact_store: WebArtifactStore
    execution_transport: NorthCUAWebSocketTransport
    session_manager: SessionManager
    user_id: str
    transport_capabilities: AgentTransportCapabilities
    client_factory: WebBackendFactory | None = None
    external_tool_device_executor: AgentKitCommandExecutor | None = None


@dataclass(slots=True, frozen=True)
class WebAgentRunnerRunSpec:
    """User-facing web-agent run request.

    Attributes:
        user_query: User task to complete.
        url: Optional initial URL to open.
        headed: Optional per-run override for showing the browser window.
        max_steps: Optional round/query step limit override.
        max_session_steps: Optional cumulative session step limit.
        tool_choice_policy: Per-task model tool-selection policy.
        allowed_tool_names: Exact model-visible tool allowlist. An empty tuple
            preserves the default full web-agent surface.
    """

    user_query: str
    url: str = ""
    headed: bool | None = None
    max_steps: int | None = None
    max_session_steps: int = 0
    tool_choice_policy: str = "auto"
    allowed_tool_names: tuple[str, ...] = ()


@dataclass(slots=True)
class WebAgentRunnerCallbacks:
    """Optional callbacks for CLI/server rendering.

    Attributes:
        on_before_step: Callback invoked before each model step.
        on_step: Callback invoked after a step panel record is available.
    """

    on_before_step: Callable[[int], None] | None = None
    on_step: Callable[[WebStepPanelRecord], None] | None = None
    on_finalize: Callable[[WebAgentResult], Awaitable[None]] | None = None
