"""Shared runtime scaffolding for internal NorthCUA agents.

Exports are lazy so CLI parser construction can import lightweight adapter
contracts without requiring NexAU-backed application classes.
"""

from __future__ import annotations

from importlib import import_module

_EXPORT_MODULES = {
    "AgentApplicationBase": "northcua.agentinfra.runtime.application",
    "AgentArtifactStoreBase": "northcua.agentinfra.runtime.artifacts",
    "AgentCliAdapterBase": "northcua.agentinfra.runtime.cli",
    "AgentKind": "northcua.agentinfra.runtime.protocol",
    "AgentRunRequest": "northcua.agentinfra.runtime.protocol",
    "AgentRunResult": "northcua.agentinfra.runtime.protocol",
    "AgentRunnerBase": "northcua.agentinfra.runtime.runner",
    "AgentRuntimeContext": "northcua.agentinfra.runtime.context",
    "AgentRuntimeDescriptor": "northcua.agentinfra.runtime.protocol",
    "AgentServerBase": "northcua.agentinfra.runtime.server",
    "AgentStepEvent": "northcua.agentinfra.runtime.protocol",
    "AgentTerminalEvent": "northcua.agentinfra.runtime.protocol",
    "AgentTransportCapabilities": "northcua.agentinfra.runtime.protocol",
    "ArtifactStoreFactory": "northcua.agentinfra.runtime.artifacts",
    "BrowserArtifactStoreBase": "northcua.agentinfra.runtime.artifacts",
    "BrowserStateProvider": "northcua.agentinfra.runtime.contracts",
    "BrowserStepEvent": "northcua.agentinfra.runtime.protocol",
    "DesktopRunRequest": "northcua.agentinfra.runtime.protocol",
    "DesktopRunResult": "northcua.agentinfra.runtime.protocol",
    "DesktopStepEvent": "northcua.agentinfra.runtime.protocol",
    "SessionScopedAgentFacade": "northcua.agentinfra.runtime.facade",
    "StepCallbackSet": "northcua.agentinfra.runtime.runner",
    "WebRunRequest": "northcua.agentinfra.runtime.protocol",
    "WebRunResult": "northcua.agentinfra.runtime.protocol",
    "WebStepEvent": "northcua.agentinfra.runtime.protocol",
}


def __getattr__(name: str) -> object:
    """Resolve compatibility exports without eager runtime imports."""

    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name), name)


__all__ = sorted(_EXPORT_MODULES)
