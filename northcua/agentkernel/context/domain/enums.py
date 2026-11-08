"""Closed context-management vocabularies shared across agents and runtimes."""

from __future__ import annotations

from enum import StrEnum


class ContextManagementMode(StrEnum):
    """Whether context management observes or changes provider input."""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class ContextSegmentKind(StrEnum):
    """Semantic role of one atomic context segment."""

    SYSTEM = "system"
    TASK = "task"
    CURRENT_OBSERVATION = "current_observation"
    USER_HISTORY = "user_history"
    ASSISTANT_TOOL_TURN = "assistant_tool_turn"
    OPEN_TOOL_EXCHANGE = "open_tool_exchange"
    CHECKPOINT = "checkpoint"
    WORKING_MEMORY = "working_memory"
    FRAMEWORK = "framework"


class RetentionClass(StrEnum):
    """Loss policy for a segment during compaction."""

    PINNED = "pinned"
    RECENT = "recent"
    COMPACTABLE = "compactable"
    OPEN_EXCHANGE = "open_exchange"


class CompactionTrigger(StrEnum):
    """Reason a context plan was evaluated or executed."""

    BELOW_THRESHOLD = "below_threshold"
    SOFT_BUDGET = "soft_budget"
    HARD_BUDGET = "hard_budget"
    PROVIDER_OVERFLOW = "provider_overflow"
    CHECKPOINT_REUSE = "checkpoint_reuse"
    SHADOW = "shadow"


class CompactionOutcome(StrEnum):
    """Terminal result of one context preparation attempt."""

    UNCHANGED = "unchanged"
    WOULD_COMPACT = "would_compact"
    CHECKPOINT_REUSED = "checkpoint_reused"
    CHECKPOINT_COMMITTED = "checkpoint_committed"
    DETERMINISTIC_TRIM = "deterministic_trim"
    SUMMARY_FAILED = "summary_failed"
    HARD_BUDGET_REJECTED = "hard_budget_rejected"


class SummaryInvocationKind(StrEnum):
    """Purpose of one internal summary model call."""

    CONTEXT_SUMMARY = "context_summary"
    CONTEXT_SUMMARY_MERGE = "context_summary_merge"
    CONTEXT_SUMMARY_REPAIR = "context_summary_repair"
    CONTEXT_EMERGENCY = "context_emergency"


class CheckpointScope(StrEnum):
    """Visibility boundary for persisted context state."""

    AGENT = "agent"
    SHARED_FACT = "shared_fact"


class CheckpointBackend(StrEnum):
    """Supported durable checkpoint implementations."""

    ARTIFACT = "artifact"


class FailurePolicy(StrEnum):
    """Configured action when summary or persistence fails."""

    CONTINUE_UNMODIFIED = "continue_unmodified"
    ATOMIC_TRIM_THEN_ERROR = "atomic_trim_then_error"
    ERROR = "error"


class SummaryModelSource(StrEnum):
    """How the summary LLM runtime is resolved."""

    INHERIT_PRIMARY = "inherit_primary"
    AGENT_CONFIG = "agent_config"


class SummaryTracePayloadMode(StrEnum):
    """Amount of summary invocation data persisted to disk."""

    FULL_REDACTED = "full_redacted"
    SUMMARY_AND_USAGE = "summary_and_usage"
    METADATA_ONLY = "metadata_only"
