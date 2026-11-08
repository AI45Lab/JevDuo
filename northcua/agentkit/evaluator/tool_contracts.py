"""Lightweight evaluator tool contract declarations.

This module intentionally avoids NexAU runtime imports so low-code evaluator
specs can be imported in controller/base environments.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from northcua.agentkernel.json_types import JsonObject
from northcua_protocol.capabilities import Capability
from northcua_runtime_shared.tool_contracts import (
    DirectHandlerBinding,
    EvaluatorArtifactBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
)

from .protocol import RoundEvaluationReport


@dataclass(slots=True, frozen=True)
class EvaluatorArtifactToolRef:
    """Static tool contract bound to artifact reader methods at runtime."""

    contract: ToolContract
    implementation_name: str

    @property
    def name(self) -> str:
        """Return the provider-visible tool name from the contract."""

        return self.contract.provider_name

    @property
    def description(self) -> str:
        """Return the contract description."""

        return self.contract.description

    @property
    def input_schema(self) -> JsonObject:
        """Return the contract parameter schema."""

        return self.contract.parameters_schema


def _artifact_tool_ref(
    *,
    name: str,
    description: str,
    input_schema: JsonObject,
    implementation_name: str,
    profiles: tuple[str, ...] = ("evaluator.desktop.round", "evaluator.web.round"),
    capability: Capability = Capability.DESKTOP_MONITOR,
) -> EvaluatorArtifactToolRef:
    contract = ToolContract(
        tool_id=name,
        public_name=name,
        description=description,
        namespace="evaluator.artifact",
        agent_kind="evaluator",
        kind=ToolContractKind.ARTIFACT,
        audience=ToolAudience.EVALUATOR,
        placement=ToolPlacement.EVALUATOR_ARTIFACT,
        capability=capability,
        json_schema=input_schema,
        side_effect=False,
        readonly=True,
        binding=EvaluatorArtifactBinding(
            handler_path=(
                f"northcua.agentkit.evaluator.tools.artifact_tools:ArtifactInspectionToolFactory.{implementation_name}"
            ),
            artifact_kind="round",
        ),
        profiles=profiles,
        source="northcua.agentkit.evaluator.tools.artifact_tools",
    )
    return EvaluatorArtifactToolRef(contract=contract, implementation_name=implementation_name)


ARTIFACT_TOOL_REFS: tuple[EvaluatorArtifactToolRef, ...] = (
    _artifact_tool_ref(
        name="read_round_bundle",
        description=(
            "Read the standard model-facing round bundle for the current evaluation target. "
            "Use section='trace' or section='trajectory' for the evaluated trajectory; "
            "use detail_level='debug' only for rare artifact debugging."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": (
                        "Bundle section. Valid values include trace, trajectory, final, evidence, steps, "
                        "summary, tools, tool_trace, request, messages, all; raw bundle field names such as "
                        "session_id are also accepted for compatibility. When omitted, returns the compact "
                        "model-facing bundle rather than one narrow section."
                    ),
                    "examples": ["trace", "trajectory", "final", "evidence", "tools"],
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["standard", "debug"],
                    "default": "standard",
                    "description": "Use standard for normal judging. Use debug only when compact evidence is insufficient.",
                },
            },
        },
        implementation_name="_read_round_bundle",
    ),
    _artifact_tool_ref(
        name="read_step",
        description="Read one persisted step trace summary by session step number.",
        input_schema={
            "type": "object",
            "properties": {"session_step": {"type": "integer"}, "detail_level": {"type": "string"}},
            "required": ["session_step"],
        },
        implementation_name="_read_step_trace",
    ),
    _artifact_tool_ref(
        name="read_trace",
        description="Alias for read_step; reads persisted model/tool trace for one session step.",
        input_schema={
            "type": "object",
            "properties": {"session_step": {"type": "integer"}, "detail_level": {"type": "string"}},
            "required": ["session_step"],
        },
        implementation_name="_read_step_trace",
    ),
    _artifact_tool_ref(
        name="query_artifacts",
        description="List selected evidence refs by optional evidence kind.",
        input_schema={"type": "object", "properties": {"kind": {"type": "string"}}},
        implementation_name="_query_evidence_manifest",
    ),
    _artifact_tool_ref(
        name="query_runtime_events",
        description="Search runtime event summaries for the current round.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        implementation_name="_query_runtime_events",
    ),
    _artifact_tool_ref(
        name="query_tool_results",
        description="Search tool results produced in the current round.",
        input_schema={
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "session_step": {"type": "integer"},
                "detail_level": {"type": "string"},
            },
        },
        implementation_name="_query_tool_results",
    ),
    _artifact_tool_ref(
        name="query_a11y_snapshot",
        description="Read the persisted accessibility snapshot for one evidence reference or step.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_ref": {"type": "string"},
                "session_step": {"type": "integer"},
                "detail_level": {"type": "string"},
            },
        },
        implementation_name="_query_a11y_snapshot",
        profiles=("evaluator.desktop.round",),
        capability=Capability.A11Y_TREE,
    ),
    _artifact_tool_ref(
        name="query_dom_snapshot",
        description="Read the persisted Web DOM snapshot for one evidence reference or step.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_ref": {"type": "string"},
                "session_step": {"type": "integer"},
                "detail_level": {"type": "string"},
            },
        },
        implementation_name="_query_dom_snapshot",
        profiles=("evaluator.web.round",),
        capability=Capability.BROWSER_PAGE,
    ),
    _artifact_tool_ref(
        name="query_web_page_state",
        description="Read compact URL, title, ref count, settled state, and verification state for one Web step.",
        input_schema={
            "type": "object",
            "properties": {"session_step": {"type": "integer"}, "detail_level": {"type": "string"}},
            "required": ["session_step"],
        },
        implementation_name="_query_web_page_state",
        profiles=("evaluator.web.round",),
        capability=Capability.BROWSER_PAGE,
    ),
    _artifact_tool_ref(
        name="query_som_annotation",
        description=(
            "Read compact Set-of-Mark annotation metadata for one Web evidence reference or step. "
            "Geometry is returned only when include_geometry=true and ref_filter is provided."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_ref": {"type": "string"},
                "session_step": {"type": "integer"},
                "include_geometry": {"type": "boolean"},
                "ref_filter": {"type": "array", "items": {"type": "string"}},
                "detail_level": {"type": "string"},
            },
        },
        implementation_name="_query_som_annotation",
        profiles=("evaluator.web.round",),
        capability=Capability.BROWSER_PAGE,
    ),
    _artifact_tool_ref(
        name="query_web_terminal_verification",
        description="Read the terminal verification record for the evaluated Web round.",
        input_schema={
            "type": "object",
            "properties": {"session_step": {"type": "integer"}, "detail_level": {"type": "string"}},
        },
        implementation_name="_query_web_terminal_verification",
        profiles=("evaluator.web.round",),
        capability=Capability.BROWSER_PAGE,
    ),
    _artifact_tool_ref(
        name="compare_images",
        description="Compare two evidence images and return deterministic similarity metrics.",
        input_schema={
            "type": "object",
            "properties": {"left_ref": {"type": "string"}, "right_ref": {"type": "string"}},
            "required": ["left_ref", "right_ref"],
        },
        implementation_name="_compare_images",
    ),
    _artifact_tool_ref(
        name="extract_image_text_summary",
        description="OCR summary for one evidence image using the required local tesseract binary.",
        input_schema={
            "type": "object",
            "properties": {"evidence_ref": {"type": "string"}},
            "required": ["evidence_ref"],
        },
        implementation_name="_extract_image_text_summary",
    ),
)


def final_report_tool_contract(report_model: type[BaseModel] = RoundEvaluationReport) -> ToolContract:
    """Return the canonical finalizer contract for an evaluator report model."""

    return ToolContract(
        tool_id="final_report",
        public_name="final_report",
        description="Submit the final typed evaluator report and finish evaluation.",
        namespace="evaluator.finalizer",
        agent_kind="evaluator",
        kind=ToolContractKind.FINALIZER,
        audience=ToolAudience.EVALUATOR,
        placement=ToolPlacement.FINALIZER,
        capability=Capability.DESKTOP_MONITOR,
        json_schema=report_model.model_json_schema(),
        side_effect=False,
        readonly=True,
        binding=DirectHandlerBinding(
            handler_path="northcua.agentkit.evaluator.tools.final_report:FinalReportToolFactory._submit_final_report"
        ),
        profiles=("evaluator.desktop.round", "evaluator.web.round"),
        source="northcua.agentkit.evaluator.tools.final_report",
    )


__all__ = ["ARTIFACT_TOOL_REFS", "EvaluatorArtifactToolRef", "final_report_tool_contract"]
