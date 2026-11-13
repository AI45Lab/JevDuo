"""Scripted orchestration integration: real runtime/state/ports, no VM or hosted model."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent_context import GlobalStorage
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.execution.hooks import BeforeModelHookInput
from northcua.agentinfra.context.adapters.provider_guard import (
    ProviderContextBudgetGuard,
    install_provider_context_budget_guard,
)
from northcua.agentinfra.runtime.protocol import DesktopRunRequest
from northcua.agentinfra.tracing.log_writer import LLMTraceLogWriter
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkernel.tracing.capture import TraceCaptureService
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.agentkernel.working_memory import WorkingMemoryStore
from northcua.agentkit.orchestration.contracts import (
    Note,
    SlowDecision,
    SubagentRegistry,
    SubagentRequest,
    SubagentResult,
    SubagentSpec,
    SubgoalContract,
)
from northcua.agentkit.orchestration.runtime import BudgetExceeded, Journal, ScopedRuntime, read_records
from northcua.agentkit.runtime import AgentRoundRuntimeSpec
from northcua.agents.desktop.facade import NorthCUAAgentFacade
from northcua.agents.desktop.round_execution.step_persistence import RoundStepPersister
from northcua.agents.desktop.runtime.contracts import DesktopRuntimeOptions
from northcua.agents.desktop.runtime.factory import DesktopRuntimeFactory
from northcua.agents.desktop.runtime.orchestration import DispatchResult
from northcua.agents.desktop.services.session_state import RoundStateStore
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.core.budget import AgentStepBudget
from northcua.core.config import NorthCUAAppConfig
from northcua.core.coordinates import CoordinateTarget, coordinate_policy_context_values, policy_from_config
from northcua.core.enums import CoordinateSpace, ResultStatus
from northcua.core.models import CommandExecutionResultRecord, ObservationContext
from northcua.core.protocol import CommandResultPayload
from northcua.core.terminal import AgentTerminalDecision
from northcua.platforms.desktop import DesktopPlatformRuntime
from northcua.platforms.dispatch import PlatformCommandExecutor
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.desktop import DesktopObservationService
from northcua.transport.ws.message_seed import SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY, visible_runtime_mcp_servers

import appcfg
import launch
import recorder


def contract(**overrides):
    return SubgoalContract.model_validate(
        {
            "subgoal_id": "open-dialog",
            "agent_id": "fake",
            "goal": "Open dialog",
            "success_criteria": ["Dialog visible"],
            "allowed_tools": ["click"],
            "budget": {"max_model_calls": 2, "max_tool_calls": 1, "max_seconds": 5},
            **overrides,
        }
    )


class Sessions:
    session = None

    async def get_session(self, **kwargs):
        return self.session

    async def update_session_state(self, *, context, storage, **kwargs):
        self.session = SimpleNamespace(context=context, storage=storage)


class Device:
    def __init__(self):
        self.calls = []

    async def execute_command_request(self, payload):
        self.calls.append(payload)
        return CommandResultPayload(
            session_id=payload.session_id,
            status=ResultStatus.SUCCESS,
            results=[
                CommandExecutionResultRecord(
                    call_id=payload.commands[0].call_id,
                    status=ResultStatus.SUCCESS,
                    result={"dialog": "open"},
                ),
            ],
        )


class Observer:
    def __init__(self):
        self.calls = 0

    async def collect(self, session_step, previous_steps):
        self.calls += 1
        return ObservationContext(
            platform_name="test-vm", current_clean_screenshot_path=f"shot-{self.calls}.png"
        )


class ScriptedModel:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.contexts = []

    async def run_step(self, **kwargs):
        kwargs["before_provider_call"]()
        messages = kwargs["build_envelope"]([], None)
        prompt = messages[-1].content[0].text
        if not prompt.startswith("{"):
            response = "local model response"
        else:
            payload = json.loads(prompt)
            self.contexts.append(payload)
            action = next(self.actions)
            response = json.dumps(
                {
                    "action": action["action"],
                    "summary": "scripted",
                    "verification": "inspect current image",
                    "observation_id": payload["observation"]["observation_id"],
                    "evidence_refs": [payload["observation"]["observation_id"]],
                    **action,
                }
            )
        return SimpleNamespace(response_text=response, trace_state=TraceCaptureState(assistant_text=response))


class FakeSubagent:
    spec = SubagentSpec("fake", "Test only", required_tools=("click",))

    def __init__(self, behavior="normal"):
        self.calls = 0
        self.behavior = behavior
        self.handle = None

    async def run(self, request, runtime):
        self.calls += 1
        self.handle = runtime
        await runtime.execute("click", {"x": 1})
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        if self.behavior == "error":
            raise OSError("lost reply")
        if self.behavior == "overrun":
            await runtime.execute("click", {"x": 2})
        if self.behavior == "normal":
            await runtime.observe()
            await runtime.infer("check the dialog")
        return SubagentResult(
            subgoal_id=request.contract.subgoal_id,
            attempt_id=request.attempt_id,
            outcome="achieved",
            exit_reason="criteria_claimed",
            summary="Dialog open",
            evidence_refs=["dialog-screenshot"],
            proposed_notes=[Note(key="dialog", text="open")],
        )


class ScriptedTransport:
    """Exercise the real model executor, tool projection and trace hooks without HTTP."""

    def __init__(self, app_config):
        self.app_config = app_config
        self.prepared = []
        self.provider_calls = 0

    async def prepare_agent(self, *, agent_config, **kwargs):
        visible = visible_runtime_mcp_servers(
            raw_agent_mcp_servers=self.app_config.agent_mcp_servers,
            agent_config=agent_config,
            runtime_visibility_filter=lambda servers: servers,
        )
        if visible or agent_config.tools or agent_config.sub_agents:
            raise AssertionError("Environment tools leaked into Slow")
        self.prepared.append(agent_config)
        return SimpleNamespace(
            config=agent_config,
            tool_call_payload=[],
            global_storage={},
            executor=SimpleNamespace(
                update_structured_tools=lambda tools: None,
                llm_caller=SimpleNamespace(
                    _call_with_retry=self.call_model,
                    _call_with_retry_async=self.call_model_async,
                ),
            ),
        )

    async def run_local_round(self, *, messages, agent_config, agent, **kwargs):
        hook_input = BeforeModelHookInput(
            agent_state=SimpleNamespace(),
            messages=messages,
            max_iterations=1,
            current_iteration=1,
        )
        for hook in agent_config.before_model_hooks:
            hook(hook_input)
        response = agent.executor.llm_caller._call_with_retry(
            SimpleNamespace(messages=messages, api_params={})
        )
        return (response, {}), agent

    def call_model(self, params):
        self.provider_calls += 1
        payload = json.loads(params.messages[-1].content[0].text)
        identity = payload["observation"]["observation_id"]
        response = json.dumps(
            {
                "action": "SUCCESS",
                "summary": "current image verifies task",
                "observation_id": identity,
                "verification": "scripted visual outcome",
                "evidence_refs": [identity],
            }
        )
        return response

    async def call_model_async(self, params):
        return self.call_model(params)


class OrchestrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = NorthCUAAppConfig(
            agent_config_path="unused",
            evaluation={"enabled": False},
            orchestration={"enabled": True, "max_cycles": 4, "min_subagent_steps": 0},
        )
        self.store = NorthCUAArtifactStore(self.root, self.config.artifacts, session_id="test")
        self.rounds = NorthCUARoundCoordinator("test")
        self.rounds.state.round_num, self.rounds.state.latest_request = 1, "open the dialog"
        self.sessions = Sessions()
        self.state_store = RoundStateStore(self.sessions, user_id="test")
        await self.state_store.save(self.rounds.state, storage=GlobalStorage())
        self.observer, self.device = Observer(), Device()
        llm = LLMConfig(
            model="scripted",
            api_key="test-only",
            api_type="openai_chat_completion",
            base_url="http://unused.invalid/v1",
        )
        agent_config = AgentConfig(name="scripted", llm_config=llm)
        self.service = SimpleNamespace(
            app_config=self.config,
            agent_config=agent_config,
            state_store=self.state_store,
            observation_service=DesktopObservationService(coordinator=self.observer),
            platform_runtime=None,
            execution_transport=None,
            user_id="test",
            transport_capabilities=None,
            tool_surface_service=ToolSurfaceService(),
            trace_capture_service=TraceCaptureService(llm),
            local_device_executor=self.device,
        )
        self.service.step_persister = RoundStepPersister(
            artifact_store=self.store,
            state_store=self.state_store,
            round_coordinator=self.rounds,
            session_manager=self.sessions,
            user_id="test",
            llm_trace_writer=LLMTraceLogWriter(self.store.llm_trace_log_path),
        )
        self.factory = DesktopRuntimeFactory(self.store, self.rounds, self.service, None)

    def make_runtime(self, actions):
        runtime = self.factory.build_round_runtime(options=DesktopRuntimeOptions())
        ports = runtime.step_runtime.step_executor
        self.real_model = ports.model
        self.model = ScriptedModel(actions)
        ports.model = self.model
        return runtime

    async def run_script(self, actions):
        runtime = self.make_runtime(actions)
        return await runtime.run(
            AgentRoundRuntimeSpec(
                query="open the dialog",
                budget=AgentStepBudget(max_round_steps=self.config.orchestration.max_cycles),
                fallback_round_steps=4,
                begin_new_round=False,
            )
        )

    async def test_empty_pool_is_configuration_error(self):
        result = await self.run_script([{"action": "FAIL", "reason": "no_subagent_available"}])
        self.assertEqual(result.terminal_decision.reason.value, "configuration_error")
        self.assertEqual(result.terminal_decision.final_answer, "no_subagent_available")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(self.device.calls, [])
        self.assertEqual(self.rounds.state.orchestration.usage.model_calls, 1)

    async def test_subagent_budget_floor_is_enforced_and_persisted(self):
        self.assertEqual(NorthCUAAppConfig(agent_config_path="unused").orchestration.min_subagent_steps, 30)
        self.config.orchestration.min_subagent_steps = 30
        agent = FakeSubagent()
        self.factory.subagents.register(agent)
        result = await self.run_script([
            {"action": "CONTINUE", "contract": contract(budget={
                "max_model_calls": 18, "max_tool_calls": 24, "max_seconds": 5,
            }).model_dump()},
            {"action": "SUCCESS"},
        ])
        budget = agent.handle.request.contract.budget
        self.assertEqual((budget.max_model_calls, budget.max_tool_calls), (30, 30))
        persisted = next(read_records(self.store.step_log_path))["orchestration"]["decision"]["contract"]
        self.assertEqual(persisted["budget"], budget.model_dump())
        self.assertEqual(self.model.contexts[0]["budget"]["min_subagent_steps"], 30)
        self.assertEqual(result.status.value, "success")

    async def test_subagent_budget_floor_preserves_larger_limits_and_global_cap(self):
        self.config.orchestration.min_subagent_steps = 30
        self.config.orchestration.max_model_calls = 2
        agent = FakeSubagent()
        self.factory.subagents.register(agent)
        result = await self.run_script([
            {"action": "CONTINUE", "contract": contract(budget={
                "max_model_calls": 48, "max_tool_calls": 65, "max_seconds": 5,
            }).model_dump()},
            {"action": "SUCCESS"},
        ])
        budget = agent.handle.request.contract.budget
        self.assertEqual((budget.max_model_calls, budget.max_tool_calls), (48, 65))
        self.assertEqual(self.rounds.state.orchestration.usage.model_calls, 2)
        self.assertEqual(self.model.contexts[-1]["previous_result"]["exit_reason"], "BudgetExceeded")
        self.assertEqual(result.status.value, "success")

    async def test_global_failure_bypasses_registry_even_when_pool_is_empty(self):
        result = await self.run_script([{"action": "FAIL", "reason": "infeasible"}])
        self.assertEqual(result.terminal_decision.reason.value, "model_reported_failure")

    async def test_unknown_agent_is_a_configuration_error(self):
        result = await self.run_script([{"action": "CONTINUE", "contract": contract().model_dump()}])
        self.assertEqual(result.terminal_decision.reason.value, "configuration_error")
        self.assertEqual(self.device.calls, [])

    async def test_complete_cycle_uses_fresh_verification_and_single_persistence(self):
        agent = FakeSubagent()
        self.factory.subagents.register(agent)
        result = await self.run_script(
            [
                {"action": "CONTINUE", "contract": contract().model_dump()},
                {"action": "SUCCESS", "notes": [{"key": "verified", "text": "dialog open"}]},
            ]
        )
        self.assertEqual(result.status.value, "success")
        self.assertEqual((agent.calls, len(self.device.calls), len(result.steps)), (1, 1, 2))
        self.assertEqual(self.observer.calls, 3)  # Initial, plugin observe, subsequent Slow verification.
        self.assertEqual(len(list(read_records(self.store.step_log_path))), 2)
        self.assertEqual(self.rounds.state.session_step, 3)
        self.assertEqual(self.model.contexts[1]["previous_result"]["outcome"], "achieved")
        self.assertEqual(self.model.contexts[1]["observation"]["screenshot_path"], "shot-3.png")
        self.assertEqual(self.rounds.state.orchestration.usage.model_calls, 3)
        self.assertEqual(self.rounds.state.orchestration.usage.tool_calls, 1)
        self.assertEqual(self.memory()[0]["key"], "verified")
        self.assertTrue(agent.handle.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await agent.handle.execute("click", {})

    def memory(self):
        return WorkingMemoryStore(self.root).active_entries(1)

    async def test_cancellation_retains_intent_and_usage_without_replaying(self):
        agent = FakeSubagent("cancel")
        self.factory.subagents.register(agent)
        with self.assertRaises(asyncio.CancelledError):
            await self.run_script([{"action": "CONTINUE", "contract": contract().model_dump()}])
        self.assertIsNotNone(self.rounds.state.orchestration.pending)
        self.assertEqual(len(self.device.calls), 1)
        # Rebuild the runtime, whose hydrate replays the journal over stale session state.
        result = await self.run_script([{"action": "SUCCESS"}])
        self.assertEqual(result.status.value, "success")
        self.assertEqual(agent.calls, 1)
        self.assertEqual(self.model.contexts[0]["previous_result"]["exit_reason"], "interrupted")
        self.assertEqual(self.model.contexts[0]["previous_result"]["usage"]["tool_calls"], 1)
        self.assertEqual(self.rounds.state.orchestration.usage.tool_calls, 1)
        self.assertEqual(self.rounds.state.orchestration.usage.model_calls, 2)

    async def test_subagent_budget_stops_before_second_effect(self):
        agent = FakeSubagent("overrun")
        self.factory.subagents.register(agent)
        result = await self.run_script(
            [
                {"action": "CONTINUE", "contract": contract().model_dump()},
                {"action": "FAIL"},
            ]
        )
        self.assertEqual(len(self.device.calls), 1)
        self.assertEqual(result.steps[0].orchestration.subagent_result.outcome, "failed")
        self.assertEqual(result.steps[0].orchestration.subagent_result.exit_reason, "BudgetExceeded")

    async def test_reserves_a_cycle_for_verification(self):
        self.config.orchestration.max_cycles = 1
        agent = FakeSubagent()
        self.factory.subagents.register(agent)
        result = await self.run_script([{"action": "CONTINUE", "contract": contract().model_dump()}])
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(agent.calls, 0)

    async def test_fixed_policy_runs_twenty_full_children_and_verifies_inside_last_cycle(self):
        self.config.orchestration.max_cycles = 20
        self.config.orchestration.subagent_steps_per_cycle = 50
        self.config.orchestration.max_model_calls = 1200
        self.config.orchestration.max_tool_calls = 1000

        class FullBudgetChild(FakeSubagent):
            async def run(child, request, runtime):
                child.calls += 1
                self.assertEqual(request.contract.budget.max_model_calls, 50)
                self.assertEqual(request.contract.budget.max_tool_calls, 50)
                for _ in range(50):
                    await runtime.infer("bounded child step")
                self.assertEqual(runtime.remaining_budget().max_model_calls, 0)
                return SubagentResult(subgoal_id=request.contract.subgoal_id,
                                      attempt_id=request.attempt_id, outcome="yielded",
                                      exit_reason="model_budget_exhausted", summary="retained findings")

        agent = FullBudgetChild()
        self.factory.subagents.register(agent)
        actions = [{"action": "CONTINUE", "contract": contract(subgoal_id=f"s-{i}").model_dump()}
                   for i in range(20)]
        result = await self.run_script([*actions, {"action": "SUCCESS"}])
        self.assertEqual(agent.calls, 20)
        self.assertEqual(len(result.steps), 20)
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(result.steps[-1].orchestration.usage.model_calls, 1021)
        self.assertEqual(result.steps[-1].orchestration.final_verification.action, "SUCCESS")
        self.assertEqual(self.model.contexts[-1]["previous_result"]["summary"], "retained findings")
        self.assertEqual(len(self.model.contexts[-1]["history"]), 20)
        self.assertEqual(self.model.contexts[-1]["history"][-1]["decision"]["contract"]["subgoal_id"], "s-19")
        self.assertNotEqual(self.model.contexts[-1]["observation"]["observation_id"],
                            self.model.contexts[-2]["observation"]["observation_id"])

    async def test_fixed_last_cycle_cannot_dispatch_an_extra_child(self):
        self.config.orchestration.max_cycles = 1
        self.config.orchestration.subagent_steps_per_cycle = 50
        agent = FakeSubagent()
        self.factory.subagents.register(agent)
        result = await self.run_script([
            {"action": "CONTINUE", "contract": contract().model_dump()},
            {"action": "CONTINUE", "contract": contract(subgoal_id="not-dispatched").model_dump()},
        ])
        self.assertEqual(agent.calls, 1)
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(result.terminal_decision.final_answer, "cycle_limit_after_final_verification")

    async def test_expired_budget_makes_no_model_or_observation_call(self):
        runtime = self.make_runtime([])
        await runtime.session_runtime.hydrate()
        self.rounds.state.orchestration.started_at = time.time() - 1000
        await runtime.session_runtime.save()
        runtime.step_runtime.step_executor.journal.write("expired", self.rounds.state.orchestration)
        result = await runtime.run(
            AgentRoundRuntimeSpec(
                query="open the dialog",
                budget=AgentStepBudget(max_round_steps=4),
                fallback_round_steps=4,
                begin_new_round=False,
            )
        )
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(self.observer.calls, 0)
        self.assertEqual(self.model.contexts, [])

    async def test_completed_task_cannot_be_resumed(self):
        await self.run_script([{"action": "SUCCESS"}])
        with self.assertRaisesRegex(ValueError, "already terminated"):
            await self.run_script([])

    async def test_cancellation_during_first_observation_preserves_start_time(self):
        original_collect = self.observer.collect

        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError()

        self.observer.collect = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.run_script([])
        started = self.rounds.state.orchestration.started_at
        self.observer.collect = original_collect
        await self.run_script([{"action": "SUCCESS"}])
        self.assertEqual(self.rounds.state.orchestration.started_at, started)

    async def test_model_config_removes_all_environment_tool_sources(self):
        self.make_runtime([])
        config = self.real_model.agent_config
        self.assertEqual(config.tools, [])
        self.assertFalse(config.sub_agents)
        self.assertEqual(config.mcp_servers, [])
        self.assertIs(config.global_storage[SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY], True)
        ports = self.factory.build_round_runtime(options=DesktopRuntimeOptions()).step_runtime.step_executor
        with self.assertRaisesRegex(RuntimeError, "empty provider"):
            ports._model_envelope([], [{"name": "click"}])

    async def test_real_model_executor_suppresses_raw_mcp_and_counts_hooks(self):
        self.config.agent_mcp_servers = [{"name": "EnvironmentActionServer"}]
        transport = ScriptedTransport(self.config)
        self.service.execution_transport = transport
        runtime = self.factory.build_round_runtime(options=DesktopRuntimeOptions())
        result = await runtime.run(
            AgentRoundRuntimeSpec(
                query="open the dialog",
                budget=AgentStepBudget(max_round_steps=4),
                fallback_round_steps=4,
                begin_new_round=False,
            )
        )
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(transport.prepared), 1)
        self.assertEqual(self.rounds.state.orchestration.usage.model_calls, 1)
        trace = list(read_records(self.store.llm_trace_log_path))[0]
        self.assertEqual(trace["agent_path"], "orchestrator")
        self.assertFalse(trace["llm_trace"]["request"].get("tools"))

    async def test_observation_files_survive_same_step_probe_overwrites(self):
        original = self.root / "probe.png"

        async def collect(session_step, previous_steps):
            self.observer.calls += 1
            original.write_bytes(f"image {self.observer.calls}".encode())
            return ObservationContext(platform_name="test-vm", current_clean_screenshot_path=str(original))

        self.observer.collect = collect
        self.factory.subagents.register(FakeSubagent())
        result = await self.run_script(
            [
                {"action": "CONTINUE", "contract": contract().model_dump()},
                {"action": "SUCCESS"},
            ]
        )
        first = Path(result.steps[0].clean_screenshot_path)
        inner = Path(result.steps[0].orchestration.subagent_result.last_observation["screenshot_path"])
        verified = Path(result.steps[1].clean_screenshot_path)
        self.assertEqual(
            [path.read_bytes() for path in [first, inner, verified]], [b"image 1", b"image 2", b"image 3"]
        )

    async def test_task_round_history_and_usage_survive_session_roundtrip(self):
        self.factory.subagents.register(FakeSubagent("error"))
        result = await self.run_script(
            [
                {"action": "CONTINUE", "contract": contract().model_dump()},
                {"action": "FAIL"},
            ]
        )
        restored = await self.state_store.load("test")
        self.assertEqual(restored.orchestration.usage.tool_calls, 1)
        self.assertEqual(restored.orchestration.previous_result.exit_reason, "OSError")
        self.assertEqual(restored.step_logs[0].orchestration.subagent_result.outcome, "failed")
        self.assertEqual(result.status.value, "fail")

    async def test_action_routing_and_coordinates_survive_platform_adapters(self):
        self.config.desktop.coordinate_policy.input_space = CoordinateSpace.NORMALIZED_1000
        self.service.local_device_executor = PlatformCommandExecutor(
            DesktopPlatformRuntime(action_executor=self.device),
        )
        self.factory.subagents.register(FakeSubagent("error"))
        await self.run_script(
            [
                {"action": "CONTINUE", "contract": contract().model_dump()},
                {"action": "FAIL"},
            ]
        )
        payload = self.device.calls[0]
        self.assertEqual(payload.session_id, "test")
        self.assertEqual(payload.commands[0].server_name, "DesktopNativeActionExecutor")
        for key, value in coordinate_policy_context_values(
            CoordinateTarget.DESKTOP,
            policy_from_config(self.config.desktop.coordinate_policy),
        ).items():
            self.assertEqual(payload.metadata[key], value)

    async def test_global_model_limit_blocks_next_call(self):
        self.config.orchestration.max_model_calls = 1
        self.factory.subagents.register(FakeSubagent("error"))
        result = await self.run_script(
            [
                {"action": "CONTINUE", "contract": contract().model_dump()},
                {"action": "SUCCESS"},
            ]
        )
        self.assertEqual(result.status.value, "fail")
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(self.rounds.state.orchestration.usage.model_calls, 1)
        self.assertEqual(len(self.model.contexts), 1)

    async def test_completed_main_step_reconciles_stale_terminal_checkpoint(self):
        await self.run_script([{"action": "SUCCESS"}])
        checkpoint = self.rounds.state.orchestration.model_copy(deep=True)
        checkpoint.terminal = False
        ports = self.factory.build_round_runtime(options=DesktopRuntimeOptions()).step_runtime.step_executor
        ports.journal.write("stale_checkpoint", checkpoint)
        with self.assertRaisesRegex(ValueError, "already terminated"):
            await self.run_script([])

    async def test_unsupported_entries_fail_before_observation(self):
        self.config.websocket.runtime_mode = "remote"
        with self.assertRaisesRegex(ValueError, "local Python"):
            self.make_runtime([])
        self.assertEqual(self.observer.calls, 0)

    async def test_terminal_append_survives_crash_before_journal(self):
        runtime = self.make_runtime([{"action": "SUCCESS"}])
        write = Journal.write

        def crash(journal, event, state, **payload):
            if event == "cycle_persisted":
                raise OSError("crash after append")
            return write(journal, event, state, **payload)

        with patch.object(Journal, "write", crash), self.assertRaisesRegex(OSError, "crash after append"):
            await runtime.run(
                AgentRoundRuntimeSpec(
                    query="open the dialog",
                    budget=AgentStepBudget(max_round_steps=4),
                    fallback_round_steps=4,
                    begin_new_round=False,
                )
            )
        with self.assertRaisesRegex(ValueError, "already terminated"):
            await self.run_script([])
        self.assertEqual(len(list(read_records(self.store.step_log_path))), 1)

    async def test_uncommitted_cycle_does_not_activate_notes(self):
        runtime = self.make_runtime([{"action": "SUCCESS", "notes": [{"key": "orphan", "text": "wrong"}]}])
        with patch.object(
            self.service.step_persister, "persist_step", AsyncMock(side_effect=OSError("append failed"))
        ):
            with self.assertRaisesRegex(OSError, "append failed"):
                await runtime.run(
                    AgentRoundRuntimeSpec(
                        query="open the dialog",
                        budget=AgentStepBudget(max_round_steps=4),
                        fallback_round_steps=4,
                        begin_new_round=False,
                    )
                )
        self.assertEqual(self.memory(), [])
        await self.run_script([{"action": "SUCCESS"}])
        self.assertEqual(self.model.contexts[0]["notes"], [])

    async def test_committed_cycle_recovers_notes_after_sqlite_failure(self):
        runtime = self.make_runtime([{"action": "SUCCESS", "notes": [{"key": "verified", "text": "right"}]}])
        with patch.object(WorkingMemoryStore, "record_notes", side_effect=OSError("sqlite failure")):
            with self.assertRaisesRegex(OSError, "sqlite failure"):
                await runtime.run(
                    AgentRoundRuntimeSpec(
                        query="open the dialog",
                        budget=AgentStepBudget(max_round_steps=4),
                        fallback_round_steps=4,
                        begin_new_round=False,
                    )
                )
        self.assertEqual(self.memory(), [])
        with self.assertRaisesRegex(ValueError, "already terminated"):
            await self.run_script([])
        self.assertEqual(self.memory()[0]["text"], "right")

    async def test_truncated_main_log_blocks_resume_before_model_or_observation(self):
        self.store.step_log_path.write_text('{"partial":', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Incomplete JSONL"):
            await self.run_script([])
        self.assertEqual(self.model.contexts, [])
        self.assertEqual(self.observer.calls, 0)

    async def test_whitespace_tail_is_incomplete_but_complete_blank_lines_are_valid(self):
        for tail in [" ", "\t", " \t"]:
            with self.subTest(tail=repr(tail)):
                self.store.step_log_path.write_text(tail, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "Incomplete JSONL"):
                    await self.run_script([])
                self.assertEqual(self.observer.calls, 0)
        self.store.step_log_path.write_text('\n \t\n{"valid": true}\n', encoding="utf-8")
        self.assertEqual(list(read_records(self.store.step_log_path)), [{"valid": True}])

    async def test_preflight_limit_rewrites_recovered_cycle_without_an_extra_record(self):
        self.config.orchestration.max_cycles = 1
        runtime = self.make_runtime([{"action": "CONTINUE", "contract": contract().model_dump()}])
        ports = runtime.step_runtime.step_executor
        ports.dispatch_tools = AsyncMock(return_value=DispatchResult(AgentTerminalDecision.continue_()))
        ports.project_terminal = AsyncMock(side_effect=OSError("crash before rewrite"))
        with self.assertRaisesRegex(OSError, "crash before rewrite"):
            await runtime.run(
                AgentRoundRuntimeSpec(
                    query="open the dialog",
                    budget=AgentStepBudget(max_round_steps=1),
                    fallback_round_steps=1,
                    begin_new_round=False,
                )
            )
        result = await self.run_script([])
        records = list(read_records(self.store.step_log_path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "fail")
        self.assertEqual(records[0]["orchestration"]["exit_reason"], "max_steps")
        self.assertEqual(result.terminal_decision.reason.value, "max_steps")

    async def _background_case(self, cancel):
        entered = asyncio.Event()
        completed = []

        async def delayed_device(payload):
            entered.set()
            await asyncio.Event().wait()
            completed.append(payload)

        self.device.execute_command_request = delayed_device

        class BackgroundAgent(FakeSubagent):
            async def run(self, request, runtime):
                self.handle = runtime
                self.background = asyncio.create_task(runtime.execute("click", {}))
                await entered.wait()
                if cancel:
                    raise asyncio.CancelledError()
                return SubagentResult(
                    subgoal_id=request.contract.subgoal_id,
                    attempt_id=request.attempt_id,
                    outcome="achieved",
                    exit_reason="claim",
                    summary="claimed",
                )

        agent = BackgroundAgent()
        self.factory.subagents.register(agent)
        actions = [{"action": "CONTINUE", "contract": contract().model_dump()}, {"action": "SUCCESS"}]
        if cancel:
            with self.assertRaises(asyncio.CancelledError):
                await self.run_script(actions)
        else:
            result = await self.run_script(actions)
            self.assertEqual(
                result.steps[0].orchestration.subagent_result.exit_reason, "background_operations_cancelled"
            )
        self.assertTrue(agent.background.cancelled())
        self.assertTrue(agent.handle.closed)
        self.assertEqual(completed, [])

    async def test_background_tool_is_drained_before_next_cycle(self):
        await self._background_case(cancel=False)

    async def test_background_tool_is_drained_on_parent_cancellation(self):
        await self._background_case(cancel=True)

    async def test_expired_deadline_after_journal_never_enters_executor(self):
        runtime = self.make_runtime([])
        await runtime.session_runtime.hydrate()
        ports = runtime.step_runtime.step_executor
        request = SubagentRequest(
            session_id="test",
            round_num=1,
            attempt_id="test-attempt",
            task="test",
            contract=contract(),
            observation={},
        )
        executor = AsyncMock(return_value={})
        scoped = ScopedRuntime(request, ports.budget, AsyncMock(), executor, AsyncMock())
        write = Journal.write

        def expire(journal, event, state, **payload):
            write(journal, event, state, **payload)
            if event == "tool_started":
                scoped.deadline = time.monotonic() - 1

        with patch.object(Journal, "write", expire), self.assertRaises(BudgetExceeded):
            await scoped.execute("click", {})
        executor.assert_not_awaited()
        with self.assertRaises(BudgetExceeded):
            scoped.remaining_budget()
        await scoped.close()

    async def test_provider_budget_abort_crosses_retry_middleware(self):
        calls = []

        class Caller:
            middleware_manager = SimpleNamespace(wrap_model_call=lambda params, next_call: next_call(params))

            def call_llm(self, params):
                for _ in range(3):
                    try:
                        return self.middleware_manager.wrap_model_call(
                            params, lambda _p: calls.append("sent")
                        )
                    except Exception:
                        continue

            async def call_llm_async(self, params):
                return self.call_llm(params)

        def abort():
            raise BudgetExceeded("no remaining calls")

        caller = Caller()
        install_provider_context_budget_guard(
            caller,
            ProviderContextBudgetGuard(model="scripted"),
            before_dispatch=abort,
        )
        with self.assertRaises(BudgetExceeded):
            caller.call_llm(SimpleNamespace(messages=[], api_params={}))
        self.assertEqual(calls, [])

    async def test_registry_checks_interface_capability_and_input(self):
        registry = SubagentRegistry()
        registry.register(FakeSubagent())
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            registry.register(FakeSubagent())
        with self.assertRaisesRegex(ValueError, "capability_mismatch"):
            registry.resolve(contract(required_capabilities=["game"]))
        with self.assertRaisesRegex(ValueError, "tool_mismatch"):
            registry.resolve(contract(allowed_tools=[]))
        with self.assertRaises(ValueError):
            registry.resolve(contract(agent_input={"unexpected": True}))

    async def test_invalid_success_or_stale_observation_is_rejected(self):
        with self.assertRaises(ValueError):
            SlowDecision(action="SUCCESS", summary="done", observation_id="x", verification="done")
        result = await self.run_script([{"action": "SUCCESS", "observation_id": "old"}])
        self.assertEqual(result.status.value, "error")

    async def test_recorder_prefers_explicit_main_trace_and_keeps_child_references(self):
        await self.run_script([{"action": "SUCCESS"}])
        directory = self.root
        recorder.write_trajectory(directory, self.root, step_log_path=self.store.step_log_path)
        item = json.loads((directory / "traj.jsonl").read_text())
        self.assertEqual(item["orchestration"]["decision"]["action"], "SUCCESS")

    async def test_wrapper_configuration_and_status_mapping(self):
        generated = appcfg.app_config(
            vm_ip="vm",
            server_port=5000,
            max_steps=99,
            artifact_root=self.root,
            harness={"orchestration": {"enabled": True, "max_cycles": 3}},
        )
        self.assertEqual(generated["orchestration"]["max_cycles"], 3)
        self.assertEqual(generated["execution"]["max_agent_iterations_per_round"], 99)
        self.assertEqual(
            launch.classify(
                {
                    "returncode": 0,
                    "step_count": 1,
                    "terminal_status": "fail",
                    "terminal_decision": {"reason": "budget_exhausted"},
                }
            ),
            (None, "orchestration_budget_exhausted"),
        )

    async def test_public_facade_uses_orchestration_configuration(self):
        facade = self.scripted_facade()
        result = await facade.run_query(
            DesktopRunRequest(
                session_id="test",
                query="open the dialog",
                max_steps=99,
            )
        )
        self.assertEqual(result.terminal_status, "finish")
        self.assertEqual(result.step_count, 1)
        self.assertEqual(result.terminal_decision["metadata"]["step_unit"], "orchestration_cycle")

    def scripted_facade(self):
        # Exercise the real public queue/task/factory path with fake environment services.
        facade = object.__new__(NorthCUAAgentFacade)
        facade.app_config, facade.artifact_store = self.config, self.store
        facade.rounds, facade.state_store = self.rounds, self.state_store
        facade.runtime_factory, facade.round_execution_service = self.factory, self.service
        self.service.drain_background_tasks = AsyncMock()
        original = DesktopRuntimeFactory._build_orchestration

        def build(factory, options):
            runtime = original(factory, options)
            runtime.step_runtime.step_executor.model = ScriptedModel([{"action": "SUCCESS"}])
            return runtime

        patcher = patch.object(DesktopRuntimeFactory, "_build_orchestration", build)
        patcher.start()
        self.addCleanup(patcher.stop)
        return facade

    async def test_public_preflight_terminal_is_not_counted_as_a_cycle(self):
        facade = self.scripted_facade()
        self.rounds.state.session_step = 2
        await self.state_store.save(self.rounds.state)
        result = await facade.run_query(
            DesktopRunRequest(
                session_id="test",
                query="open the dialog",
                max_session_steps=1,
            )
        )
        self.assertEqual(result.step_count, 0)
        self.assertTrue(result.hit_step_limit)
        self.assertEqual(self.observer.calls, 0)
        self.assertTrue(self.rounds.state.step_logs[-1].orchestration.synthetic)
