"""Shared AgentKit runtime hierarchy.

The runtime package defines the reusable lifecycle spine for agent sessions,
rounds, steps, and evaluator phases. Concrete agents plug into this spine
through typed ports so local and remote modes can share orchestration while
swapping only their platform/session and tool-dispatch adapters.
"""

from .cursor import AgentRuntimeCounterState, AgentRuntimeCursor, AgentRuntimePhase
from .pipeline import PortStepPipelineExecutor, StepResultBuilder, TerminalDecisionBuilder
from .ports import (
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    AgentRuntimeTerminalContext,
    AgentRuntimeTerminalPoint,
    AgentRuntimeTerminalProjection,
    EvaluationPort,
    ModelStepPort,
    ObservationPort,
    PromptPort,
    RuntimeStateStorePort,
    SessionResourcePort,
    StepExecutor,
    StepPersistencePort,
    TerminalProjectionPort,
    ToolDispatchPort,
)
from .round import AgentRoundRunResult, AgentRoundRuntime, AgentRoundRuntimeSpec
from .session import AgentSessionRuntime
from .step import AgentStepRuntime, no_tool_call_decision
from .strategies import (
    CallableObservationPort,
    CallablePromptPort,
    InMemoryStepPersistence,
    NoOpEvaluation,
    NoOpSessionResource,
    StaticPromptPort,
)

__all__ = [
    "AgentRoundRunResult",
    "AgentRoundRuntime",
    "AgentRoundRuntimeSpec",
    "AgentRuntimeCounterState",
    "AgentRuntimeCursor",
    "AgentRuntimePhase",
    "AgentRuntimeStepContext",
    "AgentRuntimeStepOutcome",
    "AgentRuntimeTerminalContext",
    "AgentRuntimeTerminalPoint",
    "AgentRuntimeTerminalProjection",
    "AgentSessionRuntime",
    "AgentStepRuntime",
    "CallableObservationPort",
    "CallablePromptPort",
    "EvaluationPort",
    "InMemoryStepPersistence",
    "ModelStepPort",
    "NoOpEvaluation",
    "NoOpSessionResource",
    "ObservationPort",
    "PromptPort",
    "PortStepPipelineExecutor",
    "RuntimeStateStorePort",
    "SessionResourcePort",
    "StaticPromptPort",
    "StepExecutor",
    "StepPersistencePort",
    "StepResultBuilder",
    "TerminalProjectionPort",
    "TerminalDecisionBuilder",
    "ToolDispatchPort",
    "no_tool_call_decision",
]
