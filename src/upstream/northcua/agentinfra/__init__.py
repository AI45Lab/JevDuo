"""Shared internal runtime infrastructure for NorthCUA agents.

The package root intentionally uses lazy exports. Controller and submitter
commands need the lightweight CLI/runtime contracts without importing NexAU,
provider adapters, or concrete local agents during ``northcua --help``.
"""

from __future__ import annotations

from importlib import import_module

_RUNTIME_EXPORTS = {
    "AgentApplicationBase",
    "AgentArtifactStoreBase",
    "AgentCliAdapterBase",
    "AgentKind",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRunnerBase",
    "AgentRuntimeContext",
    "AgentRuntimeDescriptor",
    "AgentServerBase",
    "AgentStepEvent",
    "AgentTerminalEvent",
    "AgentTransportCapabilities",
    "ArtifactStoreFactory",
    "BrowserArtifactStoreBase",
    "BrowserStateProvider",
    "BrowserStepEvent",
    "DesktopRunRequest",
    "DesktopRunResult",
    "DesktopStepEvent",
    "SessionScopedAgentFacade",
    "StepCallbackSet",
    "WebRunRequest",
    "WebRunResult",
    "WebStepEvent",
}
_PROVIDER_EXPORTS = {
    "LLMFallbackCandidate",
    "ProviderAPIParamsProjector",
    "ProviderCapabilityProfile",
    "ProviderFamily",
    "ProviderImageInspector",
    "ProviderImageTranscoder",
    "ProviderMessagePreparer",
    "ProviderPayloadInputBuilder",
    "StructuredToolSchemaAdapter",
    "agent_config_for_llm_candidate",
    "attach_llm_fallbacks",
    "build_payload_inputs",
    "build_provider_request_payload",
    "llm_fallback_candidates",
    "parse_llm_fallbacks",
    "provider_family_from_api_type",
    "resolve_provider_profile",
    "run_provider_probe_suite",
}
_TRACING_EXPORTS = {"JsonlTraceWriter", "LLMTraceLogWriter"}


def __getattr__(name: str) -> object:
    """Load legacy package-root exports only when they are actually used."""

    if name in _RUNTIME_EXPORTS:
        module = import_module("northcua.agentinfra.runtime")
        return getattr(module, name)
    if name in _PROVIDER_EXPORTS:
        module = import_module("northcua.agentinfra.providers")
        return getattr(module, name)
    if name in _TRACING_EXPORTS:
        module = import_module("northcua.agentinfra.tracing")
        return getattr(module, name)
    raise AttributeError(name)


__all__ = sorted(_RUNTIME_EXPORTS | _PROVIDER_EXPORTS | _TRACING_EXPORTS)
