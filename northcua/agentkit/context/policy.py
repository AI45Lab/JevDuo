"""Default context policy for AgentKit and subagent plugins."""

from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy

GENERIC_CONTEXT_SUMMARY_POLICY = ContextSummaryPolicy(
    profile="generic",
    rules=(
        "Keep assigned scope, dependencies, deliverables, provenance and unresolved work.",
        "Do not expose one agent's raw transcript or checkpoint to another logical agent.",
    ),
)
