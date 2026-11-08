"""Registry assembling agent-owned policies for the shared runtime."""

from __future__ import annotations

from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy
from northcua.agentkit.context import GENERIC_CONTEXT_SUMMARY_POLICY
from northcua.agentkit.evaluator.context import EVALUATOR_CONTEXT_SUMMARY_POLICY
from northcua.agents.desktop.context import DESKTOP_CONTEXT_SUMMARY_POLICY
from northcua.agents.web.context import WEB_CONTEXT_SUMMARY_POLICY


class ContextPolicyRegistry:
    """Resolve a typed profile while retaining an extensible open key."""

    def __init__(self, policies: tuple[ContextSummaryPolicy, ...]) -> None:
        self._policies = {policy.profile: policy for policy in policies}

    def resolve(self, profile: str) -> ContextSummaryPolicy:
        try:
            return self._policies[profile]
        except KeyError as exc:
            raise ValueError(f"unknown context summary policy profile: {profile}") from exc

    def register(self, policy: ContextSummaryPolicy) -> None:
        """Register an explicit plugin profile before session bootstrap."""

        if not policy.profile.strip():
            raise ValueError("context summary policy profile must not be empty")
        self._policies[policy.profile] = policy


BUILTIN_CONTEXT_POLICY_REGISTRY = ContextPolicyRegistry(
    (
        GENERIC_CONTEXT_SUMMARY_POLICY,
        DESKTOP_CONTEXT_SUMMARY_POLICY,
        WEB_CONTEXT_SUMMARY_POLICY,
        EVALUATOR_CONTEXT_SUMMARY_POLICY,
    )
)
