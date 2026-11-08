"""Evaluator-specific long-term context rules."""

from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy

EVALUATOR_CONTEXT_SUMMARY_POLICY = ContextSummaryPolicy(
    profile="evaluator",
    rules=(
        "Keep the rubric unchanged, evidence identifiers, verified findings and finalizer progress.",
        "Never convert an unverified observation into a fact or verdict.",
        "Preserve evidence provenance required to audit the final report.",
    ),
)
