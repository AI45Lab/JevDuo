"""Desktop-specific long-term context rules."""

from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy

DESKTOP_CONTEXT_SUMMARY_POLICY = ContextSummaryPolicy(
    profile="desktop",
    rules=(
        "Keep semantic application, file and task state plus verified outcomes.",
        "Omit routine click, wait and focus actions unless they establish a verified semantic state change.",
        "Observation capture mechanics are not durable facts; retain only semantic state verified by their content.",
        "Never preserve old coordinates, window geometry or overlay markers as current executable state.",
        "The current image may be in the current USER observation or the newest GUI tool result; never infer its location.",
    ),
)
