"""Generic AgentKit evaluator runtime orchestration primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.tool.tool import Tool
from nexau.core.messages import Message
from pydantic import BaseModel

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.prompt_history import PromptHistoryReplayer
from northcua.transport.ws.message_seed import SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY

from ..artifacts import EvaluatorArtifactWriter
from ..protocol import (
    EvaluationEvidenceManifest,
    RoundEvaluationBundle,
    RoundEvaluationReport,
)
from ..routing import EvaluatorClientToolDispatcher, ToolPlacementResolver
from ..spec import EvaluatorSpec
from .contracts import EvaluatorPromptEnvelope, EvaluatorRunContext, EvaluatorRunResult, EvaluatorToolset
from .prompt_history import EvaluatorToolResultContentProjector
from .runner_loop import _run_evaluator_round
from .tool_calls import EvaluatorClientToolBuilder, EvaluatorToolCallExecutor
from .toolset_builder import EvaluatorToolsetBuilder
from .trace_writer import EvaluatorTraceWriter

if TYPE_CHECKING:
    from northcua.agentkit.model_step import AgentKitModelStepExecutor


class AgentKitEvaluatorRunner:
    """Reusable runtime scaffold for model-driven evaluators."""

    def __init__(
        self,
        *,
        spec: EvaluatorSpec,
        artifact_writer: EvaluatorArtifactWriter | None = None,
        model_step_executor: "AgentKitModelStepExecutor | None" = None,
        placement_resolver: ToolPlacementResolver | None = None,
        prompt_history_replayer: PromptHistoryReplayer | None = None,
        tool_call_executor: EvaluatorToolCallExecutor | None = None,
        client_tool_builder: EvaluatorClientToolBuilder | None = None,
        trace_writer: EvaluatorTraceWriter | None = None,
    ) -> None:
        self.spec = spec
        self.artifact_writer = artifact_writer
        self.model_step_executor = model_step_executor
        self.placement_resolver = placement_resolver or ToolPlacementResolver()
        self.prompt_history_replayer = prompt_history_replayer or PromptHistoryReplayer(
            tool_result_projector=EvaluatorToolResultContentProjector()
        )
        self.tool_call_executor = tool_call_executor or EvaluatorToolCallExecutor()
        self.client_tool_builder = client_tool_builder or EvaluatorClientToolBuilder()
        self.trace_writer = trace_writer or EvaluatorTraceWriter(artifact_writer)
        self.toolset_builder = EvaluatorToolsetBuilder(
            spec=spec, placement_resolver=self.placement_resolver, client_tool_builder=self.client_tool_builder
        )

    def persist_inputs(
        self, *, round_num: int, bundle: RoundEvaluationBundle, evidence_manifest: EvaluationEvidenceManifest
    ) -> None:
        """Persist evaluator input artifacts.

        Args:
            round_num: One-based evaluation round number.
            bundle: Canonical round evaluation bundle.
            evidence_manifest: Selected evidence manifest.
        """
        if self.artifact_writer is None:
            return
        self.artifact_writer.write_bundle(round_num=round_num, payload=bundle)
        self.artifact_writer.write_evidence_manifest(round_num=round_num, payload=evidence_manifest)

    def persist_final_report(self, *, round_num: int, report: BaseModel | JsonObject) -> None:
        """Persist a validated final report.

        Args:
            round_num: One-based evaluation round number.
            report: Final report model or JSON object.
        """
        if self.artifact_writer is None:
            return
        self.artifact_writer.write_final_report(round_num=round_num, payload=report)

    async def run_round(
        self,
        *,
        context: EvaluatorRunContext,
        messages: list[Message],
        trace_context: JsonObject | None = None,
        externalize_tools: bool = False,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | None = None,
    ) -> EvaluatorRunResult:
        """Run one evaluator loop through AgentKit.

        Args:
            context: Typed runtime context for the evaluated round.
            messages: Prompt messages for the evaluator model.
            trace_context: Optional structured metadata for tracing.
            externalize_tools: Whether tools are routed outside the local process.
            client_tool_dispatcher: Optional live client tool dispatcher.

        Returns:
            Final typed round evaluation result.

        Raises:
            RuntimeError: If no model executor is configured or no final report is submitted.
        """
        return await _run_evaluator_round(
            self,
            context=context,
            messages=messages,
            trace_context=trace_context,
            externalize_tools=externalize_tools,
            client_tool_dispatcher=client_tool_dispatcher,
        )

    def build_toolset(
        self, context: EvaluatorRunContext, *, client_tool_dispatcher: EvaluatorClientToolDispatcher | None = None
    ) -> EvaluatorToolset:
        """Build the model-visible toolset.

        Args:
            context: Typed runtime context for the evaluator run.

        Returns:
            Toolset containing resolved artifact, client, and finalizer tools.
        """
        return self.build_toolset_with_options(context=context, client_tool_dispatcher=client_tool_dispatcher)

    def build_toolset_with_options(
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
            Toolset containing filtered server artifact tools and finalizer state.

        Raises:
            ValueError: If finalizer policy or custom/client tool wiring is invalid.
        """
        return self.toolset_builder.build(
            context=context,
            include_finalizer=include_finalizer,
            require_finalizer=require_finalizer,
            client_tool_dispatcher=client_tool_dispatcher,
        )

    def _configure_model_agent(self, agent_config: AgentConfig, tools: tuple[Tool, ...]) -> None:
        agent_config.name = f"{self.spec.kind}Evaluator"
        agent_config.description = self.spec.display_name
        agent_config.tools = list(tools)
        agent_config.mcp_servers = []
        global_storage = dict(getattr(agent_config, "global_storage", {}) or {})
        global_storage[SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY] = True
        agent_config.global_storage = global_storage
        agent_config.max_iterations = max(int(agent_config.max_iterations or 0), self.spec.max_steps)

    @staticmethod
    def _coerce_round_report(report: BaseModel) -> RoundEvaluationReport:
        if isinstance(report, RoundEvaluationReport):
            return report
        return RoundEvaluationReport.model_validate(report.model_dump(mode="json"))

    def validate_finalizer_available(self) -> None:
        """Raise if the evaluator spec cannot terminate through final_report.

        Raises:
            ValueError: If no visible `FINALIZER` tool is configured.
        """
        self.toolset_builder.validate_finalizer_available()


__all__ = [
    "AgentKitEvaluatorRunner",
    "EvaluatorPromptEnvelope",
    "EvaluatorRunContext",
    "EvaluatorRunResult",
    "EvaluatorToolset",
]
