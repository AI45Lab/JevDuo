"""AgentKit evaluator runtime public API.

The package root keeps protocol/spec imports lightweight. NexAU-backed runtime
and tool factories are loaded lazily so low-code declarations can import the
contracts in controller/base environments.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .artifacts import (
    ArtifactPathResolver,
    EvaluatorArtifactLayout,
    EvaluatorArtifactWriter,
    EvaluatorRoundArtifactPaths,
)
from .evidence import EvidenceSelector, RoundEvidenceAdapter
from .projection import (
    EvaluatorDetailLevel,
    EvaluatorEvidenceProjector,
    EvaluatorEvidenceSummary,
    EvaluatorEvidenceView,
    EvaluatorGeometryPolicy,
    EvaluatorToolResultView,
    EvaluatorTraceView,
)
from .prompts import DEFAULT_EVALUATOR_PROMPTS, EvaluatorPromptTemplateSet
from .protocol import (
    EvaluationCriterion,
    EvaluationCriterionScore,
    EvaluationDiagnosis,
    EvaluationEfficiencyAssessment,
    EvaluationEvidenceManifest,
    EvaluationEvidenceRef,
    EvaluationInputScope,
    EvaluationJudgeMetadata,
    EvaluationSafetyAssessment,
    EvaluationSafetyFinding,
    EvaluationSideEffect,
    EvaluationToolCheck,
    EvidenceSelectionReason,
    JudgeResponse,
    PriorRoundEvaluationSummary,
    RoundEvaluationBundle,
    RoundEvaluationRecord,
    RoundEvaluationReport,
    RoundEvaluationStepRef,
    RoundEvaluationTraceRecord,
    RubricGenerationResponse,
    SessionEvaluationRollup,
)
from .spec import EvaluatorSpec, EvaluatorToolRef, EvidencePolicy, ToolPlacementPolicy

if TYPE_CHECKING:
    from .routing import (
        EvaluationToolPlacementError,
        EvaluatorClientToolDispatcher,
        ExternalEvaluatorToolDispatcher,
        LocalEvaluatorClientToolDispatcher,
        PlacementResolution,
        RemoteEvaluatorClientToolDispatcher,
        ToolPlacementResolver,
        UnavailableEvaluatorClientToolDispatcher,
    )
    from .runtime import (
        AgentKitEvaluatorRunner,
        ArtifactEvaluationRequest,
        ArtifactEvaluationRunnerBase,
        EvaluatorPromptEnvelope,
        EvaluatorRunContext,
        EvaluatorRunResult,
        EvaluatorToolset,
    )
    from .tools import (
        ArtifactInspectionToolFactory,
        FinalReportState,
        FinalReportToolFactory,
        standard_client_gui_tools,
        standard_client_probe_tools,
    )

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentKitEvaluatorRunner": ("northcua.agentkit.evaluator.runtime", "AgentKitEvaluatorRunner"),
    "ArtifactEvaluationRequest": ("northcua.agentkit.evaluator.runtime", "ArtifactEvaluationRequest"),
    "ArtifactEvaluationRunnerBase": ("northcua.agentkit.evaluator.runtime", "ArtifactEvaluationRunnerBase"),
    "ArtifactInspectionToolFactory": ("northcua.agentkit.evaluator.tools", "ArtifactInspectionToolFactory"),
    "EvaluationToolPlacementError": ("northcua.agentkit.evaluator.routing", "EvaluationToolPlacementError"),
    "EvaluatorPromptEnvelope": ("northcua.agentkit.evaluator.runtime", "EvaluatorPromptEnvelope"),
    "EvaluatorClientToolDispatcher": ("northcua.agentkit.evaluator.routing", "EvaluatorClientToolDispatcher"),
    "EvaluatorRunContext": ("northcua.agentkit.evaluator.runtime", "EvaluatorRunContext"),
    "EvaluatorRunResult": ("northcua.agentkit.evaluator.runtime", "EvaluatorRunResult"),
    "EvaluatorToolset": ("northcua.agentkit.evaluator.runtime", "EvaluatorToolset"),
    "ExternalEvaluatorToolDispatcher": ("northcua.agentkit.evaluator.routing", "ExternalEvaluatorToolDispatcher"),
    "FinalReportState": ("northcua.agentkit.evaluator.tools", "FinalReportState"),
    "FinalReportToolFactory": ("northcua.agentkit.evaluator.tools", "FinalReportToolFactory"),
    "LocalEvaluatorClientToolDispatcher": ("northcua.agentkit.evaluator.routing", "LocalEvaluatorClientToolDispatcher"),
    "PlacementResolution": ("northcua.agentkit.evaluator.routing", "PlacementResolution"),
    "RemoteEvaluatorClientToolDispatcher": (
        "northcua.agentkit.evaluator.routing",
        "RemoteEvaluatorClientToolDispatcher",
    ),
    "UnavailableEvaluatorClientToolDispatcher": (
        "northcua.agentkit.evaluator.routing",
        "UnavailableEvaluatorClientToolDispatcher",
    ),
    "standard_client_gui_tools": ("northcua.agentkit.evaluator.tools", "standard_client_gui_tools"),
    "standard_client_probe_tools": ("northcua.agentkit.evaluator.tools", "standard_client_probe_tools"),
    "ToolPlacementResolver": ("northcua.agentkit.evaluator.routing", "ToolPlacementResolver"),
}

__all__ = [
    "AgentKitEvaluatorRunner",
    "ArtifactPathResolver",
    "ArtifactEvaluationRequest",
    "ArtifactEvaluationRunnerBase",
    "ArtifactInspectionToolFactory",
    "DEFAULT_EVALUATOR_PROMPTS",
    "EvaluationCriterion",
    "EvaluationCriterionScore",
    "EvaluationDiagnosis",
    "EvaluationEfficiencyAssessment",
    "EvaluationEvidenceManifest",
    "EvaluationEvidenceRef",
    "EvaluationInputScope",
    "EvaluationJudgeMetadata",
    "EvaluationSafetyAssessment",
    "EvaluationSafetyFinding",
    "EvaluationSideEffect",
    "EvaluationToolCheck",
    "EvaluationToolPlacementError",
    "EvaluatorClientToolDispatcher",
    "ExternalEvaluatorToolDispatcher",
    "EvaluatorArtifactLayout",
    "EvaluatorArtifactWriter",
    "EvaluatorDetailLevel",
    "EvaluatorEvidenceProjector",
    "EvaluatorEvidenceSummary",
    "EvaluatorEvidenceView",
    "EvaluatorGeometryPolicy",
    "EvaluatorPromptTemplateSet",
    "EvaluatorPromptEnvelope",
    "EvaluatorRoundArtifactPaths",
    "EvaluatorRunContext",
    "EvaluatorRunResult",
    "EvaluatorSpec",
    "EvaluatorToolRef",
    "EvaluatorToolset",
    "EvaluatorToolResultView",
    "EvaluatorTraceView",
    "EvidencePolicy",
    "EvidenceSelectionReason",
    "EvidenceSelector",
    "FinalReportState",
    "FinalReportToolFactory",
    "JudgeResponse",
    "LocalEvaluatorClientToolDispatcher",
    "PlacementResolution",
    "RemoteEvaluatorClientToolDispatcher",
    "PriorRoundEvaluationSummary",
    "RoundEvaluationBundle",
    "RoundEvaluationRecord",
    "RoundEvaluationReport",
    "RoundEvaluationStepRef",
    "RoundEvaluationTraceRecord",
    "RoundEvidenceAdapter",
    "RubricGenerationResponse",
    "SessionEvaluationRollup",
    "standard_client_gui_tools",
    "standard_client_probe_tools",
    "ToolPlacementPolicy",
    "ToolPlacementResolver",
    "UnavailableEvaluatorClientToolDispatcher",
]


def __getattr__(name: str) -> object:
    """Load NexAU-backed evaluator exports on first use."""

    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
