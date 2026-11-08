"""MCP servers owned by the generic NorthCUA web agent."""

from .registration import (
    WEB_AGENT_MCP_REGISTRATION_HOOK,
    ensure_web_agent_mcp_servers_registered,
    register_web_agent_mcp_extension_hooks,
)

__all__ = [
    "WEB_AGENT_MCP_REGISTRATION_HOOK",
    "ensure_web_agent_mcp_servers_registered",
    "register_web_agent_mcp_extension_hooks",
]
