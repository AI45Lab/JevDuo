"""Typed runtime contracts for AgentKit evaluator orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nexau.archs.tool.tool import Tool
from nexau.core.messages import Message

from northcua.agentkernel.json_types import JsonObject
from northcua.core.terminal import AgentTerminalDecision

from ..protocol import (
    EvaluationEvidenceManifest,
    RoundEvaluationBundle,
    RoundEvaluationRecord,
)
from ..routing import PlacementResolution
from ..tools import FinalReportState


@dataclass(slots=True, frozen=True)
class EvaluatorToolset:
    """Model-facing evaluator tools and their finalizer state.

    Attributes:
        tools: NexAU tool objects exposed to the evaluator model.
        placement: Placement partition used to build the tools.
        final_report_state: Mutable state captured by the finalizer tool.
    """

    tools: tuple[Tool, ...]
    placement: PlacementResolution
    final_report_state: FinalReportState


@dataclass(slots=True, frozen=True)
class EvaluatorRunContext:
    """Typed inputs needed to prepare one evaluator model loop.

    Attributes:
        bundle: Canonical evaluation bundle.
        evidence_manifest: Selected evidence visible to the model.
        artifact_root: Session artifact root for relative path resolution.
        step_trace_by_session_step: Persisted step traces keyed by session step.
        remote: Whether the evaluated device is remote.
    """

    bundle: RoundEvaluationBundle
    evidence_manifest: EvaluationEvidenceManifest
    artifact_root: Path
    step_trace_by_session_step: dict[int, JsonObject]
    remote: bool = False


@dataclass(slots=True, frozen=True)
class EvaluatorPromptEnvelope:
    """Prompt envelope passed through the shared AgentKit model executor.

    Attributes:
        messages: NexAU messages sent to the evaluator model.
        provider_tools: Provider-specific tool schemas resolved for the model.
        tool_choice: Provider-specific tool-choice policy.
    """

    messages: list[Message]
    provider_tools: list[JsonObject]
    tool_choice: JsonObject | str | None


@dataclass(slots=True, frozen=True)
class EvaluatorRunResult:
    """Result returned by a completed low-code evaluator run.

    Attributes:
        record: Persistable round evaluation record.
        response_text: Final assistant text returned by the model loop.
        toolset: Toolset used for this evaluator run.
        terminal_decision: Shared terminal decision for evaluator runtime
            completion. This describes evaluator execution, not the judged
            main-agent verdict.
    """

    record: RoundEvaluationRecord
    response_text: str
    toolset: EvaluatorToolset
    terminal_decision: AgentTerminalDecision


__all__ = [
    "EvaluatorPromptEnvelope",
    "EvaluatorRunContext",
    "EvaluatorRunResult",
    "EvaluatorToolset",
]
