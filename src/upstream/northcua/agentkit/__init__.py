"""Low-code CUA harness API for registering NorthCUA agents.

The package root intentionally imports only domain-neutral, lightweight
contracts. Model execution helpers are loaded lazily so controller-only or
base CLI environments can import ``northcua.agentkit`` without installing
NexAU.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from northcua.core.budget import AgentBudgetPolicy, AgentBudgetScope, AgentStepBudget
from northcua.core.terminal import (
    AgentRunStatus,
    AgentTerminalAction,
    AgentTerminalDecision,
    AgentTerminalReason,
    TerminalDecisionAdapter,
    TerminalDecisionPolicy,
)

from .decorators import cua_probe, cua_tool, register_agent
from .presentation import (
    AgentRenderContext,
    AgentTerminalRendererProtocol,
    IdentityPanelProjector,
    PanelProjectorProtocol,
    TemplateTerminalRenderer,
    TerminalRunCallbacks,
    presentation_spec,
    resolve_panel_projector,
    resolve_terminal_renderer,
    run_with_terminal_renderer,
)
from .registry import AgentKitRegistry, get_agentkit_registry, load_all_agent_specs
from .runtime import (
    AgentRoundRunResult,
    AgentRoundRuntime,
    AgentRoundRuntimeSpec,
    AgentRuntimeCounterState,
    AgentRuntimeCursor,
    AgentRuntimePhase,
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    AgentSessionRuntime,
    AgentStepRuntime,
)
from .runtime_templates import (
    AgentArtifactPaths,
    StepwiseToolAgentTemplate,
    StepwiseToolRunResult,
    StepwiseToolStepContext,
    StepwiseToolStepResult,
)
from .skills import AgentKitSkillResolver, ResolvedAgentSkillSettings
from .spec import (
    AgentCliArgumentSpec,
    AgentCliCommandSpec,
    AgentCliSpec,
    AgentPresentationSpec,
    AgentRemoteSpec,
    AgentRuntimeSpec,
    AgentSessionSpec,
    AgentSkillSpec,
    AgentSpec,
    ArtifactLayoutSpec,
    ObservationProvider,
    ObservationSpec,
    ProbeBundle,
    ProbeUseRef,
    PromptProjector,
    PromptSpec,
    SessionAdapter,
    TerminalPolicy,
    ToolBundle,
    ToolExecutionMode,
    ToolUseRef,
)

if TYPE_CHECKING:
    from northcua.core.enums import EvaluationSchedule, EvaluationToolPlacement

    from .agent_config import clone_agent_config_with_hooks
    from .builtin_common import BuiltinAgentCliArguments, MCPServerSpecProjector, ProtocolDescriptorProjector
    from .device_adapter import AgentKitMCPRuntimeAdapter
    from .evaluator import (
        AgentKitEvaluatorRunner,
        EvaluationEfficiencyAssessment,
        EvaluationInputScope,
        EvaluationSafetyAssessment,
        EvaluationSafetyFinding,
        EvaluatorRunContext,
        EvaluatorRunResult,
        EvaluatorSpec,
        EvaluatorToolRef,
        EvidencePolicy,
        RoundEvaluationBundle,
        RoundEvaluationReport,
        ToolPlacementPolicy,
    )
    from .external_tools import (
        AgentKitExternalToolDispatcher,
        ExternalToolDispatchResult,
        command_records_from_pending_tool_calls,
        pending_tool_calls_from_meta,
        tool_execution_records_from_command_results,
    )
    from .model_step import (
        AgentKitModelStepComponents,
        AgentKitModelStepExecutor,
        AgentKitModelStepResult,
    )
    from .prompt_history import (
        PromptHistoryMessageInspector,
        PromptHistoryReplayer,
        StepHistoryTrimmer,
        ToolResultContentProjector,
        ToolUseBlockFactory,
        append_tool_step_history,
        assistant_history_message,
        message_has_image,
        tool_result_history_messages,
        trim_step_history,
    )
    from .tool_trace import (
        ResolvedToolExecution,
        ToolExecutionResolver,
        ToolOutputProjector,
        compact_tool_output,
        compact_tool_outputs,
        resolve_tool_executions,
        tool_history_name,
        tool_history_text,
        tool_output_text,
    )

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentKitExternalToolDispatcher": ("northcua.agentkit.external_tools", "AgentKitExternalToolDispatcher"),
    "AgentKitEvaluatorRunner": ("northcua.agentkit.evaluator", "AgentKitEvaluatorRunner"),
    "AgentKitMCPRuntimeAdapter": ("northcua.agentkit.device_adapter", "AgentKitMCPRuntimeAdapter"),
    "AgentKitModelStepComponents": ("northcua.agentkit.model_step", "AgentKitModelStepComponents"),
    "AgentKitModelStepExecutor": ("northcua.agentkit.model_step", "AgentKitModelStepExecutor"),
    "AgentKitModelStepResult": ("northcua.agentkit.model_step", "AgentKitModelStepResult"),
    "BuiltinAgentCliArguments": ("northcua.agentkit.builtin_common", "BuiltinAgentCliArguments"),
    "EvaluatorSpec": ("northcua.agentkit.evaluator", "EvaluatorSpec"),
    "EvaluatorToolRef": ("northcua.agentkit.evaluator", "EvaluatorToolRef"),
    "EvaluatorRunContext": ("northcua.agentkit.evaluator", "EvaluatorRunContext"),
    "EvaluatorRunResult": ("northcua.agentkit.evaluator", "EvaluatorRunResult"),
    "EvidencePolicy": ("northcua.agentkit.evaluator", "EvidencePolicy"),
    "EvaluationEfficiencyAssessment": ("northcua.agentkit.evaluator", "EvaluationEfficiencyAssessment"),
    "EvaluationInputScope": ("northcua.agentkit.evaluator", "EvaluationInputScope"),
    "EvaluationSafetyAssessment": ("northcua.agentkit.evaluator", "EvaluationSafetyAssessment"),
    "EvaluationSafetyFinding": ("northcua.agentkit.evaluator", "EvaluationSafetyFinding"),
    "EvaluationSchedule": ("northcua.core.enums", "EvaluationSchedule"),
    "EvaluationToolPlacement": ("northcua.core.enums", "EvaluationToolPlacement"),
    "ExternalToolDispatchResult": ("northcua.agentkit.external_tools", "ExternalToolDispatchResult"),
    "RoundEvaluationBundle": ("northcua.agentkit.evaluator", "RoundEvaluationBundle"),
    "RoundEvaluationReport": ("northcua.agentkit.evaluator", "RoundEvaluationReport"),
    "ToolPlacementPolicy": ("northcua.agentkit.evaluator", "ToolPlacementPolicy"),
    "MCPServerSpecProjector": ("northcua.agentkit.builtin_common", "MCPServerSpecProjector"),
    "ProtocolDescriptorProjector": ("northcua.agentkit.builtin_common", "ProtocolDescriptorProjector"),
    "PromptHistoryMessageInspector": ("northcua.agentkit.prompt_history", "PromptHistoryMessageInspector"),
    "PromptHistoryReplayer": ("northcua.agentkit.prompt_history", "PromptHistoryReplayer"),
    "ResolvedToolExecution": ("northcua.agentkit.tool_trace", "ResolvedToolExecution"),
    "StepHistoryTrimmer": ("northcua.agentkit.prompt_history", "StepHistoryTrimmer"),
    "ToolExecutionResolver": ("northcua.agentkit.tool_trace", "ToolExecutionResolver"),
    "ToolOutputProjector": ("northcua.agentkit.tool_trace", "ToolOutputProjector"),
    "ToolResultContentProjector": ("northcua.agentkit.prompt_history", "ToolResultContentProjector"),
    "ToolUseBlockFactory": ("northcua.agentkit.prompt_history", "ToolUseBlockFactory"),
    "append_tool_step_history": ("northcua.agentkit.prompt_history", "append_tool_step_history"),
    "assistant_history_message": ("northcua.agentkit.prompt_history", "assistant_history_message"),
    "clone_agent_config_with_hooks": ("northcua.agentkit.agent_config", "clone_agent_config_with_hooks"),
    "command_records_from_pending_tool_calls": (
        "northcua.agentkit.external_tools",
        "command_records_from_pending_tool_calls",
    ),
    "compact_tool_output": ("northcua.agentkit.tool_trace", "compact_tool_output"),
    "compact_tool_outputs": ("northcua.agentkit.tool_trace", "compact_tool_outputs"),
    "message_has_image": ("northcua.agentkit.prompt_history", "message_has_image"),
    "pending_tool_calls_from_meta": ("northcua.agentkit.external_tools", "pending_tool_calls_from_meta"),
    "resolve_tool_executions": ("northcua.agentkit.tool_trace", "resolve_tool_executions"),
    "tool_execution_records_from_command_results": (
        "northcua.agentkit.external_tools",
        "tool_execution_records_from_command_results",
    ),
    "tool_history_name": ("northcua.agentkit.tool_trace", "tool_history_name"),
    "tool_history_text": ("northcua.agentkit.tool_trace", "tool_history_text"),
    "tool_output_text": ("northcua.agentkit.tool_trace", "tool_output_text"),
    "tool_result_history_messages": ("northcua.agentkit.prompt_history", "tool_result_history_messages"),
    "trim_step_history": ("northcua.agentkit.prompt_history", "trim_step_history"),
}

__all__ = [
    "AgentArtifactPaths",
    "AgentBudgetPolicy",
    "AgentBudgetScope",
    "AgentCliArgumentSpec",
    "AgentCliCommandSpec",
    "AgentCliSpec",
    "AgentKitExternalToolDispatcher",
    "AgentKitEvaluatorRunner",
    "AgentKitMCPRuntimeAdapter",
    "AgentKitModelStepComponents",
    "AgentKitModelStepExecutor",
    "AgentKitModelStepResult",
    "AgentKitRegistry",
    "AgentKitSkillResolver",
    "AgentPresentationSpec",
    "AgentRemoteSpec",
    "AgentRenderContext",
    "AgentRoundRunResult",
    "AgentRoundRuntime",
    "AgentRoundRuntimeSpec",
    "AgentRuntimeCounterState",
    "AgentRuntimeCursor",
    "AgentRuntimePhase",
    "AgentRuntimeSpec",
    "AgentRuntimeStepContext",
    "AgentRuntimeStepOutcome",
    "AgentSessionSpec",
    "AgentSessionRuntime",
    "AgentSkillSpec",
    "AgentSpec",
    "AgentStepRuntime",
    "AgentStepBudget",
    "AgentRunStatus",
    "AgentTerminalAction",
    "AgentTerminalDecision",
    "AgentTerminalRendererProtocol",
    "AgentTerminalReason",
    "ArtifactLayoutSpec",
    "BuiltinAgentCliArguments",
    "EvaluatorSpec",
    "EvaluatorToolRef",
    "EvaluatorRunContext",
    "EvaluatorRunResult",
    "EvidencePolicy",
    "EvaluationEfficiencyAssessment",
    "EvaluationInputScope",
    "EvaluationSafetyAssessment",
    "EvaluationSafetyFinding",
    "EvaluationSchedule",
    "EvaluationToolPlacement",
    "ExternalToolDispatchResult",
    "IdentityPanelProjector",
    "MCPServerSpecProjector",
    "ObservationProvider",
    "ObservationSpec",
    "PanelProjectorProtocol",
    "ProbeBundle",
    "ProbeUseRef",
    "PromptProjector",
    "PromptSpec",
    "PromptHistoryMessageInspector",
    "PromptHistoryReplayer",
    "ResolvedToolExecution",
    "ResolvedAgentSkillSettings",
    "ProtocolDescriptorProjector",
    "RoundEvaluationBundle",
    "RoundEvaluationReport",
    "SessionAdapter",
    "StepwiseToolAgentTemplate",
    "StepwiseToolRunResult",
    "StepwiseToolStepContext",
    "StepwiseToolStepResult",
    "TemplateTerminalRenderer",
    "TerminalPolicy",
    "TerminalRunCallbacks",
    "TerminalDecisionAdapter",
    "TerminalDecisionPolicy",
    "StepHistoryTrimmer",
    "ToolExecutionResolver",
    "ToolOutputProjector",
    "ToolResultContentProjector",
    "ToolUseBlockFactory",
    "ToolBundle",
    "ToolExecutionMode",
    "ToolPlacementPolicy",
    "ToolUseRef",
    "append_tool_step_history",
    "assistant_history_message",
    "clone_agent_config_with_hooks",
    "command_records_from_pending_tool_calls",
    "compact_tool_output",
    "compact_tool_outputs",
    "cua_probe",
    "cua_tool",
    "get_agentkit_registry",
    "load_all_agent_specs",
    "message_has_image",
    "pending_tool_calls_from_meta",
    "presentation_spec",
    "register_agent",
    "resolve_panel_projector",
    "resolve_terminal_renderer",
    "resolve_tool_executions",
    "run_with_terminal_renderer",
    "tool_execution_records_from_command_results",
    "tool_history_name",
    "tool_history_text",
    "tool_output_text",
    "tool_result_history_messages",
    "trim_step_history",
]


def __getattr__(name: str) -> object:
    """Resolve optional AgentKit helpers on first use."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
