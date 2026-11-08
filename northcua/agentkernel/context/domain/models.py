"""Serializable context-management domain models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from northcua.agentkernel.json_types import JsonArray, JsonObject

from .enums import (
    CheckpointScope,
    CompactionOutcome,
    CompactionTrigger,
    ContextSegmentKind,
    SummaryInvocationKind,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentKindKey(RootModel[str]):
    """Validated open key used by built-in and plugin-defined agents."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("agent kind must not be empty")
        return normalized


class ContextOwnerKey(_FrozenModel):
    """Stable isolation key for one logical agent's durable checkpoint."""

    user_id: str
    session_id: str
    task_epoch: str
    logical_agent_path: str

    def storage_key(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ContextSourceRef(_FrozenModel):
    """Stable provenance cursor for one source segment."""

    sequence: int = Field(ge=0)
    round_num: int = Field(default=0, ge=0)
    step: int = Field(default=0, ge=0)
    stable_digest: str = Field(min_length=16)


class SummaryItem(_FrozenModel):
    """One factual summary item with optional source provenance."""

    text: str = Field(min_length=1, max_length=8192)
    source_segment_ids: tuple[str, ...] = ()


class ContextSummaryDocumentV1(_FrozenModel):
    """Versioned, validated summary checkpoint payload."""

    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    goal: str = Field(default="", max_length=16384)
    constraints: tuple[SummaryItem, ...] = ()
    verified_facts: tuple[SummaryItem, ...] = ()
    decisions: tuple[SummaryItem, ...] = ()
    completed: tuple[SummaryItem, ...] = ()
    failures: tuple[SummaryItem, ...] = ()
    current_state: tuple[SummaryItem, ...] = ()
    open_items: tuple[SummaryItem, ...] = ()
    artifacts: tuple[SummaryItem, ...] = ()

    def canonical_payload(self) -> JsonObject:
        """Return the stable compact IR rendered into provider history.

        Top-level fields remain explicit for the versioned checkpoint contract.
        Empty per-item provenance is optional and omitted so validation cannot
        inflate a model response merely by materializing Pydantic defaults.
        """

        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "constraints": self._canonical_items(self.constraints),
            "verified_facts": self._canonical_items(self.verified_facts),
            "decisions": self._canonical_items(self.decisions),
            "completed": self._canonical_items(self.completed),
            "failures": self._canonical_items(self.failures),
            "current_state": self._canonical_items(self.current_state),
            "open_items": self._canonical_items(self.open_items),
            "artifacts": self._canonical_items(self.artifacts),
        }

    @staticmethod
    def _canonical_items(items: tuple[SummaryItem, ...]) -> JsonArray:
        payload: JsonArray = []
        for item in items:
            encoded: JsonObject = {"text": item.text}
            if item.source_segment_ids:
                encoded["source_segment_ids"] = list(item.source_segment_ids)
            payload.append(encoded)
        return payload

    def canonical_prompt_text(self) -> str:
        """Render stable prompt content without timestamps or random identifiers."""

        return (
            "<northcua_context_checkpoint>\n"
            + json.dumps(
                self.canonical_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n</northcua_context_checkpoint>"
        )


class SummaryCompressionContract(_FrozenModel):
    """Provider-neutral measurable output contract for one summary invocation."""

    counting_unit: Literal["unicode_characters"] = "unicode_characters"
    source_canonical_characters: int = Field(ge=0)
    retained_baseline_characters: int = Field(ge=0)
    new_source_characters: int = Field(ge=0)
    minimum_reduction_ratio: float = Field(gt=0, lt=1)
    # Local source/partial reduction ceiling before any final post-processing.
    maximum_canonical_characters: int = Field(ge=0)
    # End-to-end ceiling measured against the original invocation source.
    global_final_ceiling_characters: int = Field(ge=0)
    reserved_output_characters: int = Field(default=0, ge=0)
    # min(local ceiling, global final ceiling - deterministic reserve).
    maximum_model_canonical_characters: int = Field(ge=0)
    maximum_json_characters: int = Field(ge=0)


class ContextBudget(_FrozenModel):
    """Resolved per-request input budget."""

    context_limit: int = Field(gt=0)
    output_reserve: int = Field(ge=0)
    safety_margin: int = Field(ge=0)
    hard_input_budget: int = Field(gt=0)
    soft_trigger_tokens: int = Field(gt=0)
    target_tokens: int = Field(gt=0)


class ContextTokenEstimate(_FrozenModel):
    """Conservative token estimate including messages and tool schema."""

    message_tokens: int = Field(ge=0)
    tool_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimator: str


class SummarySourceSegment(_FrozenModel):
    """Sanitized, bounded source data sent to the summary model."""

    segment_id: str
    kind: ContextSegmentKind
    text: str
    source_refs: tuple[ContextSourceRef, ...] = ()


class SummaryInvocationRequest(_FrozenModel):
    """Provider-neutral request passed to a summary client."""

    invocation_id: str
    compaction_event_id: str
    checkpoint_id: str
    owner: ContextOwnerKey
    invocation_kind: SummaryInvocationKind = SummaryInvocationKind.CONTEXT_SUMMARY
    policy_profile: str
    prompt_fingerprint: str
    source_digest: str
    previous_summary: ContextSummaryDocumentV1 | None = None
    segments: tuple[SummarySourceSegment, ...]
    max_output_tokens: int = Field(gt=0)
    max_calls: int = Field(default=0, ge=0)


class SummaryInvocationCallRecord(_FrozenModel):
    """One physical provider call within chunk/repair/merge orchestration."""

    call_index: int = Field(ge=1)
    purpose: str
    request: JsonObject = Field(default_factory=dict)
    response: JsonObject = Field(default_factory=dict)
    usage: JsonObject = Field(default_factory=dict)
    latency_ms: int = Field(default=0, ge=0)
    status: str
    error_type: str = ""
    error_message: str = ""


class SummaryInvocationResult(_FrozenModel):
    """Validated summary response plus normalized trace data."""

    invocation_id: str
    document: ContextSummaryDocumentV1
    model: str
    provider: str
    usage: JsonObject = Field(default_factory=dict)
    request_payload: JsonObject = Field(default_factory=dict)
    response_payload: JsonObject = Field(default_factory=dict)
    latency_ms: int = Field(default=0, ge=0)
    call_records: tuple[SummaryInvocationCallRecord, ...] = ()


class ContextCoverageEntry(_FrozenModel):
    """Representation-independent identity for one checkpoint-covered segment."""

    segment_id: str
    stable_digest: str = Field(min_length=16)


class ContextCheckpoint(_FrozenModel):
    """Durable source of truth for one agent's compacted context."""

    checkpoint_id: str
    scope: CheckpointScope = CheckpointScope.AGENT
    owner: ContextOwnerKey
    schema_version: int = Field(default=1, ge=1)
    policy_version: str
    policy_fingerprint: str
    prompt_fingerprint: str
    model_fingerprint: str
    revision: int = Field(ge=1)
    epoch: int = Field(ge=1)
    covered_segment_ids: tuple[str, ...]
    covered_sources: tuple[ContextCoverageEntry, ...] = ()
    source_digest: str
    summary: ContextSummaryDocumentV1
    rendered_token_count: int = Field(default=0, ge=0)
    summary_trace_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TokenUsageRecord(_FrozenModel):
    """Provider-neutral token accounting for summary traces."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ModelInvocationTraceEnvelope(_FrozenModel):
    """Full-fidelity trace envelope for internal context model calls."""

    trace_id: str
    invocation_kind: SummaryInvocationKind
    compaction_event_id: str
    checkpoint_id: str
    parent_main_invocation_id: str = ""
    owner: ContextOwnerKey
    source_digest: str
    model: str
    provider: str
    prompt_fingerprint: str
    request: JsonObject = Field(default_factory=dict)
    response: JsonObject = Field(default_factory=dict)
    usage: TokenUsageRecord = Field(default_factory=TokenUsageRecord)
    latency_ms: int = Field(default=0, ge=0)
    status: str
    error_type: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextCompactionEvent(_FrozenModel):
    """Audit record for planning, summary and checkpoint lifecycle."""

    event_id: str
    logical_step_id: str
    owner: ContextOwnerKey
    trigger: CompactionTrigger
    outcome: CompactionOutcome
    policy_fingerprint: str
    source_digest: str = ""
    checkpoint_id: str = ""
    checkpoint_epoch: int = Field(default=0, ge=0)
    summary_trace_id: str = ""
    before_tokens: int = Field(default=0, ge=0)
    after_tokens: int = Field(default=0, ge=0)
    hard_input_budget: int = Field(default=0, ge=0)
    covered_segment_count: int = Field(default=0, ge=0)
    status: str
    error_type: str = ""
    error_message: str = ""
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
