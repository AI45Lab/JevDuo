"""In-memory generic context preparation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from northcua.agentkernel.json_types import JsonObject

from .config import ResolvedContextPolicy
from .enums import CompactionOutcome, ContextSegmentKind, RetentionClass
from .models import ContextBudget, ContextCheckpoint, ContextOwnerKey, ContextSourceRef, ContextTokenEstimate

MessageT = TypeVar("MessageT")
ToolT = TypeVar("ToolT")


@dataclass(frozen=True, slots=True)
class ContextSegment(Generic[MessageT]):
    """Atomic runtime segment retaining typed provider messages by reference."""

    segment_id: str
    kind: ContextSegmentKind
    retention: RetentionClass
    source_refs: tuple[ContextSourceRef, ...]
    messages: tuple[MessageT, ...]
    summary_text: str
    stable_digest: str
    checkpoint_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextPreparationRequest(Generic[MessageT, ToolT]):
    """All inputs required to prepare one logical model step."""

    logical_step_id: str
    owner: ContextOwnerKey
    agent_kind: str
    policy: ResolvedContextPolicy
    original_messages: tuple[MessageT, ...]
    segments: tuple[ContextSegment[MessageT], ...]
    tools: tuple[ToolT, ...]
    context_limit: int
    output_reserve: int
    policy_fingerprint: str
    prompt_fingerprint: str
    model_fingerprint: str
    parent_main_invocation_id: str = ""
    metadata: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class ContextPreparationResult(Generic[MessageT]):
    """Effective messages and audit state returned by the coordinator."""

    messages: tuple[MessageT, ...]
    outcome: CompactionOutcome
    budget: ContextBudget | None = None
    estimate_before: ContextTokenEstimate | None = None
    estimate_after: ContextTokenEstimate | None = None
    checkpoint: ContextCheckpoint | None = None
    removed_segment_ids: tuple[str, ...] = ()
