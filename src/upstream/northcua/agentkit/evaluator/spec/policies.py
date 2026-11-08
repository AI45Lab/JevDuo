"""Composable evaluator policy objects used by low-code AgentKit specs."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from pydantic import BaseModel

from northcua.agentkernel.json_types import JsonObject
from northcua.core.enums import EvaluationSchedule, EvaluationToolPlacement
from northcua_runtime_shared.tool_contracts import (
    DirectHandlerBinding,
    EvaluatorClientBinding,
    ToolContract,
    ToolPlacement,
)

from ..protocol import EvaluationInputScope, RoundEvaluationReport
from ..tool_contracts import ARTIFACT_TOOL_REFS, final_report_tool_contract

EvaluatorToolHandler = Callable[..., object]
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


@dataclass(slots=True, frozen=True)
class EvidencePolicy:
    """Evidence defaults for one evaluator.

    The policy is deliberately domain-neutral. Concrete agents decide how to
    adapt their step/runtime records into candidate evidence refs.

    Attributes:
        include_query: Whether the round query is included in context.
        include_tool_calls: Whether assistant tool calls are included.
        include_tool_results: Whether tool results are included.
        include_trace_summary: Whether trace summaries are included.
        screenshot_max: Maximum number of selected image refs before pins.
        include_initial_screenshot: Whether the earliest screenshot is pinned.
        include_last_screenshot: Whether the latest screenshot is pinned.
        include_pinned_evidence: Whether adapter-marked required evidence is pinned.
        include_non_image_evidence: Whether a11y/runtime refs are included.
    """

    include_query: bool = True
    include_tool_calls: bool = True
    include_tool_results: bool = True
    include_trace_summary: bool = True
    screenshot_max: int = 5
    include_initial_screenshot: bool = True
    include_last_screenshot: bool = True
    include_pinned_evidence: bool = True
    include_non_image_evidence: bool = True

    def __post_init__(self) -> None:
        """Validate evidence policy limits."""

        if self.screenshot_max < 0:
            raise ValueError("screenshot_max must be non-negative.")


@dataclass(slots=True, frozen=True)
class EvaluatorToolRef:
    """Evaluator use-site reference for an owned tool contract."""

    contract: ToolContract
    placement: EvaluationToolPlacement
    model_visible: bool = True
    implementation: EvaluatorToolHandler | None = None

    @classmethod
    def from_contract(
        cls,
        contract: ToolContract,
        *,
        placement: EvaluationToolPlacement,
        implementation: EvaluatorToolHandler | None = None,
        model_visible: bool | None = None,
    ) -> "EvaluatorToolRef":
        """Create an evaluator tool ref from the canonical contract."""

        if implementation is not None and not contract.all_bindings:
            contract = replace(
                contract,
                binding=DirectHandlerBinding(handler_path=_handler_path(implementation)),
            )
        visible = contract.model_visible if model_visible is None else model_visible
        return cls(
            contract=contract,
            placement=placement,
            model_visible=visible,
            implementation=implementation,
        )

    def __post_init__(self) -> None:
        """Validate use-site policy against the canonical contract."""

        if not _TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"Invalid evaluator tool name: {self.name!r}")
        if self.dispatch_name and not _TOOL_NAME_PATTERN.fullmatch(self.dispatch_name):
            raise ValueError(f"Invalid evaluator tool dispatch_name: {self.dispatch_name!r}")
        if self.timeout_seconds <= 0:
            raise ValueError("Evaluator tool timeout_seconds must be positive.")
        if self.name == "final_report" and self.placement != EvaluationToolPlacement.FINALIZER:
            raise ValueError("The final_report tool name is reserved for FINALIZER placement.")
        if self.placement == EvaluationToolPlacement.FINALIZER and self.name != "final_report":
            raise ValueError("FINALIZER evaluator tools must use the final_report tool name.")
        expected_placement = _contract_evaluation_placement(self.contract)
        if expected_placement != self.placement:
            raise ValueError(f"Evaluator tool {self.name!r} placement drifts from ToolContract")
        if self.placement in {EvaluationToolPlacement.CLIENT_PROBE, EvaluationToolPlacement.CLIENT_MUTATING}:
            self._validate_client_tool_contract()
        if self.model_visible and not self.contract.model_visible:
            raise ValueError(f"Evaluator tool {self.name!r} cannot expose a non-model-visible ToolContract")
        object.__setattr__(self, "model_visible", self.model_visible and self.contract.model_visible)

    def _validate_client_tool_contract(self) -> None:
        expected_placement = _contract_evaluation_placement(self.contract)
        if expected_placement != self.placement:
            raise ValueError(f"Evaluator tool {self.name!r} placement drifts from ToolContract")
        client_binding = _client_binding(self.contract)
        if client_binding is None:
            raise ValueError(f"Evaluator tool {self.name!r} ToolContract lacks an EvaluatorClientBinding")

    @property
    def name(self) -> str:
        return self.contract.provider_name

    @property
    def description(self) -> str:
        return self.contract.description

    @property
    def readonly(self) -> bool:
        return self.contract.is_readonly

    @property
    def input_schema(self) -> JsonObject:
        return self.contract.parameters_schema

    @property
    def dispatch_name(self) -> str:
        return _contract_dispatch_name(self.contract)

    @property
    def server_name(self) -> str:
        return self.contract.server_name

    @property
    def registration_hook(self) -> str:
        return self.contract.registration_hook

    @property
    def timeout_seconds(self) -> float:
        return self.contract.timeout_seconds


@dataclass(slots=True, frozen=True)
class ToolPlacementPolicy:
    """Tool placement allowlist and live-probe safety gates.

    Attributes:
        allowed_placements: Placements this evaluator may expose.
        allow_client_live_probe: Whether client-side read-only live probes may run.
        allow_client_mutating_tools: Whether client-side mutating tools may run.
    """

    allowed_placements: frozenset[EvaluationToolPlacement] = field(
        default_factory=lambda: frozenset(
            {
                EvaluationToolPlacement.SERVER_ARTIFACT,
                EvaluationToolPlacement.CLIENT_PROBE,
                EvaluationToolPlacement.FINALIZER,
            }
        )
    )
    allow_client_live_probe: bool = False
    allow_client_mutating_tools: bool = False

    def allows(self, placement: EvaluationToolPlacement) -> bool:
        """Return whether the placement is allowed by this policy.

        Args:
            placement: Placement to check.

        Returns:
            True when the placement is allowlisted.
        """

        return placement in self.allowed_placements


@dataclass(slots=True, frozen=True)
class EvaluatorSpec:
    """Declarative source of truth for one AgentKit evaluator.

    Attributes:
        kind: Stable evaluator kind.
        display_name: Human-readable evaluator name.
        input_scope: Scope evaluated by this evaluator.
        report_model: Pydantic report schema used by final_report.
        evidence_policy: Evidence inclusion and pinning policy.
        placement_policy: Tool placement allowlist and live-probe gates.
        schedule: When the evaluator runs relative to the main agent.
        tools: Declared evaluator tool specs.
        max_steps: Maximum model/tool loop steps for this evaluator.
        force_finalizer_after_steps: Optional soft cap for evidence-gathering
            steps. When set, the shared runtime switches to a compact
            finalizer-only repair turn after this many non-finalizer steps
            instead of allowing artifact probing to consume the full hard
            budget.
        prompt_template_set: Prompt template set key.
    """

    kind: str
    display_name: str
    input_scope: EvaluationInputScope = EvaluationInputScope.ROUND
    report_model: type[BaseModel] = RoundEvaluationReport
    evidence_policy: EvidencePolicy = field(default_factory=EvidencePolicy)
    placement_policy: ToolPlacementPolicy = field(default_factory=ToolPlacementPolicy)
    schedule: EvaluationSchedule = EvaluationSchedule.AFTER_ROUND_ASYNC
    tools: tuple[EvaluatorToolRef, ...] = ()
    max_steps: int = 50
    force_finalizer_after_steps: int | None = None
    prompt_template_set: str = "default"

    def __post_init__(self) -> None:
        """Validate evaluator identity and visible tool surface."""

        if not _TOOL_NAME_PATTERN.fullmatch(self.kind):
            raise ValueError(f"Invalid evaluator kind: {self.kind!r}")
        if self.max_steps < 1:
            raise ValueError("Evaluator max_steps must be at least 1.")
        if self.force_finalizer_after_steps is not None:
            if self.force_finalizer_after_steps < 1:
                raise ValueError("Evaluator force_finalizer_after_steps must be at least 1 when set.")
            if self.force_finalizer_after_steps >= self.max_steps:
                raise ValueError("Evaluator force_finalizer_after_steps must leave at least one finalizer step.")
        visible_names = [tool.name for tool in self.tools if tool.model_visible]
        duplicate_names = sorted({name for name in visible_names if visible_names.count(name) > 1})
        if duplicate_names:
            raise ValueError(f"Duplicate evaluator tool names: {', '.join(duplicate_names)}")

    def replace(self, **changes: object) -> "EvaluatorSpec":
        """Return a copy with selected low-code overrides applied.

        Args:
            **changes: Dataclass fields to replace.

        Returns:
            New evaluator spec.
        """

        return replace(self, **changes)

    def visible_tools(self) -> tuple[EvaluatorToolRef, ...]:
        """Return tools visible to the evaluator model after placement filtering.

        Returns:
            Tuple of model-visible tools allowed by placement policy.
        """

        return tuple(tool for tool in self.tools if tool.model_visible and self.placement_policy.allows(tool.placement))

    def has_client_live_tools(self) -> bool:
        """Return whether this evaluator exposes client-side live tools.

        Returns:
            True when client probe or mutating tools are visible.
        """

        return any(
            tool.placement
            in {
                EvaluationToolPlacement.CLIENT_PROBE,
                EvaluationToolPlacement.CLIENT_MUTATING,
            }
            for tool in self.visible_tools()
        )

    def effective_schedule(self) -> EvaluationSchedule:
        """Return the schedule after live-client safety coercion.

        Client probe and mutating tools touch the evaluated environment, so an
        async after-round evaluator must run synchronously instead of racing
        the main agent or remote sandbox lifecycle.

        Returns:
            Coerced evaluation schedule.
        """

        if self.has_client_live_tools() and self.schedule == EvaluationSchedule.AFTER_ROUND_ASYNC:
            return EvaluationSchedule.AFTER_ROUND_SYNC
        return self.schedule

    @classmethod
    def with_default_tools(
        cls,
        *,
        kind: str,
        display_name: str,
        tools: Iterable[EvaluatorToolRef] = (),
        **overrides: object,
    ) -> "EvaluatorSpec":
        """Build a spec with the common server artifact and finalizer tools.

        Args:
            kind: Stable evaluator kind.
            display_name: Human-readable evaluator name.
            tools: Additional evaluator-specific tool specs.
            **overrides: Optional spec field overrides.

        Returns:
            Evaluator spec with read_step/read_trace/query_artifacts/final_report.
        """

        report_model = _report_model_from_overrides(overrides)
        default_tools = _default_evaluator_tool_specs(report_model=report_model)
        spec = cls(
            kind=kind,
            display_name=display_name,
            tools=(*default_tools, *tuple(tools)),
        )
        return spec.replace(**overrides) if overrides else spec


def _report_model_from_overrides(overrides: dict[str, object]) -> type[BaseModel]:
    report_model = overrides.get("report_model", RoundEvaluationReport)
    if not isinstance(report_model, type) or not issubclass(report_model, BaseModel):
        raise ValueError("EvaluatorSpec report_model override must be a Pydantic BaseModel type.")
    return report_model


def _default_evaluator_tool_specs(*, report_model: type[BaseModel]) -> tuple[EvaluatorToolRef, ...]:
    artifacts = {spec.name: spec.contract for spec in ARTIFACT_TOOL_REFS}
    final_report_contract = final_report_tool_contract(report_model)
    return (
        _tool_spec_from_contract(artifacts["read_step"], placement=EvaluationToolPlacement.SERVER_ARTIFACT),
        _tool_spec_from_contract(artifacts["read_trace"], placement=EvaluationToolPlacement.SERVER_ARTIFACT),
        _tool_spec_from_contract(artifacts["query_artifacts"], placement=EvaluationToolPlacement.SERVER_ARTIFACT),
        _tool_spec_from_contract(final_report_contract, placement=EvaluationToolPlacement.FINALIZER),
    )


def _tool_spec_from_contract(contract: ToolContract, *, placement: EvaluationToolPlacement) -> EvaluatorToolRef:
    return EvaluatorToolRef.from_contract(contract, placement=placement)


def _handler_path(handler: EvaluatorToolHandler) -> str:
    module = getattr(handler, "__module__", "")
    qualname = getattr(handler, "__qualname__", getattr(handler, "__name__", ""))
    return f"{module}:{qualname}" if module and qualname else ""


def _contract_dispatch_name(contract: ToolContract) -> str:
    for binding in contract.all_bindings:
        dispatch_name = str(getattr(binding, "dispatch_name", "") or "")
        if dispatch_name:
            return dispatch_name
    return ""


def _contract_evaluation_placement(contract: ToolContract) -> EvaluationToolPlacement:
    if contract.placement == ToolPlacement.CLIENT_PROBE:
        return EvaluationToolPlacement.CLIENT_PROBE
    if contract.placement == ToolPlacement.CLIENT_MUTATING:
        return EvaluationToolPlacement.CLIENT_MUTATING
    if contract.placement == ToolPlacement.EVALUATOR_ARTIFACT:
        return EvaluationToolPlacement.SERVER_ARTIFACT
    if contract.placement == ToolPlacement.FINALIZER:
        return EvaluationToolPlacement.FINALIZER
    raise ValueError(f"Unsupported evaluator ToolContract placement: {contract.placement.value}")


def _client_binding(contract: ToolContract) -> EvaluatorClientBinding | None:
    for binding in contract.all_bindings:
        if isinstance(binding, EvaluatorClientBinding):
            return binding
    return None


__all__ = [
    "EvaluatorSpec",
    "EvaluatorToolRef",
    "EvidencePolicy",
    "ToolPlacementPolicy",
]
