"""Agent-owned registration hook for generic web-agent MCP servers."""

from __future__ import annotations

from northcua.runtime.mcp.agent_extensions import register_agent_mcp_servers

_WEB_AGENT_SERVER_NAMES = frozenset({"WebAgentProbeExecutor", "WebAgentActionExecutor", "WebAgentBackendExecutor"})
WEB_AGENT_MCP_REGISTRATION_HOOK = "northcua.agents.web.mcp.registration:ensure_web_agent_mcp_servers_registered"


def ensure_web_agent_mcp_servers_registered() -> None:
    """Import web-agent MCP modules so registry decorators run."""

    from . import action_server as _web_agent_actions  # noqa: F401
    from . import backend_server as _web_agent_backend  # noqa: F401
    from . import probe_server as _web_agent_probes  # noqa: F401


def register_web_agent_mcp_extension_hooks() -> None:
    """Register web-agent server names with the neutral MCP hook registry."""

    register_agent_mcp_servers(_WEB_AGENT_SERVER_NAMES, ensure_web_agent_mcp_servers_registered)
