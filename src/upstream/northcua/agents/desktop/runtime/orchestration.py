"""Desktop ports for the shared AgentKit orchestration pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import uuid4

from nexau.core.messages import ImageBlock, Message, Role, TextBlock
from pydantic import ValidationError

from northcua.agentinfra.nexau_compat.adapters.openai_chat import capture_chat_responses
from northcua.agentinfra.providers.chat_response_capture import (
    ChatResponseCapture,
    _ChatResponseAbort,
    chat_finish_reason,
)
from northcua.agentkernel.working_memory import WorkingMemoryStore
from northcua.agentkit.agent_config import clone_agent_config_with_hooks
from northcua.agentkit.model_step import (
    AgentKitModelStepComponents,
    AgentKitModelStepExecutor,
)
from northcua.agentkit.orchestration.contracts import (
    CycleRecord,
    OrchestrationState,
    SlowContext,
    SlowDecision,
    SubagentRegistry,
    SubagentRequest,
    SubagentResult,
    Usage,
)
from northcua.agentkit.orchestration.runtime import (
    BudgetExceeded,
    Journal,
    RunBudget,
    ScopedRuntime,
    read_records,
)
from northcua.agentkit.runtime import (
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    AgentRuntimeTerminalProjection,
    PortStepPipelineExecutor,
)
from northcua.core.coordinates import (
    CoordinateTarget,
    coordinate_policy_context_values,
    policy_from_config,
)
from northcua.core.enums import TaskStatus
from northcua.core.models import CommandRequestRecord, StepLogRecord
from northcua.core.protocol import CommandRequestPayload
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua.transport.ws.local_step.single_model import ModelStepLLMTimeoutError
from northcua.transport.ws.message_seed import SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY

from .observation import DesktopObservationProvider
from .state import DesktopRuntimeStateStore


class OrchestrationStateStore(DesktopRuntimeStateStore):
    """Recover durable usage/dispatch intent and reconcile appended main steps."""

    async def hydrate(self) -> None:
        await super().hydrate()
        state = self.round_coordinator.state
        self.artifact_store.set_query_index(state.round_num)
        for event in read_records(self.artifact_store.query_dir(state.round_num) / "orchestration.jsonl"):
            checkpoint = OrchestrationState.model_validate(event["state"])
            if checkpoint.round_num == state.round_num:
                state.orchestration = checkpoint
        records = {(s.round_num, s.session_step): s for s in state.step_logs}
        for payload in read_records(self.artifact_store.step_log_path):
            step = StepLogRecord.model_validate(payload)
            records[step.round_num, step.session_step] = step
        state.step_logs = sorted(records.values(), key=lambda s: (s.round_num, s.session_step))
        memory = WorkingMemoryStore(self.artifact_store.root)
        for step in state.step_logs:
            if step.orchestration is not None and step.orchestration.decision is not None:
                memory.record_notes(
                    step.orchestration.decision.notes,
                    round_num=step.round_num,
                    session_step=step.session_step,
                    source=step.orchestration.cycle_id,
                )
            if step.orchestration is not None and step.orchestration.synthetic:
                continue
            state.session_step = max(state.session_step, step.session_step + 1)
            if step.round_num == state.round_num:
                state.round_step = max(state.round_step, step.round_step + 1)
        if state.orchestration is None or state.orchestration.round_num != state.round_num:
            state.orchestration = OrchestrationState(round_num=state.round_num, started_at=time.time())
            Journal(self.artifact_store.query_dir() / "orchestration.jsonl").write("run_started", state.orchestration)
        current_steps = [step for step in state.step_logs if step.round_num == state.round_num]
        if current_steps and current_steps[-1].status != TaskStatus.CONTINUE:
            state.orchestration.terminal = True
        if state.orchestration.terminal:
            raise ValueError("This task already terminated; start a new round instead of resuming")
        self._copy_round_state_to_counters()


@dataclass
class DispatchResult:
    terminal: AgentTerminalDecision
    result: SubagentResult | None = None
    verification: SlowDecision | None = None


class DesktopOrchestrationPorts:
    """A single bundle implements the existing Observation/Prompt/Model/Dispatch ports."""

    def __init__(self, service, store, coordinator, options, registry: SubagentRegistry) -> None:
        self.service, self.store, self.coordinator = service, store, coordinator
        self.options, self.registry = options, registry
        self.observer = DesktopObservationProvider(service.observation_service, service.platform_runtime)
        self.memory = WorkingMemoryStore(store.root)
        config = clone_agent_config_with_hooks(
            service.agent_config,
            before_model_hooks=[],
            after_model_hooks=[],
            after_tool_hooks=[],
        )
        config.tools, config.sub_agents, config.mcp_servers = [], {}, []
        config.middlewares = []
        config.before_model_hooks, config.after_model_hooks = [], []
        config.before_tool_hooks, config.after_tool_hooks = [], []
        config.global_storage = {
            **(config.global_storage or {}),
            SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY: True,
        }
        self.model = AgentKitModelStepExecutor(
            AgentKitModelStepComponents(
                app_config=service.app_config,
                agent_config=config,
                execution_transport=service.execution_transport,
                user_id=service.user_id,
                transport_capabilities=service.transport_capabilities,
                tool_surface_service=service.tool_surface_service,
            )
        )
        self.pipeline = PortStepPipelineExecutor(
            self,
            self,
            self,
            self,
            self.build_result,
            lambda _c, _m, dispatched: dispatched.terminal,
        )
        self.observation = None
        self.cycle_id = ""

    @property
    def state(self) -> OrchestrationState:
        state = self.coordinator.state.orchestration
        if state is None:
            raise RuntimeError("Orchestration state must be hydrated before execution")
        return state

    @property
    def journal(self) -> Journal:
        return Journal(self.store.query_dir() / "orchestration.jsonl")

    @property
    def budget(self) -> RunBudget:
        return RunBudget(self.service.app_config.orchestration, self.state, self.journal)

    async def __call__(self, context):
        self.cycle_id, self.observation = uuid4().hex, None
        self.store.set_query_index(context.cursor.round_num)
        deadline = None
        try:
            self.budget.check()
            if self.state.usage.model_calls >= self.budget.config.max_model_calls:
                raise BudgetExceeded("global_model_calls_limit")
            deadline = asyncio.timeout(self.budget.seconds_left())
            async with deadline:
                return await self.pipeline(context)
        except asyncio.CancelledError:
            with suppress(Exception):
                self.journal.write("cycle_cancelled", self.state, cycle_id=self.cycle_id)
            raise
        except BudgetExceeded as exc:
            decision = AgentTerminalDecision.fail(
                reason=AgentTerminalReason.BUDGET_EXHAUSTED,
                final_answer=str(exc) or "wall_time_limit",
                metadata={"unverified": True},
            )
        except TimeoutError as exc:
            if deadline is not None and deadline.expired():
                decision = AgentTerminalDecision.fail(
                    reason=AgentTerminalReason.BUDGET_EXHAUSTED,
                    final_answer="wall_time_limit",
                    metadata={"unverified": True},
                )
            else:
                decision = AgentTerminalDecision.error(
                    final_answer=f"{type(exc).__name__}: {exc}", retryable=False,
                )
        except Exception as exc:
            decision = AgentTerminalDecision.error(final_answer=f"{type(exc).__name__}: {exc}", retryable=False)
        self.journal.write("cycle_failed", self.state, cycle_id=self.cycle_id, reason=decision.final_answer)
        result = self.build_result(context, None, DispatchResult(decision))
        return AgentRuntimeStepOutcome(context.cursor, result, decision)

    async def collect(self, context):
        self.budget.check()
        # A previous attempt may have changed the environment before interruption.
        # Observe again before the Slow model considers any further dispatch.
        self.observation = await self.capture(context)
        if self.state.pending is not None:
            pending = self.state.pending
            previous = self.state.previous_result
            if not (
                previous is not None and previous.attempt_id == pending.attempt_id
                and previous.exit_reason == "unsafe_to_replay"
            ):
                self.state.previous_result = SubagentResult(
                    subgoal_id=pending.contract.subgoal_id,
                    attempt_id=pending.attempt_id,
                    outcome="blocked",
                    exit_reason="interrupted",
                    summary="Prior attempt requires fresh verification",
                    trace_ref=str(self.journal.path),
                    usage=self.state.pending_usage.model_copy(),
                )
            self.state.pending = None
            self.state.pending_usage = Usage()
            self.journal.write("dispatch_interrupted", self.state)
        return self.observation

    async def capture(self, context):
        from pathlib import Path

        # Screenshots are reads: a transient observation failure may be retried,
        # but never redispatch the action which preceded it or reuse an old image.
        for attempt in range(3):
            try:
                observation = await self.observer.collect(
                    session_step=context.cursor.session_step,
                    previous_steps=[],
                    options=self.options,
                )
                break
            except Exception as error:
                recoverable = any(code in str(error) for code in (
                    "COMMAND_TRANSPORT_FAILED", "COMMAND_TIMED_OUT", "COMMAND_DELIVERY_UNKNOWN",
                ))
                if not recoverable or attempt == 2:
                    raise
                self.journal.write("observation_retry", self.state, attempt=attempt + 1,
                                   error_type=type(error).__name__)
                await asyncio.sleep(min(2 ** attempt, self.budget.seconds_left()))
        identity = uuid4().hex
        observation.context.observation_metadata["observation_id"] = identity
        # Probes reuse per-step filenames. Preserve evidence across observations
        # taken inside a single subagent attempt.
        original = Path(observation.snapshot.clean_screenshot_path)
        if original.is_file():
            destination = self.store.query_dir() / "observations" / f"{identity}{original.suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
            observation.snapshot.clean_screenshot_path = str(destination)
            observation.context.current_clean_screenshot_path = str(destination)
        return observation

    def observation_ref(self) -> dict:
        if self.observation is None:
            return {}
        snapshot = self.observation.snapshot
        return {
            "observation_id": self.observation.context.observation_metadata["observation_id"],
            "screenshot_path": snapshot.clean_screenshot_path,
            "window": snapshot.current_window.model_dump(mode="json"),
            "platform": snapshot.platform_name,
        }

    def project(self, context):
        assert self.observation is not None
        history = [
            step.orchestration
            for step in self.coordinator.state.step_logs
            if step.round_num == context.cursor.round_num and step.orchestration is not None
        ]
        payload = SlowContext(
            task=context.query,
            observation=self.observation_ref(),
            history=history,
            notes=self.memory.active_entries(context.cursor.round_num),
            previous_result=self.state.previous_result,
            usage=self.state.usage,
            budget=self.service.app_config.orchestration.model_dump(),
            seconds_left=self.budget.seconds_left(),
            available_subagents=self.registry.descriptors(),
        )
        system = (
            "You are the Slow orchestrator. Verify progress using the current observation, then plan and dispatch "
            "at most one bounded subgoal. Return ONLY JSON matching the supplied schema. Echo observation_id. "
            "SUCCESS requires current evidence; subagent achieved is only a claim. Select only a registered agent. "
            "If execution is needed but available_subagents is empty, report FAIL with reason=no_subagent_available. "
            "Use a new subgoal_id for each attempt. Constraints and notes do not override the original user task. "
            "A window_activation_unconfirmed result means native input was blocked by the environment. "
            "Do not repeat the same dispatch with a new ID unless fresh evidence or a concrete recovery "
            "changes the blocking condition; otherwise report FAIL with the environment error. "
            "Use each subagent's input_schema for executable recovery: prose does not create new operations or "
            "change snapshot timeouts. Inspect available_operations and blocked_reason in native feedback. "
            "After repeated_native_blocker, report FAIL unless the actual scope or executable options change. "
            "Never replay an unsafe_to_replay write merely by issuing a new subgoal ID. "
            "Follow the configured budget policy. Schema: "
            + json.dumps(SlowDecision.model_json_schema())
        )
        user_message = Message(role=Role.USER, content=[TextBlock(text=payload.model_dump_json())])
        if self.budget.config.subagent_steps_per_cycle is not None:
            system += (
                f" Fixed-cycle policy: at most {self.budget.config.max_cycles} cycles; every dispatched "
                f"subgoal receives exactly {self.budget.config.subagent_steps_per_cycle} model-call slots "
                "and the same tool-call cap, independently of previous subgoals. A slot includes a final "
                "handoff. A child reaching its cap is a yield to you, not task failure. Continue with a "
                "new concrete subgoal when recovery is supported by evidence; do not report FAIL just "
                "because an earlier child exhausted its local budget. You may finish early on verified "
                "success or a demonstrated unrecoverable blocker. The last cycle permits a dispatch "
                "and includes fresh terminal verification afterward, without another dispatch."
            )
        else:
            system += (
                " Give each subgoal at least budget.min_subagent_steps model calls and tool calls; "
                "larger subgoal budgets are allowed. Global quotas and deadlines take precedence. "
                "Reserve budget for a subsequent verification cycle."
            )
        image = self.observation.snapshot.clean_screenshot_url
        if image:
            user_message.content.append(ImageBlock(url=image))
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=system)]), user_message]

    async def _infer_step(self, messages, charge, context, agent_path, tool_definitions=None):
        inference_id = uuid4().hex
        messages, tool_definitions = deepcopy(messages), deepcopy(tool_definitions)
        empty_attempts = self.budget.config.empty_response_retries + 1
        timeout_attempts = self.budget.config.model_timeout_retries + 1
        # Both failure types share one bounded attempt pool. A mixed sequence
        # must not multiply retries or consume future child allocations.
        attempts = max(empty_attempts, timeout_attempts)
        for attempt in range(1, attempts + 1):
            capture = ChatResponseCapture(
                self.store.root / "provider_responses.jsonl",
                {
                    "session_id": context.cursor.session_id,
                    "session_step": context.cursor.session_step,
                    "round_num": context.cursor.round_num,
                    "round_step": context.cursor.round_step,
                    "cycle_id": self.cycle_id,
                    "agent_path": agent_path,
                    "inference_id": inference_id,
                },
                attempt,
            )
            try:
                with capture_chat_responses(capture):
                    result = await self._model_attempt(
                        deepcopy(messages), charge, context, agent_path, deepcopy(tool_definitions)
                    )
            except _ChatResponseAbort as abort:
                await asyncio.to_thread(
                    capture.record_failure, abort.error, getattr(abort, "model_step_trace_state", None),
                )
                if not abort.retryable or attempt >= empty_attempts:
                    raise abort.error from None
                self.journal.write(
                    "empty_response_retry", self.state, inference_id=inference_id,
                    attempt=attempt, max_attempts=attempts, agent_path=agent_path,
                    finish_reason=chat_finish_reason(capture.last_response),
                )
                # Re-enter the existing provider guard: each retry must charge and
                # respect global/local deadlines, call limits and verification reserve.
                continue
            except Exception as error:
                await asyncio.to_thread(
                    capture.record_failure, error, getattr(error, "model_step_trace_state", None),
                )
                if isinstance(error, ModelStepLLMTimeoutError) and attempt < timeout_attempts:
                    self.journal.write(
                        "model_timeout_retry", self.state, inference_id=inference_id,
                        attempt=attempt, max_attempts=timeout_attempts, agent_path=agent_path,
                    )
                    # Model execution is externalized: no environment tool has
                    # been dispatched by this failed inference. The next send
                    # re-enters charge(), including local/global/deadline guards.
                    continue
                raise
            final_api = getattr(result.trace_state.llm_config, "api_type", None) or result.trace_state.provider_family
            if capture.last_response and final_api == "openai_chat_completion":
                result.trace_state.stop_reason = chat_finish_reason(capture.last_response)
                result.trace_state.response_payload = {
                    **result.trace_state.response_payload, "provider_response": capture.last_response,
                    "inference_id": inference_id, "attempt": attempt,
                }
            trace = self.service.trace_capture_service.build_trace_record(result.trace_state)
            self.service.step_persister.persist_llm_trace(
                session_id=context.cursor.session_id,
                session_step=context.cursor.session_step,
                round_num=context.cursor.round_num,
                round_step=context.cursor.round_step,
                request=context.query,
                llm_trace=trace,
                agent_path=agent_path,
            )
            return result
        raise AssertionError("unreachable model retry state")

    async def _model_attempt(self, messages, charge, context, agent_path, tool_definitions):
        from ..subagents.general_model import configure_tools

        return await self.model.run_step(
            agent_kind="orchestrator",
            session_id=context.cursor.session_id,
            step_index=context.cursor.session_step,
            build_envelope=lambda tools, _choice: (
                self._model_envelope(messages, tools) if tool_definitions is None else messages
            ),
            envelope_messages=lambda envelope: envelope,
            before_provider_call=charge,
            configure_agent=(lambda config: configure_tools(config, tool_definitions)) if tool_definitions else None,
            context={
                "task_id": f"round-{context.cursor.round_num}",
                "round_num": context.cursor.round_num,
                "logical_agent_path": agent_path,
                "context_artifact_root": str(self.store.root),
            },
        )

    async def infer(self, messages, charge, context, agent_path="orchestrator"):
        result = await self._infer_step(messages, charge, context, agent_path)
        if result.trace_state.assistant_tool_calls or result.trace_state.tool_executions:
            raise RuntimeError("Slow model attempted tool execution")
        return result.response_text

    @staticmethod
    def _model_envelope(messages, tools):
        if tools:
            raise RuntimeError("Slow model must have an empty provider tool surface")
        return messages

    async def run_model_step(self, context):
        messages = list(context.prompt)
        current = self.observation_ref()
        for attempt in range(3):
            response = await self.infer(messages, lambda: self.budget.charge("model_calls"), context)
            try:
                decision = SlowDecision.model_validate_json(response)
                if decision.observation_id != current["observation_id"]:
                    raise ValueError("Slow decision references a stale observation")
                if decision.action == "SUCCESS" and decision.observation_id not in decision.evidence_refs:
                    # An exact reference to this observation's immutable image is
                    # the same evidence, not a reason to discard the entire task.
                    if current.get("screenshot_path") in decision.evidence_refs:
                        decision.evidence_refs.append(current["observation_id"])
                        self.journal.write("evidence_reference_normalized", self.state,
                                           cycle_id=self.cycle_id)
                    else:
                        raise ValueError("SUCCESS must cite the current observation ID")
                return decision
            except (ValidationError, ValueError) as error:
                if attempt == 2:
                    raise
                detail = (
                    json.dumps(error.errors(include_input=False, include_context=False), default=str)
                    if isinstance(error, ValidationError) else str(error)
                )
                self.journal.write("decision_validation_retry", self.state,
                                   cycle_id=self.cycle_id, attempt=attempt + 1, detail=detail)
                messages.extend([
                    Message(role=Role.ASSISTANT, content=[TextBlock(text=response)]),
                    Message(role=Role.USER, content=[TextBlock(text=(
                        "Your decision was rejected before execution. Correct only the decision JSON "
                        "using the supplied schema; do not invent progress or dispatch any tool. "
                        f"Validation: {detail}. Current observation_id: {current['observation_id']}. "
                        "For SUCCESS include that exact ID in evidence_refs."
                    ))]),
                ])
        raise AssertionError("unreachable decision recovery state")

    async def dispatch_tools(self, context, model_result) -> DispatchResult:
        decision = model_result
        if decision.action == "SUCCESS":
            return DispatchResult(
                AgentTerminalDecision.finish(
                    final_answer=decision.summary,
                    metadata={"verification": decision.verification, "evidence_refs": decision.evidence_refs},
                )
            )
        if decision.action == "FAIL":
            if decision.reason == "no_subagent_available":
                return DispatchResult(
                    AgentTerminalDecision.error(
                        reason=AgentTerminalReason.CONFIGURATION_ERROR,
                        final_answer="no_subagent_available",
                        retryable=False,
                    )
                )
            return DispatchResult(
                AgentTerminalDecision.fail(
                    reason=AgentTerminalReason.MODEL_REPORTED_FAILURE,
                    final_answer=decision.summary,
                )
            )
        contract = decision.contract
        try:
            agent = self.registry.resolve(contract)
        except ValueError as exc:
            return DispatchResult(
                AgentTerminalDecision.error(
                    reason=AgentTerminalReason.CONFIGURATION_ERROR,
                    final_answer=str(exc),
                    retryable=False,
                )
            )
        if self.state.usage.model_calls >= self.budget.config.max_model_calls:
            raise BudgetExceeded("verification_model_budget_required")
        step_budget = context.metadata.get("step_budget", {})
        fixed_steps = self.budget.config.subagent_steps_per_cycle
        last_cycle = step_budget.get("remaining_round_steps", 2) <= 1
        if last_cycle and fixed_steps is None:
            raise BudgetExceeded("verification_cycle_required")
        if contract.subgoal_id in self.state.attempted_subgoals:
            raise ValueError("subgoal_already_attempted; observe and use a new contract")
        minimum = self.budget.config.min_subagent_steps
        contract.budget = contract.budget.model_copy(update={
            "max_model_calls": fixed_steps or max(contract.budget.max_model_calls, minimum),
            "max_tool_calls": fixed_steps or max(contract.budget.max_tool_calls, minimum),
        })
        request = SubagentRequest(
            session_id=context.cursor.session_id,
            round_num=context.cursor.round_num,
            attempt_id=uuid4().hex,
            task=context.query,
            contract=contract,
            observation=self.observation_ref(),
            notes=self.memory.active_entries(context.cursor.round_num),
            history=[
                step.orchestration.model_dump(mode="json")
                for step in self.coordinator.state.step_logs
                if step.round_num == context.cursor.round_num and step.orchestration is not None
            ],
        )

        async def observe():
            observation = await self.capture(context)
            return {
                "observation_id": observation.context.observation_metadata["observation_id"],
                "screenshot_path": observation.snapshot.clean_screenshot_path,
                "window": observation.snapshot.current_window.model_dump(mode="json"),
                "platform": observation.snapshot.platform_name,
            }

        interrupted_general_result = None

        async def execute(name, arguments):
            nonlocal interrupted_general_result
            if contract.agent_id == "general":
                try:
                    return await agent.environment.execute(
                        name, arguments, max_seconds=scoped.remaining_budget().max_seconds,
                    )
                except asyncio.CancelledError as error:
                    delivery = getattr(error, "general_delivery_result", None)
                    if delivery is not None:
                        interrupted_general_result = SubagentResult(
                            subgoal_id=contract.subgoal_id,
                            attempt_id=request.attempt_id,
                            outcome="blocked",
                            exit_reason="unsafe_to_replay",
                            summary=json.dumps({
                                "message": "Execution interrupted; observe before any further action",
                                "unresolved_delivery": True,
                                "tool_results": [{"tool": name, "result": delivery}],
                            }, ensure_ascii=False),
                            usage=scoped.usage.model_copy(),
                            trace_ref=str(self.journal.path),
                        )
                        self.state.previous_result = interrupted_general_result
                        self.journal.write("general_delivery_interrupted", self.state)
                    raise
            if name == "agent_desktop":
                environment = getattr(agent, "environment", None)
                if contract.agent_id != "gui_jev" or environment is None:
                    raise ValueError("agent_desktop requires the configured gui_jev environment")
                return await environment.execute(arguments)
            payload = CommandRequestPayload(
                session_id=request.session_id,
                round_num=request.round_num,
                round_step=context.cursor.round_step,
                metadata={
                    **coordinate_policy_context_values(
                        CoordinateTarget.DESKTOP,
                        policy_from_config(self.service.app_config.desktop.coordinate_policy),
                    )
                },
                # First-phase actions are explicitly bound to the task desktop.
                # Unknown tools must not resolve to controller-host shell servers.
                commands=[
                    CommandRequestRecord(
                        tool_name=name,
                        server_name="DesktopNativeActionExecutor",
                        parameters=arguments,
                        call_id=uuid4().hex,
                    )
                ],
            )
            reply = await self.service.local_device_executor.execute_command_request(payload)
            return reply.model_dump(mode="json")

        async def infer(prompt, charge):
            return await self.infer(
                [Message(role=Role.USER, content=[TextBlock(text=prompt)])],
                charge,
                context,
                agent_path=f"subagents/{contract.agent_id}/{request.attempt_id}",
            )

        async def infer_tools(messages, tools, charge):
            from ..subagents.general_model import assistant_message

            result = await self._infer_step(
                messages,
                charge,
                context,
                agent_path=f"subagents/{contract.agent_id}/{request.attempt_id}",
                tool_definitions=tools,
            )
            return assistant_message(result)

        async def cleanup(name, arguments):
            environment = getattr(agent, "environment", None)
            if name != "agent_desktop" or contract.agent_id != "gui_jev" or environment is None:
                raise ValueError("no_cleanup_handler")
            return await environment.execute(arguments, cleanup=True)

        scoped = ScopedRuntime(
            request, self.budget, observe, execute, infer, cleanup,
            reserve_model_calls=1, infer_tools=infer_tools,
        )
        self.state.pending = request
        self.state.pending_usage = scoped.usage
        self.state.attempted_subgoals.append(contract.subgoal_id)
        self.journal.write("dispatch_started", self.state, cycle_id=self.cycle_id)
        try:
            async with asyncio.timeout(scoped.remaining_budget().max_seconds):
                result = SubagentResult.model_validate(await agent.run(request, scoped))
            if result.subgoal_id != contract.subgoal_id or result.attempt_id != request.attempt_id:
                raise ValueError("subagent_result_identity_mismatch")
        except asyncio.CancelledError:
            with suppress(Exception):
                self.journal.write("dispatch_cancelled", self.state, cycle_id=self.cycle_id)
            raise
        except Exception as exc:
            partial = (
                interrupted_general_result or getattr(exc, "subagent_result", None)
                or getattr(exc.__cause__, "subagent_result", None)
            )
            result = partial if (
                isinstance(partial, SubagentResult)
                and partial.subgoal_id == contract.subgoal_id and partial.attempt_id == request.attempt_id
            ) else SubagentResult(
                subgoal_id=contract.subgoal_id,
                attempt_id=request.attempt_id,
                outcome="failed",
                exit_reason=type(exc).__name__,
                summary=str(exc),
            )
        finally:
            abandoned_operations = await scoped.close()
        if abandoned_operations:
            result.outcome, result.exit_reason = "yielded", "background_operations_cancelled"
        result.usage = scoped.usage.model_copy()
        result.last_observation = scoped.last_observation
        result.trace_ref = str(self.journal.path)
        self.state.previous_result, self.state.pending = result, None
        self.state.pending_usage = Usage()
        self.journal.write("dispatch_finished", self.state, cycle_id=self.cycle_id)
        if fixed_steps is not None and last_cycle:
            # Verify within this transaction: twenty cycles can actually dispatch
            # twenty subgoals, without a hidden twenty-first action cycle.
            self.observation = await self.capture(context)
            messages = self.project(context)
            payload = json.loads(messages[1].content[0].text)
            payload["history"].append(CycleRecord(
                cycle_id=self.cycle_id, observation_id=decision.observation_id,
                decision=decision, subagent_result=result, usage=self.state.usage.model_copy(),
            ).model_dump(mode="json"))
            messages[1].content[0] = TextBlock(text=json.dumps(payload, ensure_ascii=False))
            messages[0].content.append(TextBlock(text=(
                "This is the terminal verification of the final execution cycle. Inspect the fresh "
                "observation and previous_result. Return SUCCESS or FAIL with current evidence. "
                "No further subgoal can be dispatched; do not claim success merely from a child claim."
            )))
            verification = await self.run_model_step(replace(context, prompt=messages))
            self.journal.write("final_verification", self.state, cycle_id=self.cycle_id,
                               decision=verification.model_dump(mode="json"))
            if verification.action == "CONTINUE":
                terminal = AgentTerminalDecision.fail(
                    reason=AgentTerminalReason.BUDGET_EXHAUSTED,
                    final_answer="cycle_limit_after_final_verification",
                    metadata={"unverified": True},
                )
            else:
                terminal = (await self.dispatch_tools(context, verification)).terminal
            return DispatchResult(terminal, result, verification)
        return DispatchResult(AgentTerminalDecision.continue_(), result)

    def build_result(self, context, decision, dispatched):
        terminal = dispatched.terminal
        record = CycleRecord(
            cycle_id=self.cycle_id,
            observation_id=decision.observation_id if decision else self.observation_ref().get("observation_id", ""),
            decision=decision,
            final_verification=dispatched.verification,
            subagent_result=dispatched.result,
            usage=self.state.usage.model_copy(),
            exit_reason=terminal.reason.value,
        )
        snapshot = self.observation.snapshot if self.observation else self.coordinator.state.latest_snapshot
        return StepLogRecord(
            session_id=context.cursor.session_id,
            round_num=context.cursor.round_num,
            session_step=context.cursor.session_step,
            round_step=context.cursor.round_step,
            status=TaskStatus(terminal.action.value),
            request=context.query,
            result=terminal.final_answer,
            assistant_text=decision.summary if decision else terminal.final_answer,
            terminal_decision={
                **terminal.to_json(),
                "metadata": {
                    **terminal.metadata,
                    "step_unit": "orchestration_cycle",
                    "usage": record.usage.model_dump(),
                },
            },
            current_window=snapshot.current_window,
            platform_name=snapshot.platform_name,
            clean_screenshot_path=snapshot.clean_screenshot_path,
            llm_trace_path=str(self.store.llm_trace_log_path),
            orchestration=record,
        )

    async def persist_step(self, context, result):
        self.state.terminal = result.status != TaskStatus.CONTINUE
        await self.service.step_persister.persist_step(
            step_log=result,
            snapshot=self.observation.snapshot if self.observation else self.coordinator.state.latest_snapshot,
            duration_ms=0,
            terminal_summary=result.result,
            advance_counters=False,
            save_state=False,
        )
        # SQLite notes must not become durable before their source cycle.
        with self.store.step_log_path.open("rb") as committed_step:
            os.fsync(committed_step.fileno())
        notes = result.orchestration.decision.notes if result.orchestration.decision else []
        self.memory.record_notes(
            notes,
            round_num=context.cursor.round_num,
            session_step=context.cursor.session_step,
            source=result.orchestration.cycle_id,
        )
        self.journal.write("cycle_persisted", self.state, cycle_id=self.cycle_id)

    async def project_terminal(self, context):
        # Budget exhaustion rewrites the last cycle; it never dispatches another agent.
        steps = list(context.steps) or [
            step for step in self.coordinator.state.step_logs if step.round_num == self.state.round_num
        ]
        if not steps:
            if context.cursor is None:
                return None
            self.cycle_id = uuid4().hex
            step_context = AgentRuntimeStepContext(cursor=context.cursor, query=context.query)
            step = self.build_result(step_context, None, DispatchResult(context.decision))
            assert step.orchestration is not None
            step.orchestration.synthetic = True
            await self.persist_step(step_context, step)
            await self.service.state_store.save(self.coordinator.state)
            return AgentRuntimeTerminalProjection(step)
        step = steps[-1].model_copy(deep=True)
        step.status, step.result = TaskStatus.FAIL, context.decision.final_answer
        step.terminal_decision = {
            **context.decision.to_json(),
            "metadata": {
                **context.decision.metadata,
                "step_unit": "orchestration_cycle",
                "unverified": True,
                "usage": self.state.usage.model_dump(),
            },
        }
        self.coordinator.state.step_logs[-1] = step
        if step.orchestration is not None:
            step.orchestration.exit_reason = context.decision.reason.value
        self.state.terminal = True
        await self.service.step_persister.rewrite_step_logs_after_terminal_metadata(
            session_id=step.session_id,
            session_step=step.session_step,
        )
        self.journal.write("cycle_limit", self.state)
        await self.service.state_store.save(self.coordinator.state)
        return AgentRuntimeTerminalProjection(step)
