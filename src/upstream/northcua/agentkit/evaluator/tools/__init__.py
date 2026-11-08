"""Factories for evaluator tools.

This package is imported by controller/base environments that do not install
the NexAU runtime. Keep NexAU-backed artifact/finalizer factories lazy so
lightweight evaluator specs and client probe declarations stay importable.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .artifact_tools import ARTIFACT_TOOL_REFS, ArtifactInspectionToolFactory, EvaluatorArtifactToolRef
    from .client_probe import standard_client_gui_tools, standard_client_probe_tools
    from .final_report import FinalReportState, FinalReportToolFactory

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ARTIFACT_TOOL_REFS": ("northcua.agentkit.evaluator.tools.artifact_tools", "ARTIFACT_TOOL_REFS"),
    "ArtifactInspectionToolFactory": (
        "northcua.agentkit.evaluator.tools.artifact_tools",
        "ArtifactInspectionToolFactory",
    ),
    "EvaluatorArtifactToolRef": (
        "northcua.agentkit.evaluator.tools.artifact_tools",
        "EvaluatorArtifactToolRef",
    ),
    "FinalReportState": ("northcua.agentkit.evaluator.tools.final_report", "FinalReportState"),
    "FinalReportToolFactory": ("northcua.agentkit.evaluator.tools.final_report", "FinalReportToolFactory"),
    "standard_client_gui_tools": (
        "northcua.agentkit.evaluator.tools.client_probe",
        "standard_client_gui_tools",
    ),
    "standard_client_probe_tools": (
        "northcua.agentkit.evaluator.tools.client_probe",
        "standard_client_probe_tools",
    ),
}


def __getattr__(name: str) -> object:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(name)
    module_name, attr_name = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = [
    "ARTIFACT_TOOL_REFS",
    "ArtifactInspectionToolFactory",
    "EvaluatorArtifactToolRef",
    "FinalReportState",
    "FinalReportToolFactory",
    "standard_client_gui_tools",
    "standard_client_probe_tools",
]
