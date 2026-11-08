"""Shared model-agent configuration helpers for AgentKit runtimes."""

from __future__ import annotations

from typing import Any, cast

from nexau.archs.main_sub.config import AgentConfig


def clone_agent_config_with_hooks(
    agent_config: AgentConfig,
    *,
    before_model_hooks: list[object],
    after_model_hooks: list[object],
    after_tool_hooks: list[object],
    before_tool_hooks: list[object] | None = None,
) -> AgentConfig:
    """Clone a model-agent config while preserving stateful runtime objects.

    Args:
        agent_config: Source agent config to clone for one model step.
        before_model_hooks: Trace hooks invoked before provider calls.
        after_model_hooks: Trace hooks invoked after provider calls.
        after_tool_hooks: Trace hooks invoked after tool execution.

    Returns:
        Deep-copied config with runtime objects preserved and hooks appended.
    """

    saved_tracers = list(agent_config.tracers or [])
    saved_resolved_tracer = agent_config.resolved_tracer
    saved_middlewares = list(agent_config.middlewares or [])
    cloned = agent_config.model_copy(deep=True)
    cloned.tracers = saved_tracers
    cloned.resolved_tracer = saved_resolved_tracer
    cloned.middlewares = saved_middlewares
    cloned_any = cast(Any, cloned)
    cloned_any.before_model_hooks = [*(cloned.before_model_hooks or []), *before_model_hooks]
    cloned_any.after_model_hooks = [*(cloned.after_model_hooks or []), *after_model_hooks]
    cloned_any.after_tool_hooks = [*(cloned.after_tool_hooks or []), *after_tool_hooks]
    cloned_any.before_tool_hooks = [*(cloned.before_tool_hooks or []), *(before_tool_hooks or [])]
    return cloned
