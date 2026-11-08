"""Neutral registration registry for agent-owned MCP servers."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

AgentMCPRegistrationHook = Callable[[], None]

_REGISTRATION_HOOKS_BY_SERVER_NAME: dict[str, AgentMCPRegistrationHook] = {}


def register_agent_mcp_servers(server_names: set[str] | frozenset[str], hook: AgentMCPRegistrationHook) -> None:
    """Register an agent-owned MCP hook for explicit server names.

    Args:
        server_names: Server registry names owned by the agent.
        hook: Callable that imports/registers the matching MCP server modules.
    """

    for server_name in server_names:
        _REGISTRATION_HOOKS_BY_SERVER_NAME[server_name] = hook


def ensure_agent_mcp_server_registered(server_name: str) -> None:
    """Register an agent-owned MCP server only when explicitly requested.

    Args:
        server_name: Registry server name requested by a launcher or agent-owned
            runtime path.
    """

    registration_hook = agent_mcp_registration_hook(server_name)
    if registration_hook is None:
        return
    registration_hook()


def run_agent_mcp_registration_hook(raw_hook: str) -> None:
    """Run a generic MCP registration hook in ``module:function`` form.

    Args:
        raw_hook: Importable hook spec.

    Raises:
        ValueError: If the hook spec is malformed or does not resolve to a callable.
    """

    if ":" not in raw_hook:
        raise ValueError("MCP registration hook must use module:function syntax.")
    module_name, function_name = raw_hook.split(":", 1)
    hook = getattr(importlib.import_module(module_name), function_name)
    if not callable(hook):
        raise ValueError(f"MCP registration hook is not callable: {raw_hook}")
    hook()


def agent_mcp_registration_hook(server_name: str) -> AgentMCPRegistrationHook | None:
    """Return the lazy registration hook for one agent-owned MCP server.

    Args:
        server_name: Registry server name to resolve.

    Returns:
        Callable registration hook, or None when no agent hook owns the name.
    """

    return _REGISTRATION_HOOKS_BY_SERVER_NAME.get(server_name)
