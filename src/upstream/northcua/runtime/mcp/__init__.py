"""MCP runtime adapters for NorthCUA."""

from __future__ import annotations

from .builtin_servers import ensure_builtin_mcp_servers_registered
from .registry import MCPFactoryContext, NorthCUAMCPRegistry, RegisteredMCPServer
from .shared_bridge_plugin import register as _register_shared_bridge

_register_shared_bridge()

__all__ = [
    "MCPFactoryContext",
    "NorthCUAMCPRegistry",
    "RegisteredMCPServer",
    "ensure_builtin_mcp_servers_registered",
]
