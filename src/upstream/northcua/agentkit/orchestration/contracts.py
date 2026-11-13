"""Small, serializable contracts shared by the orchestrator and plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self

from nexau.core.messages import Message
from pydantic import BaseModel, ConfigDict, Field, model_validator

from northcua.core.json_types import JsonObject


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Usage(ContractModel):
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cleanup_tool_calls: int = Field(default=0, ge=0)


class SubgoalBudget(ContractModel):
    max_model_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    max_seconds: float = Field(gt=0, allow_inf_nan=False)


class SubgoalContract(ContractModel):
    subgoal_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    budget: SubgoalBudget
    agent_input: JsonObject = Field(default_factory=dict)


class Note(ContractModel):
    key: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SubagentResult(ContractModel):
    subgoal_id: str
    attempt_id: str
    outcome: Literal["achieved", "yielded", "blocked", "failed"]
    exit_reason: str
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    criterion_claims: list[str] = Field(default_factory=list)
    trace_ref: str = ""
    last_observation: JsonObject = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    proposed_notes: list[Note] = Field(default_factory=list)


class SlowDecision(ContractModel):
    action: Literal["SUCCESS", "FAIL", "CONTINUE"]
    reason: str = ""
    summary: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    contract: SubgoalContract | None = None
    notes: list[Note] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if (self.action == "CONTINUE") != (self.contract is not None):
            raise ValueError("Only CONTINUE must include a subgoal contract")
        if self.action == "SUCCESS" and not self.evidence_refs:
            raise ValueError("SUCCESS requires observation evidence")
        return self


class SubagentRequest(ContractModel):
    session_id: str
    round_num: int
    attempt_id: str
    task: str
    contract: SubgoalContract
    observation: JsonObject
    notes: list[dict[str, str]] = Field(default_factory=list)
    history: list[JsonObject] = Field(default_factory=list)


class OrchestrationState(ContractModel):
    round_num: int
    started_at: float
    usage: Usage = Field(default_factory=Usage)
    pending: SubagentRequest | None = None
    pending_usage: Usage = Field(default_factory=Usage)
    previous_result: SubagentResult | None = None
    attempted_subgoals: list[str] = Field(default_factory=list)
    terminal: bool = False


class CycleRecord(ContractModel):
    cycle_id: str
    synthetic: bool = False
    observation_id: str = ""
    decision: SlowDecision | None = None
    final_verification: SlowDecision | None = None
    subagent_result: SubagentResult | None = None
    usage: Usage = Field(default_factory=Usage)
    exit_reason: str = ""


class SlowContext(ContractModel):
    task: str
    observation: JsonObject
    history: list[CycleRecord]
    notes: list[dict[str, str]]
    previous_result: SubagentResult | None
    usage: Usage
    budget: JsonObject
    seconds_left: float
    available_subagents: list[JsonObject]


class SubagentRuntime(Protocol):
    """The sole effectful interface supplied to a trusted plugin.

    execute targets the current task environment, including any terminal tools.
    Empty allowed_tools permits no tools. Cancellation propagates to the caller.
    """

    async def observe(self) -> JsonObject: ...
    async def execute(self, tool_name: str, arguments: JsonObject, *, sensitive: bool = False) -> JsonObject: ...
    async def infer(self, prompt: str) -> str: ...
    async def infer_tools(self, messages: list[Message], tools: list[JsonObject]) -> Message: ...
    async def call_model(self, name: str, call: Callable[[], Awaitable[JsonObject]]) -> JsonObject: ...
    async def cleanup(self, tool_name: str, arguments: JsonObject) -> JsonObject: ...
    def remaining_budget(self) -> SubgoalBudget: ...


@dataclass(frozen=True)
class SubagentSpec:
    agent_id: str
    description: str
    capabilities: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    input_model: type[BaseModel] = ContractModel
    protocol_version: int = 1

    def descriptor(self) -> JsonObject:
        return {
            "agent_id": self.agent_id,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "required_tools": list(self.required_tools),
            "protocol_version": self.protocol_version,
            "input_schema": self.input_model.model_json_schema(),
        }


class Subagent(Protocol):
    spec: SubagentSpec

    async def run(self, request: SubagentRequest, runtime: SubagentRuntime) -> SubagentResult: ...


@dataclass
class SubagentRegistry:
    """Explicit registration only; production starts with an empty pool."""

    _agents: dict[str, Subagent] = field(default_factory=dict, init=False)

    def register(self, agent: Subagent) -> None:
        import inspect

        spec = agent.spec
        if not spec.agent_id.strip() or spec.protocol_version != 1 or not inspect.iscoroutinefunction(agent.run):
            raise ValueError("Subagent requires an ID, protocol version 1 and async run")
        if spec.agent_id in self._agents:
            raise ValueError(f"Duplicate subagent: {spec.agent_id}")
        self._agents[spec.agent_id] = agent

    def descriptors(self) -> list[JsonObject]:
        return [agent.spec.descriptor() for agent in self._agents.values()]

    def resolve(self, contract: SubgoalContract) -> Subagent:
        if contract.agent_id not in self._agents:
            raise ValueError("no_subagent_available")
        agent = self._agents[contract.agent_id]
        if not set(contract.required_capabilities) <= set(agent.spec.capabilities):
            raise ValueError("subagent_capability_mismatch")
        if not set(agent.spec.required_tools) <= set(contract.allowed_tools):
            raise ValueError("subagent_tool_mismatch")
        agent.spec.input_model.model_validate(contract.agent_input)
        return agent
