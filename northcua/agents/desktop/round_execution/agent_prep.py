"""Agent-config preparation helpers for desktop round execution."""

from __future__ import annotations

from nexau.archs.main_sub.config import AgentConfig

from northcua.agentinfra.providers.llm_fallback import LLMFallbackCandidate, llm_fallback_candidates
from northcua.agentkit.agent_config import clone_agent_config_with_hooks


class RoundAgentPreparer:
    """Prepare a per-round agent config without mutating the shared base config."""

    def __init__(self, *, agent_config: AgentConfig) -> None:
        self._agent_config = agent_config

    def prepare_agent_config(
        self,
        *,
        before_model_hooks: list[object],
        after_model_hooks: list[object],
        after_tool_hooks: list[object],
        before_tool_hooks: list[object] | None = None,
    ) -> AgentConfig:
        """Clone agent config while preserving stateful NexAU objects.

        Args:
            before_model_hooks: Hooks that capture the provider request before
                the model call for this round.
            after_model_hooks: Hooks that capture model responses for this
                round.
            after_tool_hooks: Hooks that capture tool execution details for
                this round.

        Returns:
            Per-round agent config with trace hooks appended and shared NexAU
            runtime objects preserved by reference.
        """

        return clone_agent_config_with_hooks(
            self._agent_config,
            before_model_hooks=before_model_hooks,
            after_model_hooks=after_model_hooks,
            after_tool_hooks=after_tool_hooks,
            before_tool_hooks=before_tool_hooks,
        )

    def llm_fallback_candidates(self) -> tuple[LLMFallbackCandidate, ...]:
        """Return primary and fallback LLM candidates for this agent.

        Returns:
            Ordered primary/fallback LLM candidates derived from agent config.
        """

        return llm_fallback_candidates(self._agent_config)

    @property
    def base_agent_config(self) -> AgentConfig:
        """Return the immutable-by-convention source used for budget resolution."""

        return self._agent_config
