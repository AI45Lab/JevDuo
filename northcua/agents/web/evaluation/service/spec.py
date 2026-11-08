"""Low-code AgentKit evaluator spec for Web rounds."""

from __future__ import annotations

from northcua.agentkit.evaluator.spec import EvaluatorSpec, EvaluatorToolRef, EvidencePolicy, ToolPlacementPolicy
from northcua.agentkit.evaluator.tool_contracts import ARTIFACT_TOOL_REFS
from northcua.core.enums import EvaluationSchedule, EvaluationToolPlacement

from ..domain import WebRoundEvaluationReport
from ..tools import web_browser_client_tool_refs

_WEB_ARTIFACT_TOOL_NAMES = (
    "read_round_bundle",
    "query_tool_results",
    "query_dom_snapshot",
    "query_web_page_state",
    "query_som_annotation",
    "query_web_terminal_verification",
    "compare_images",
)


def build_web_round_evaluator_spec(
    *,
    max_steps: int = 12,
    force_finalizer_after_steps: int = 8,
    evidence_policy: EvidencePolicy | None = None,
    enable_live_probe: bool = False,
    allow_client_mutating_tools: bool = False,
    schedule: EvaluationSchedule = EvaluationSchedule.AFTER_ROUND_ASYNC,
) -> EvaluatorSpec:
    """Build the Web round evaluator spec without exposing raw browser tools."""

    artifact_tools = _web_artifact_tool_refs()
    browser_tools = (
        web_browser_client_tool_refs(allow_mutating=allow_client_mutating_tools) if enable_live_probe else ()
    )
    placement_policy = ToolPlacementPolicy(
        allowed_placements=_allowed_placements(allow_client_mutating_tools=allow_client_mutating_tools),
        allow_client_live_probe=enable_live_probe,
        allow_client_mutating_tools=allow_client_mutating_tools,
    )
    return EvaluatorSpec.with_default_tools(
        kind="web_round_evaluator",
        display_name="Web Round Evaluator",
        report_model=WebRoundEvaluationReport,
        evidence_policy=evidence_policy
        or EvidencePolicy(
            screenshot_max=4,
            include_initial_screenshot=True,
            include_last_screenshot=True,
            include_pinned_evidence=True,
            include_non_image_evidence=True,
        ),
        placement_policy=placement_policy,
        schedule=schedule,
        max_steps=max_steps,
        force_finalizer_after_steps=min(force_finalizer_after_steps, max_steps - 1) if max_steps > 1 else None,
        prompt_template_set="web",
        tools=(*artifact_tools, *browser_tools),
    )


def _web_artifact_tool_refs() -> tuple[EvaluatorToolRef, ...]:
    contracts = {ref.name: ref.contract for ref in ARTIFACT_TOOL_REFS}
    return tuple(
        EvaluatorToolRef.from_contract(
            contracts[name],
            placement=EvaluationToolPlacement.SERVER_ARTIFACT,
        )
        for name in _WEB_ARTIFACT_TOOL_NAMES
    )


def _allowed_placements(*, allow_client_mutating_tools: bool) -> frozenset[EvaluationToolPlacement]:
    placements = {
        EvaluationToolPlacement.SERVER_ARTIFACT,
        EvaluationToolPlacement.CLIENT_PROBE,
        EvaluationToolPlacement.FINALIZER,
    }
    if allow_client_mutating_tools:
        placements.add(EvaluationToolPlacement.CLIENT_MUTATING)
    return frozenset(placements)
