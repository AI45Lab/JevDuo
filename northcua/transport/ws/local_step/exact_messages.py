"""Exact-message local round runner."""

from __future__ import annotations

import logging

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.context_value import ContextValue
from nexau.core.messages import Message

from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject

from ..message_seed import message_image_count, sync_runtime_agent_structured_tools
from .models import PrepareAgent, RunAgentOnce

logger = logging.getLogger(__name__)


class ExactMessageRoundRunner:
    """Run NexAU from an exact NorthCUA-authored message history."""

    def __init__(self, *, prepare_agent: PrepareAgent, run_agent_once: RunAgentOnce) -> None:
        """Initialize exact-message execution callbacks."""

        self._prepare_agent = prepare_agent
        self._run_agent_once = run_agent_once

    async def run(
        self,
        *,
        messages: list[Message],
        user_id: str,
        session_id: str,
        agent_config: AgentConfig,
        agent: Agent | None = None,
        context: JsonObject | None = None,
        variables: ContextValue | None = None,
    ) -> tuple[str | tuple[str, JsonObject], Agent]:
        """Run one round from an exact NorthCUA-authored message list.

        Args:
            messages: Exact message sequence to seed into agent history.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Runtime agent config.
            agent: Optional prepared runtime agent to reuse.
            context: Optional runtime context passed through to NexAU.
            variables: Optional NexAU context variables.

        Returns:
            Agent result and the runtime agent used for execution.
        """

        runtime_agent = agent
        if runtime_agent is None:
            runtime_agent = await self._prepare_agent(
                user_id=user_id,
                session_id=session_id,
                agent_config=agent_config,
            )
        sync_runtime_agent_structured_tools(runtime_agent)
        # Exact-message seeding is deliberately all-or-nothing: preserve the
        # caller-authored ordering and contents, then ask NexAU to continue with
        # an empty user message so it does not append another user turn.
        runtime_agent.history = messages
        log_runtime_event(
            logger,
            "transport_exact_messages_seeded",
            level=logging.DEBUG,
            user_id=user_id,
            session_id=session_id,
            message_count=len(messages),
            image_count=message_image_count(messages),
            message_roles=[message.role.value for message in messages],
        )
        result, _ = await self._run_agent_once(
            message=[],
            user_id=user_id,
            session_id=session_id,
            agent=runtime_agent,
            context=context,
            variables=variables,
        )
        return result, runtime_agent


__all__ = ["ExactMessageRoundRunner"]
