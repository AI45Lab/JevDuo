"""Built-in AgentKit spec aggregation."""

from __future__ import annotations

from northcua.agents.desktop.agentkit import desktop_agent_spec
from northcua.agents.web.agentkit import web_agent_spec

from .spec import AgentSpec


def builtin_agent_specs() -> tuple[AgentSpec, ...]:
    """Return the bundled AgentKit specs.

    Returns:
        Desktop and web specs in deterministic
        registration order.
    """

    return (desktop_agent_spec(), web_agent_spec())
