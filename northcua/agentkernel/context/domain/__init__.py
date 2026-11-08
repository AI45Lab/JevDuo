"""Typed domain contracts for NorthCUA context management."""

from .config import ContextManagementConfig, ResolvedContextPolicy
from .enums import (
    CheckpointScope,
    CompactionOutcome,
    CompactionTrigger,
    ContextManagementMode,
    ContextSegmentKind,
    FailurePolicy,
    RetentionClass,
    SummaryInvocationKind,
)
from .models import (
    AgentKindKey,
    ContextBudget,
    ContextCheckpoint,
    ContextCompactionEvent,
    ContextOwnerKey,
    ContextSourceRef,
    ContextSummaryDocumentV1,
    ContextTokenEstimate,
    ModelInvocationTraceEnvelope,
    SummaryCompressionContract,
    SummaryInvocationRequest,
    SummaryInvocationResult,
    SummaryItem,
    SummarySourceSegment,
)
from .presentation import ContextPresentationNotice, ContextPresentationNoticeCatalog
from .runtime import ContextPreparationRequest, ContextPreparationResult, ContextSegment

__all__ = [
    "AgentKindKey",
    "CheckpointScope",
    "CompactionOutcome",
    "CompactionTrigger",
    "ContextBudget",
    "ContextCheckpoint",
    "ContextCompactionEvent",
    "ContextManagementConfig",
    "ContextManagementMode",
    "ContextOwnerKey",
    "ContextPresentationNotice",
    "ContextPresentationNoticeCatalog",
    "ContextPreparationRequest",
    "ContextPreparationResult",
    "ContextSegment",
    "ContextSegmentKind",
    "ContextSourceRef",
    "ContextSummaryDocumentV1",
    "ContextTokenEstimate",
    "FailurePolicy",
    "ModelInvocationTraceEnvelope",
    "ResolvedContextPolicy",
    "RetentionClass",
    "SummaryInvocationKind",
    "SummaryCompressionContract",
    "SummaryInvocationRequest",
    "SummaryInvocationResult",
    "SummaryItem",
    "SummarySourceSegment",
]
