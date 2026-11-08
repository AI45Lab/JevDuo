"""Toolset construction for AgentKit evaluator runtime runs."""

from __future__ import annotations

from nexau.archs.tool.tool import Tool

from northcua.agentkernel.tools import contract_to_nexau_tool
from northcua.core.enums import EvaluationToolPlacement

from ..protocol import RoundEvaluationReport
from ..routing import EvaluatorClientToolDispatcher, PlacementResolution, ToolPlacementResolver
from ..spec import EvaluatorSpec
from ..tools import ARTIFACT_TOOL_REFS, ArtifactInspectionToolFactory, FinalReportState, FinalReportToolFactory
from .contracts import EvaluatorRunContext, EvaluatorToolset
from .tool_calls import EvaluatorClientToolBuilder, format_tool_output

_ARTIFACT_TOOL_NAMES = frozenset(tool.name for tool in ARTIFACT_TOOL_REFS)


class EvaluatorToolsetBuilder:
    """Build resolved evaluator tools from spec, placement, and context."""

    def __init__(
        self,
        *,
        spec: EvaluatorSpec,
        placement_resolver: ToolPlacementResolver | None = None,
        client_tool_builder: EvaluatorClientToolBuilder | None = None,
    ) -> None:
        """Create a toolset builder.

        Args:
            spec: Evaluator specification whose visible tools are exposed.
            placement_resolver: Optional placement resolver override.
            client_tool_builder: Optional client tool builder override.
        """

        self.spec = spec
        self.placement_resolver = placement_resolver or ToolPlacementResolver()
        self.client_tool_builder = client_tool_builder or EvaluatorClientToolBuilder()

    def build(
        self,
        *,
        context: EvaluatorRunContext,
        include_finalizer: bool = True,
        require_finalizer: bool = True,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | None = None,
    ) -> EvaluatorToolset:
        """Build a toolset with compatibility switches.

        Args:
            context: Typed runtime context for the evaluator run.
            include_finalizer: Whether to include `final_report` in returned tools.
            require_finalizer: Whether missing finalizer should raise.
            client_tool_dispatcher: Optional live client tool dispatcher.

        Returns:
            Toolset containing resolved artifact, client, and finalizer tools.

        Raises:
            ValueError: If finalizer policy or custom/client tool wiring is invalid.
        """

        if require_finalizer:
            self.validate_finalizer_available()
        placement = self.placement_resolver.resolve(self.spec, remote=context.remote)
        tools: list[Tool] = []
        if placement.server_tools:
            tools.extend(
                ArtifactInspectionToolFactory(
                    artifact_root=context.artifact_root,
                    bundle=context.bundle,
                    evidence_manifest=context.evidence_manifest,
                    step_trace_by_session_step=context.step_trace_by_session_step,
                ).build_tools()
            )
            tools.extend(self._build_custom_server_tools(placement))
        if placement.client_probe_tools or placement.client_mutating_tools:
            tools.extend(
                self.client_tool_builder.build(
                    placement=placement,
                    remote=context.remote,
                    client_tool_dispatcher=client_tool_dispatcher,
                )
            )
        final_report_state = FinalReportState()
        if include_finalizer and placement.finalizer_tools:
            if len(placement.finalizer_tools) != 1:
                raise ValueError("Evaluator toolset requires exactly one finalizer contract.")
            finalizer_spec = placement.finalizer_tools[0]
            tools.append(
                FinalReportToolFactory(
                    report_model=self.spec.report_model or RoundEvaluationReport,
                    evidence_manifest=context.evidence_manifest,
                    state=final_report_state,
                    contract=finalizer_spec.contract,
                ).build_tool()
            )
        return EvaluatorToolset(
            tools=tuple(self._select_visible_tools(tools)),
            placement=placement,
            final_report_state=final_report_state,
        )

    def validate_finalizer_available(self) -> None:
        """Raise if the evaluator spec cannot terminate through final_report.

        Raises:
            ValueError: If no visible `FINALIZER` tool is configured.
        """

        if not any(tool.placement == EvaluationToolPlacement.FINALIZER for tool in self.spec.visible_tools()):
            raise ValueError(f"Evaluator '{self.spec.kind}' must expose a final_report finalizer tool.")

    def _select_visible_tools(self, tools: list[Tool]) -> list[Tool]:
        visible_tool_names = [tool.name for tool in self.spec.visible_tools()]
        visible_names = set(visible_tool_names)
        tools_by_name: dict[str, Tool] = {}
        duplicate_names: set[str] = set()
        for tool in tools:
            tool_name = str(getattr(tool, "name", "") or "")
            if tool_name in tools_by_name:
                duplicate_names.add(tool_name)
            tools_by_name[tool_name] = tool
        if duplicate_names:
            raise ValueError(f"Duplicate evaluator runtime tools: {', '.join(sorted(duplicate_names))}")
        missing = sorted(visible_names - set(tools_by_name))
        if missing:
            raise ValueError(f"Evaluator visible tools missing runtime implementations: {', '.join(missing)}")
        return [tools_by_name[name] for name in visible_tool_names]

    @staticmethod
    def _build_custom_server_tools(placement: PlacementResolution) -> list[Tool]:
        custom_tools: list[Tool] = []
        for tool_spec in placement.server_tools:
            if tool_spec.name in _ARTIFACT_TOOL_NAMES:
                continue
            if tool_spec.implementation is None:
                raise ValueError(f"Evaluator tool '{tool_spec.name}' has no implementation.")
            contract = tool_spec.contract
            if contract is None:
                raise ValueError(f"Evaluator tool '{tool_spec.name}' must be backed by a ToolContract.")
            custom_tools.append(
                contract_to_nexau_tool(
                    contract,
                    implementation=tool_spec.implementation,
                    formatter=format_tool_output,
                    disable_parallel=True,
                )
            )
        return custom_tools


__all__ = ["EvaluatorToolsetBuilder"]
