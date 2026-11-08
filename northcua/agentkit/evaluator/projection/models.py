"""Typed model-facing evaluator projection contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from northcua.agentkernel.json_types import JsonObject


class EvaluatorDetailLevel(StrEnum):
    """How much canonical artifact detail should be exposed to the model."""

    SUMMARY = "summary"
    STANDARD = "standard"
    DEBUG = "debug"


class EvaluatorGeometryPolicy(StrEnum):
    """Geometry exposure policy for SoM/a11y/DOM projections."""

    NONE = "none"
    SELECTED_REFS = "selected_refs"
    FULL_DEBUG = "full_debug"


class EvaluatorEvidenceSummary(BaseModel):
    """Path-free summary for one evidence ref."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    id: str
    kind: str
    label: str
    summary: str = ""
    session_step: int = 0
    round_step: int = 0
    pinned: bool = False
    selection_reason: str = ""
    metadata: JsonObject = Field(default_factory=dict)


class EvaluatorToolResultView(BaseModel):
    """Compact model-facing view of one tool result."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    name: str = ""
    action: str = ""
    session_step: int = 0
    round_step: int = 0
    status: str = ""
    text: str = ""
    params: JsonObject = Field(default_factory=dict)
    result: JsonObject = Field(default_factory=dict)
    observation: JsonObject = Field(default_factory=dict)
    recovery: JsonObject = Field(default_factory=dict)
    evidence_refs: JsonObject = Field(default_factory=dict)


class EvaluatorTraceView(BaseModel):
    """Compact path-free trace view for one round."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    step_refs: list[JsonObject] = Field(default_factory=list)
    step_summaries: list[str] = Field(default_factory=list)
    tool_calls: list[JsonObject] = Field(default_factory=list)
    tool_results: list[JsonObject] = Field(default_factory=list)
    projection: JsonObject = Field(default_factory=dict)


class EvaluatorEvidenceView(BaseModel):
    """Compact model-facing view of the current evaluation bundle."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    session_id: str
    round_num: int
    round_request: str
    session_request: str
    step_refs: list[JsonObject] = Field(default_factory=list)
    step_summaries: list[str] = Field(default_factory=list)
    tool_calls: list[JsonObject] = Field(default_factory=list)
    tool_results: list[JsonObject] = Field(default_factory=list)
    selected_evidence_manifest: list[JsonObject] = Field(default_factory=list)
    evidence_counts: JsonObject = Field(default_factory=dict)
    projection: JsonObject = Field(default_factory=dict)


__all__ = [
    "EvaluatorDetailLevel",
    "EvaluatorEvidenceSummary",
    "EvaluatorEvidenceView",
    "EvaluatorGeometryPolicy",
    "EvaluatorToolResultView",
    "EvaluatorTraceView",
]
