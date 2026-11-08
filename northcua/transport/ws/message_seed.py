"""Shared helpers for exact-message seeding on websocket transport paths."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, cast

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message, Role, TextBlock

from northcua.agentkernel.tools.surface import active_provider_tool_payload
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject

logger = logging.getLogger(__name__)

SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY = "__northcua_suppress_raw_agent_mcp_servers__"


def message_image_count(messages: Sequence[Message]) -> int:
    """Return the total image block count across one message list.

    Args:
        messages: Messages whose top-level and nested blocks should be scanned.

    Returns:
        Number of image blocks found.
    """

    count = 0
    for message in messages:
        for block in message.content:
            if getattr(block, "type", None) == "image":
                count += 1
            nested = getattr(block, "content", None)
            if nested:
                count += sum(1 for item in nested if getattr(item, "type", None) == "image")
    return count


def sync_runtime_agent_structured_tools(agent: Agent) -> None:
    """Keep executor structured tools aligned with the live agent surface.

    Args:
        agent: Runtime NexAU agent whose executor may need tool synchronization.
    """

    executor = getattr(agent, "executor", None)
    update_structured_tools = getattr(executor, "update_structured_tools", None)
    if not callable(update_structured_tools):
        return
    tool_call_payload = active_provider_tool_payload(agent)
    if tool_call_payload is None:
        tool_call_payload = getattr(agent, "tool_call_payload", None)
    if tool_call_payload is None:
        return
    try:
        update_structured_tools(list(tool_call_payload))
    except Exception as exc:  # pragma: no cover - defensive logging path
        log_runtime_event(
            logger,
            "transport_sync_structured_tools_failed",
            level=logging.WARNING,
            agent_name=getattr(agent, "agent_name", ""),
            session_id=getattr(agent, "_session_id", ""),
            error=str(exc),
        )


def prepend_runtime_system_messages(
    *,
    agent: Agent,
    messages: list[Message],
    merged_context: JsonObject,
) -> list[Message]:
    """Mirror NexAU system-prompt building on stepwise local paths.

    Args:
        agent: Runtime NexAU agent.
        messages: Exact messages prepared by NorthCUA.
        merged_context: Runtime context passed to the prompt builder.

    Returns:
        Original messages or messages prefixed with generated system prompts.
    """

    if any(message.role == Role.SYSTEM for message in messages):
        return messages
    prompt_builder = getattr(agent, "prompt_builder", None)
    build_system_prompt = getattr(prompt_builder, "build_system_prompt", None)
    if not callable(build_system_prompt):
        return messages
    tool_registry = getattr(agent, "_tool_registry", None)
    compute_eager_tools = getattr(tool_registry, "compute_eager_tools", None)
    tools = compute_eager_tools() if callable(compute_eager_tools) else []
    system_prompt_parts = cast(
        Sequence[Any],
        build_system_prompt(
            agent_config=agent.config,
            tools=tools,
            sub_agents=agent.config.sub_agents or {},
            runtime_context=merged_context,
            include_tool_instructions=not getattr(agent, "use_structured_tool_calls", False),
        ),
    )
    system_messages = [
        Message(
            role=Role.SYSTEM,
            content=[TextBlock(text=part.text)],
            metadata={"cache": part.cache},
        )
        for part in system_prompt_parts
    ]
    # Preserve the NexAU system-prompt prepend invariant: generated prompts must
    # be before every caller-authored message, never appended or interleaved.
    return [*system_messages, *messages]


def supports_stepwise_local_execution(agent: Agent | object) -> bool:
    """Return whether the live agent exposes stepwise execution hooks.

    Args:
        agent: Candidate runtime agent object.

    Returns:
        True when the agent executor exposes one-step model hooks.
    """

    executor = getattr(agent, "executor", None)
    return executor is not None and hasattr(executor, "llm_caller") and hasattr(executor, "response_parser")


def visible_runtime_mcp_servers(
    *,
    raw_agent_mcp_servers: list[JsonObject],
    agent_config: AgentConfig,
    runtime_visibility_filter: Callable[[list[JsonObject]], list[JsonObject]],
) -> list[JsonObject]:
    """Return model-visible raw MCP server configs for runtime transport.

    Args:
        raw_agent_mcp_servers: Optional raw app-level MCP server configs.
        agent_config: Agent config used when raw server configs are absent.
        runtime_visibility_filter: Filtering function for model visibility.

    Returns:
        Filtered model-visible MCP server configs.
    """

    if (agent_config.global_storage or {}).get(SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY) is True:
        return runtime_visibility_filter(list(agent_config.mcp_servers))
    if raw_agent_mcp_servers:
        # Prefer raw config when present so exact app-authored MCP fields survive
        # until the visibility filter decides what the model may see.
        return runtime_visibility_filter(list(raw_agent_mcp_servers))
    return runtime_visibility_filter(list(agent_config.mcp_servers))
