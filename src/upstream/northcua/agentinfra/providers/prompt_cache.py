"""Prompt-cache helpers for provider-specific LLM runtime settings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Protocol

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities

_PROMPT_CACHE_KEY_PREFIX = "northcua"
_PROMPT_CACHE_KEY_VERSION = 1


class _AgentConfigLike(Protocol):
    @property
    def name(self) -> str:
        """Agent name."""
        ...

    @property
    def llm_config(self) -> Any | None:
        """Provider configuration."""
        ...

    @property
    def tool_call_mode(self) -> object:
        """Runtime tool-call mode."""
        ...


def ensure_session_prompt_cache_key(
    *,
    agent_config: _AgentConfigLike,
    user_id: str,
    session_id: str,
    remote: bool,
    externalize_tools: bool,
    transport_capabilities: AgentTransportCapabilities | None,
) -> str | None:
    """Install a stable OpenAI Responses prompt-cache key for one logical session.

    NexAU injects a random ``prompt_cache_key`` for every newly-created
    OpenAI Responses Agent when none is configured. NorthCUA intentionally
    rebuilds Agent objects per step, so this helper pins the key to a stable
    hash of the logical session/runtime shape before ``Agent.create`` runs.
    Explicit user-provided keys are preserved.

    Args:
        agent_config: Runtime AgentConfig that will be passed to NexAU.
        user_id: NexAU user identifier.
        session_id: Logical NorthCUA/NexAU session identifier.
        remote: Whether the runtime uses remote-device tool dispatch.
        externalize_tools: Whether tool execution is externalized.
        transport_capabilities: Feature flags for this runtime.

    Returns:
        The existing or generated key, or ``None`` for non-Responses providers.
    """

    llm_config = agent_config.llm_config
    if llm_config is None or llm_config.api_type != "openai_responses":
        return None
    existing = llm_config.get_param("prompt_cache_key")
    if existing:
        return str(existing)
    cache_key = session_prompt_cache_key(
        agent_config=agent_config,
        user_id=user_id,
        session_id=session_id,
        remote=remote,
        externalize_tools=externalize_tools,
        transport_capabilities=transport_capabilities,
    )
    llm_config.set_param("prompt_cache_key", cache_key)
    return cache_key


def session_prompt_cache_key(
    *,
    agent_config: _AgentConfigLike,
    user_id: str,
    session_id: str,
    remote: bool,
    externalize_tools: bool,
    transport_capabilities: AgentTransportCapabilities | None,
) -> str:
    """Return the deterministic prompt-cache routing key for one session.

    Args:
        agent_config: Runtime AgentConfig whose provider shape participates in the key.
        user_id: NexAU user identifier.
        session_id: Logical NorthCUA/NexAU session identifier.
        remote: Whether remote-device tool dispatch is enabled.
        externalize_tools: Whether tool execution is externalized.
        transport_capabilities: Runtime transport feature flags.

    Returns:
        Stable prompt-cache routing key for OpenAI Responses.
    """

    llm_config = agent_config.llm_config
    payload = {
        "version": _PROMPT_CACHE_KEY_VERSION,
        "user_id": user_id,
        "session_id": session_id,
        "agent_name": agent_config.name or "",
        "model": getattr(llm_config, "model", "") if llm_config is not None else "",
        "api_type": getattr(llm_config, "api_type", "") if llm_config is not None else "",
        "base_url": getattr(llm_config, "base_url", "") if llm_config is not None else "",
        "tool_call_mode": getattr(agent_config, "tool_call_mode", ""),
        "remote": remote,
        "externalize_tools": externalize_tools,
        "transport_capabilities": asdict(transport_capabilities) if transport_capabilities is not None else {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    return f"{_PROMPT_CACHE_KEY_PREFIX}:{digest}"
