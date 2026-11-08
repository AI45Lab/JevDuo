"""Default AgentKit evaluator spec for desktop round completion."""

from __future__ import annotations

from northcua.agentkit.evaluator import (
    EvaluationInputScope,
    EvaluatorSpec,
    EvaluatorToolRef,
    EvidencePolicy,
    RoundEvaluationReport,
    ToolPlacementPolicy,
    standard_client_gui_tools,
    standard_client_probe_tools,
)
from northcua.agentkit.evaluator.tool_contracts import ARTIFACT_TOOL_REFS
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import EvaluationToolMode, EvaluationToolPlacement

_ARTIFACT_CONTRACT_BY_NAME = {spec.name: spec.contract for spec in ARTIFACT_TOOL_REFS}
_DESKTOP_ARTIFACT_TOOLS = tuple(
    EvaluatorToolRef.from_contract(contract, placement=EvaluationToolPlacement.SERVER_ARTIFACT)
    for contract in (
        _ARTIFACT_CONTRACT_BY_NAME["read_round_bundle"],
        _ARTIFACT_CONTRACT_BY_NAME["query_runtime_events"],
        _ARTIFACT_CONTRACT_BY_NAME["query_tool_results"],
        _ARTIFACT_CONTRACT_BY_NAME["query_a11y_snapshot"],
        _ARTIFACT_CONTRACT_BY_NAME["compare_images"],
    )
)


def desktop_round_evaluator_spec(app_config: NorthCUAAppConfig) -> EvaluatorSpec:
    """Return the default low-code spec for desktop round evaluation.

    Args:
        app_config: NorthCUA runtime config carrying evaluation policy.

    Returns:
        Evaluator spec used by the desktop compatibility facade.
    """

    evidence_config = app_config.evaluation.evidence
    placement_config = app_config.evaluation.tools
    standard_probes = standard_client_probe_tools()
    standard_gui_tools = standard_client_gui_tools()
    configured_probe_names = set(placement_config.client_probe_tools)
    configured_gui_names = set(placement_config.client_gui_tools)
    if not configured_probe_names and placement_config.mode == EvaluationToolMode.ALLOW_LLM_SELECTED_TOOLS:
        configured_probe_names = {tool.name for tool in standard_probes}
    if (
        not configured_gui_names
        and placement_config.mode == EvaluationToolMode.ALLOW_LLM_SELECTED_TOOLS
        and placement_config.allow_client_mutating_tools
    ):
        configured_gui_names = {tool.name for tool in standard_gui_tools}
    live_probe_tools = (
        tuple(tool for tool in standard_probes if tool.name in configured_probe_names)
        if placement_config.allow_client_live_probe
        else ()
    )
    live_gui_tools = (
        tuple(tool for tool in standard_gui_tools if tool.name in configured_gui_names)
        if placement_config.allow_client_mutating_tools
        else ()
    )
    return EvaluatorSpec.with_default_tools(
        kind=app_config.evaluation.default_evaluator_kind,
        display_name="Desktop Round Evaluator",
        input_scope=EvaluationInputScope.ROUND,
        report_model=RoundEvaluationReport,
        evidence_policy=EvidencePolicy(
            include_query=True,
            include_tool_calls=True,
            include_tool_results=True,
            include_trace_summary=True,
            screenshot_max=evidence_config.screenshot_max,
            include_initial_screenshot=evidence_config.include_initial_screenshot,
            include_last_screenshot=evidence_config.include_last_screenshot,
            include_pinned_evidence=evidence_config.include_terminal_verification,
            include_non_image_evidence=True,
        ),
        placement_policy=ToolPlacementPolicy(
            allowed_placements=frozenset(placement_config.allowed_placements),
            allow_client_live_probe=placement_config.allow_client_live_probe,
            allow_client_mutating_tools=placement_config.allow_client_mutating_tools,
        ),
        schedule=app_config.evaluation.schedule,
        tools=(*_DESKTOP_ARTIFACT_TOOLS, *live_probe_tools, *live_gui_tools),
        max_steps=app_config.evaluation.max_steps,
        force_finalizer_after_steps=(
            min(app_config.evaluation.force_finalizer_after_steps, app_config.evaluation.max_steps - 1)
            if app_config.evaluation.max_steps > 1
            else None
        ),
        prompt_template_set="desktop_round_completion",
    )


__all__ = ["desktop_round_evaluator_spec"]
