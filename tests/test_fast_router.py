"""Fast decisions and shared-budget handoffs, without a hosted model or VM."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from northcua.agentkit.orchestration.contracts import (
    CycleRecord,
    PlanningDecision,
    SubagentResult,
    SubagentSpec,
    SubgoalAttempt,
    SubgoalIntent,
)
from northcua.agentkit.orchestration.decisions import DecisionClient
from northcua.agentkit.orchestration.routing import (
    choose,
    compact_evidence,
    recent_method_failures,
)
from northcua.agentkit.orchestration.runtime import read_records
from northcua.agentkit.runtime import AgentRoundRuntimeSpec
from northcua.core.budget import AgentStepBudget
from northcua.core.config_base import FastRouterConfig
from pydantic import ValidationError

from tests import test_orchestration as fixtures


def intent(**overrides):
    return SubgoalIntent.model_validate(
        {
            "subgoal_id": "open-dialog",
            "goal": "Open dialog",
            "success_criteria": ["Dialog visible"],
            "budget": {"max_model_calls": 12, "max_tool_calls": 12, "max_seconds": 60},
            **overrides,
        }
    )


def plan(**overrides):
    return PlanningDecision.model_validate(
        {
            "action": "CONTINUE",
            "summary": "Open the dialog",
            "observation_id": "current",
            "verification": "Dialog is not visible",
            "contract": intent().model_dump(),
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_id", "gui_jev"),
        ("allowed_tools", ["agent_desktop"]),
        ("agent_input", {}),
    ],
)
def test_planner_cannot_select_executor(field, value):
    with pytest.raises(ValidationError):
        intent(**{field: value})


def test_planning_decision_round_trips_in_existing_cycle():
    record = CycleRecord(cycle_id="cycle", decision=plan())
    restored = CycleRecord.model_validate_json(record.model_dump_json())
    assert isinstance(restored.decision, PlanningDecision)
    assert restored.decision.contract.goal == "Open dialog"
    with pytest.raises(ValidationError):
        plan(action="SUCCESS", contract=None, evidence_refs=[])


@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, 529, "timeout"])
def test_decision_transport_meters_retries_and_preserves_response(failure):
    statuses, calls = iter([failure, 200]), []

    async def run():
        async def metered(model, invoke):
            calls.append(model)
            return await invoke()

        def transport(request):
            assert json.loads(request.content)["model"] == "test-jev"
            status = next(statuses)
            if status == "timeout":
                raise httpx.ReadTimeout("scripted timeout", request=request)
            return httpx.Response(
                status, json={"answers": {"ready": {"noul": 0.9}}, "usage": {"input_tokens": 12}}
            )

        real_client = httpx.AsyncClient
        with (
            patch.object(
                httpx,
                "AsyncClient",
                side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw),
            ),
            patch("northcua.agentkit.orchestration.decisions.asyncio.sleep", new_callable=AsyncMock),
        ):
            client = DecisionClient(
                SimpleNamespace(endpoint="https://test/v1/systemone", api_key="test", model="test-jev")
            )
            return await client.ask({"state": "test", "questions": {}}, metered)

    response = asyncio.run(run())
    assert calls == ["test-jev", "test-jev"]
    assert response["usage"]["input_tokens"] == 12


@pytest.mark.parametrize("failure,attempts", [(401, 1), (500, 3), ("timeout", 3)])
def test_decision_failures_stop_within_the_metered_limit(failure, attempts):
    calls = []

    async def run():
        async def metered(model, invoke):
            calls.append(model)
            return await invoke()

        def transport(request):
            if failure == "timeout":
                raise httpx.ReadTimeout("private response", request=request)
            return httpx.Response(failure, text="private response")

        real_client = httpx.AsyncClient
        with (
            patch.object(
                httpx,
                "AsyncClient",
                side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw),
            ),
            patch("northcua.agentkit.orchestration.decisions.asyncio.sleep", new_callable=AsyncMock),
        ):
            client = DecisionClient(
                SimpleNamespace(endpoint="https://test/v1/systemone", api_key="test", model="jev")
            )
            with pytest.raises(RuntimeError) as error:
                await client.ask({"state": "test", "questions": {}}, metered)
            assert "private response" not in str(error.value)

    asyncio.run(run())
    assert len(calls) == attempts


def signals(scores=(0.95, 0.9), progress="unknown"):
    return {
        "answers": {
            **{f"capable_{i}": {"noul": value} for i, value in enumerate(scores)},
            "needs_replan": {"noul": 0.01},
            "progress": {
                "choice": progress,
                "probabilities": {
                    key: float(key == progress) for key in ("done", "progressing", "blocked", "unknown")
                },
            },
        },
        "usage": {"input_tokens": 30, "output_tokens": 0},
    }


def test_gui_preference_requires_comparable_capability():
    specs = [SubagentSpec("gui_jev", "GUI"), SubagentSpec("general", "General")]
    config = FastRouterConfig()
    assert choose(signals((0.85, 0.9)), specs, [], "o", config).agent_id == "gui_jev"
    assert choose(signals((0.71, 0.99)), specs, [], "o", config).agent_id == "general"


def test_uncalibrated_scores_use_available_general_fallback():
    specs = [SubagentSpec("gui_jev", "GUI"), SubagentSpec("general", "General")]
    config = FastRouterConfig()
    decision = choose(signals((0.6, 0.67)), specs, [], "o", config)
    assert decision.agent_id == "general"
    assert decision.reason == "general_fallback_for_uncertainty"
    config.fallback_agent = None
    assert choose(signals((0.6, 0.67)), specs, [], "o", config).action == "return_to_slow"


@pytest.mark.parametrize("score", [True, float("nan"), float("inf"), -0.01, 1.01])
def test_invalid_model_scores_never_dispatch(score):
    with pytest.raises(ValueError):
        choose(signals((score,)), [SubagentSpec("gui_jev", "GUI")], [], "o", FastRouterConfig())


def test_long_tool_output_preserves_handoff_status_and_final_readback():
    result = SubagentResult(
        subgoal_id="s",
        attempt_id="a",
        outcome="yielded",
        exit_reason="local_limit",
        summary=json.dumps(
            {
                "message": "One field remains",
                "unresolved_delivery": False,
                "tool_results": [{"stdout": "x" * 20000 + "Final readback: saved"}],
            }
        ),
        criterion_claims=["First field saved"],
    )
    evidence = compact_evidence(result)
    assert evidence["criterion_claims"] == ["First field saved"]
    assert evidence["summary"]["message"] == "One field remains"
    assert "Final readback: saved" in evidence["summary"]["tool_results"][0]["stdout"]
    assert evidence["evidence_truncated"]


def test_method_failure_context_keeps_bounded_operational_evidence_across_goals():
    cycles = []
    for index in range(9):
        contract = fixtures.contract(agent_id="gui_jev", goal=f"Update form {index}")
        result = SubagentResult(
            subgoal_id=contract.subgoal_id,
            attempt_id=f"attempt-{index}",
            outcome="failed",
            exit_reason="action_failed",
            summary=json.dumps(
                {
                    "error": {
                        "code": "ACTION_NOT_SUPPORTED",
                        "message": "Editable field required",
                        "details": "x" * 20000,
                    },
                    "screen": {
                        "app": "chrome",
                        "window": "Form",
                        "window_id": "w1",
                        "visible_context": "x" * 20000,
                    },
                    "turns": [
                        {
                            "operation": "TYPE_TEXT",
                            "target": "combobox",
                            "delivery": "not_delivered",
                            "argv": "secret",
                        }
                    ],
                }
            ),
        )
        cycles.append(
            CycleRecord(
                cycle_id=str(index),
                attempts=[SubgoalAttempt(contract=contract, result=result)],
            )
        )
    evidence = recent_method_failures(cycles)
    assert len(evidence) == 6
    assert evidence[0]["goal"] == "Update form 3"
    assert evidence[-1]["agent"] == "gui_jev"
    assert evidence[-1]["error"]["code"] == "ACTION_NOT_SUPPORTED"
    assert evidence[-1]["last_action"] == {
        "operation": "TYPE_TEXT",
        "target": "combobox",
        "delivery": "not_delivered",
    }
    assert len(json.dumps(evidence)) < 4000
    assert "secret" not in json.dumps(evidence)


class ScriptedRouter:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def ask(self, request, call_model):
        self.requests.append(request)

        async def reply():
            response = next(self.responses)
            if isinstance(response, BaseException):
                raise response
            return response

        return await call_model("scripted-jev", reply)


class RoutedAgent:
    def __init__(self, name, outcomes, *, calls=1, summary="Observed progress"):
        self.spec = SubagentSpec(name, name)
        self.outcomes = iter(outcomes)
        self.calls = calls
        self.requests = []
        self.summary = summary

    async def run(self, request, runtime):
        self.requests.append(request)

        async def response():
            return {"answers": {}}

        for _ in range(self.calls):
            await runtime.call_model("scripted-child", response)
        observation = await runtime.observe()
        outcome = next(self.outcomes)
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return SubagentResult(
            subgoal_id=request.contract.subgoal_id,
            attempt_id=request.attempt_id,
            outcome=outcome,
            exit_reason="scripted",
            summary=self.summary,
            evidence_refs=[observation["observation_id"]],
        )


class FastRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.OrchestrationTest()
        await self.f.asyncSetUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config.orchestration.fast_router = FastRouterConfig(
            enabled=True, verification_reserve_seconds=0
        )

    def register(self, gui, general=None):
        self.f.factory.subagents.register(gui)
        if general is not None:
            self.f.factory.subagents.register(general)

    async def test_new_goal_still_sees_gui_failure_before_successful_general_handoff(self):
        gui = RoutedAgent(
            "gui_jev",
            ["blocked", "achieved"],
            summary=json.dumps(
                {
                    "error": {"code": "ACTION_NOT_SUPPORTED", "message": "Editable field required"},
                    "turns": [{"operation": "TYPE_TEXT", "target": "combobox", "delivery": "not_delivered"}],
                }
            ),
        )
        self.register(gui, RoutedAgent("general", ["achieved"]))
        await self.run_plan(
            [signals(), signals(progress="blocked"), signals()],
            actions=[
                {"action": "CONTINUE", "contract": intent(subgoal_id="first").model_dump()},
                {"action": "CONTINUE", "contract": intent(subgoal_id="second").model_dump()},
                {"action": "SUCCESS"},
            ],
        )
        state = json.loads(self.router.requests[2]["state"])
        failure = state["planner_context"]["recent_method_failures"][0]
        self.assertEqual(failure["agent"], "gui_jev")
        self.assertEqual(failure["error"]["code"], "ACTION_NOT_SUPPORTED")

    async def run_plan(self, responses, *, planned=None, actions=None):
        actions = actions or [
            {"action": "CONTINUE", "contract": (planned or intent()).model_dump()},
            {"action": "SUCCESS"},
        ]
        self.runtime = self.f.make_runtime(actions)
        self.ports = self.runtime.step_runtime.step_executor
        self.router = self.ports.router_client = ScriptedRouter(responses)
        return await self.runtime.run(
            AgentRoundRuntimeSpec(
                query="open the dialog",
                budget=AgentStepBudget(max_round_steps=self.f.config.orchestration.max_cycles),
                fallback_round_steps=4,
                begin_new_round=False,
            )
        )

    async def test_switches_agent_without_an_intermediate_slow_call(self):
        gui = RoutedAgent("gui_jev", ["blocked"])
        general = RoutedAgent("general", ["achieved"])
        self.register(gui, general)
        result = await self.run_plan([signals(), signals(progress="unknown")])
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(len(self.f.model.contexts), 2)
        self.assertEqual(len(gui.requests), 1)
        self.assertEqual(len(general.requests), 1)
        cycle = result.steps[0].orchestration
        self.assertEqual([a.contract.agent_id for a in cycle.attempts], ["gui_jev", "general"])
        self.assertEqual(self.ports.state.usage.model_calls, 6)
        self.assertEqual(len(self.router.requests), 2)
        self.assertEqual(general.requests[0].history[-1]["subagent_result"]["outcome"], "blocked")
        for payload in self.f.model.contexts:
            self.assertNotIn("available_subagents", payload)
            self.assertNotIn('"agent_id"', json.dumps(payload))
        self.assertEqual(cycle.routing[-1].reason, "subgoal_completion_claim")
        self.assertEqual(self.f.model.contexts[-1]["history"][0]["execution_status"], "achieved")

    async def test_attempts_share_fixed_allowance_without_reapplying_floor(self):
        config = self.f.config.orchestration
        config.min_subagent_steps = 30
        config.subagent_steps_per_cycle = 8
        config.fast_router.attempt_model_calls = 2
        gui = RoutedAgent("gui_jev", ["yielded", "achieved"], calls=2)
        self.register(gui)
        result = await self.run_plan(
            [
                signals((0.9,)),
                signals((0.9,), "progressing"),
                signals((0.9,), "done"),
            ]
        )
        cycle = result.steps[0].orchestration
        self.assertEqual(cycle.decision.contract.budget.max_model_calls, 8)
        self.assertEqual([r.contract.budget.max_model_calls for r in gui.requests], [2, 2])
        self.assertEqual(cycle.usage.model_calls, 7)
        self.assertEqual(len(self.router.requests), 2)
        self.assertNotEqual(gui.requests[0].attempt_id, gui.requests[1].attempt_id)
        self.assertEqual(len(gui.requests[1].history), 1)

    async def test_uncertain_write_returns_to_slow_without_rerouting(self):
        gui = RoutedAgent("gui_jev", ["blocked"], summary='{"unresolved_delivery": true}')
        general = RoutedAgent("general", ["achieved"])
        self.register(gui, general)
        result = await self.run_plan([signals()])
        self.assertEqual(len(self.router.requests), 1)
        self.assertEqual(general.requests, [])
        self.assertEqual(
            result.steps[0].orchestration.routing[-1].reason, "unresolved_action_requires_verification"
        )

    async def test_progress_split_between_partial_and_complete_can_continue(self):
        gui = RoutedAgent("gui_jev", ["yielded", "achieved"])
        self.register(gui)
        progress = signals((0.9,), "progressing")
        progress["answers"]["progress"]["probabilities"] = {
            "done": 0.2,
            "progressing": 0.6,
            "unknown": 0.2,
            "blocked": 0.0,
        }
        result = await self.run_plan([signals((0.9,)), progress])
        self.assertEqual(len(gui.requests), 2)
        self.assertEqual(len(self.f.model.contexts), 2)
        self.assertEqual(result.steps[0].orchestration.routing[-1].reason, "subgoal_completion_claim")

    async def test_completion_claim_always_reaches_slow_acceptance(self):
        gui = RoutedAgent("gui_jev", ["achieved"])
        self.register(gui)
        result = await self.run_plan(
            [signals((0.9,))],
            actions=[
                {"action": "CONTINUE", "contract": intent().model_dump()},
                {"action": "FAIL"},
            ],
        )
        self.assertEqual(result.terminal_decision.action.value, "fail")
        self.assertEqual(len(self.router.requests), 1)
        payload = json.loads(self.router.requests[0]["state"])
        self.assertIn("verification", payload["planner_context"])
        self.assertNotIn("input_schema", payload["candidates"][0])

    async def test_native_probe_is_read_only_and_shares_the_goal_tool_budget(self):
        gui = RoutedAgent("gui_jev", ["achieved"])
        gui.environment = SimpleNamespace(execute=AsyncMock(return_value={"status": "observed"}))

        async def probe(read):
            return await read({"argv": ["snapshot"]})

        gui.routing_observation = probe
        self.register(gui)
        result = await self.run_plan([signals((0.9,))])
        self.assertEqual(result.steps[0].orchestration.usage.tool_calls, 1)
        self.assertEqual(gui.requests[0].contract.budget.max_tool_calls, 11)
        payload = json.loads(self.router.requests[0]["state"])
        self.assertEqual(payload["observation"]["native"]["gui_jev"]["status"], "observed")
        gui.environment.execute.assert_awaited_once()

    async def test_probe_failure_or_write_request_does_not_block_other_candidates(self):
        gui = RoutedAgent("gui_jev", [])
        gui.environment = SimpleNamespace(execute=AsyncMock())

        async def probe(read):
            return await read({"argv": ["click", "a"]})

        gui.routing_observation = probe
        general = RoutedAgent("general", ["achieved"])
        self.register(gui, general)
        result = await self.run_plan([signals((0.1, 0.9))])
        self.assertEqual(len(general.requests), 1)
        self.assertEqual(result.steps[0].orchestration.usage.tool_calls, 0)
        gui.environment.execute.assert_not_awaited()

    async def test_api_failure_is_accounted_and_returns_to_planner(self):
        gui = RoutedAgent("gui_jev", ["achieved"])
        self.register(gui)
        result = await self.run_plan([RuntimeError("service unavailable")])
        self.assertEqual(gui.requests, [])
        self.assertEqual(self.ports.state.usage.model_calls, 3)
        self.assertEqual(result.steps[0].orchestration.routing[-1].reason, "router_unavailable:RuntimeError")

    async def test_last_cycle_has_fresh_slow_verification(self):
        self.f.config.orchestration.max_cycles = 1
        self.f.config.orchestration.subagent_steps_per_cycle = 8
        gui = RoutedAgent("gui_jev", ["achieved"])
        self.register(gui)
        result = await self.run_plan([signals((0.9,)), signals((0.9,), "done")])
        self.assertEqual(len(result.steps), 1)
        cycle = result.steps[0].orchestration
        self.assertEqual(cycle.final_verification.action, "SUCCESS")
        self.assertNotEqual(cycle.final_verification.observation_id, cycle.decision.observation_id)

    async def test_planner_retry_keeps_current_observation_and_schema(self):
        runtime = self.f.make_runtime([])
        ports = runtime.step_runtime.step_executor
        prompts = []

        async def infer(messages, charge, context):
            charge()
            prompts.append(list(messages))
            observation_id = ports.observation_ref()["observation_id"]
            return plan(
                action="SUCCESS",
                contract=None,
                observation_id="stale" if len(prompts) == 1 else observation_id,
                evidence_refs=[observation_id],
            ).model_dump_json()

        ports.infer = infer
        result = await runtime.run(
            AgentRoundRuntimeSpec(
                query="open the dialog",
                budget=AgentStepBudget(max_round_steps=4),
                fallback_round_steps=4,
                begin_new_round=False,
            )
        )
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[1][:-2], prompts[0])
        self.assertIn("stale observation", prompts[1][-1].content[0].text)

    async def test_local_model_limit_yields_and_can_continue_with_shared_budget(self):
        config = self.f.config.orchestration.fast_router
        config.attempt_model_calls, config.max_attempts = 2, 2
        gui = RoutedAgent("gui_jev", [], calls=3)
        self.register(gui)
        result = await self.run_plan([signals((0.9,)), signals((0.9,), "progressing")])
        attempts = result.steps[0].orchestration.attempts
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(a.result.outcome == "yielded" for a in attempts))
        self.assertTrue(all(a.result.exit_reason == "subgoal_model_calls_limit" for a in attempts))
        self.assertEqual(self.ports.state.usage.model_calls, 8)

    async def test_router_preserves_last_global_call_for_slow(self):
        self.f.config.orchestration.max_model_calls = 2
        gui = RoutedAgent("gui_jev", ["achieved"])
        self.register(gui)
        result = await self.run_plan([])
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(self.ports.state.usage.model_calls, 2)
        self.assertEqual(self.router.requests, [])
        self.assertEqual(gui.requests, [])

    async def test_tool_policy_filters_candidates_before_model_or_execution(self):
        gui = RoutedAgent("gui_jev", ["achieved"])
        gui.spec = SubagentSpec("gui_jev", "GUI", required_tools=("agent_desktop",))
        self.register(gui)
        self.f.config.orchestration.fast_router.allowed_tools = []
        result = await self.run_plan([])
        self.assertEqual(result.terminal_decision.reason.value, "configuration_error")
        self.assertEqual(self.router.requests, [])
        self.assertEqual(gui.requests, [])

    async def test_cancelled_handoff_recovers_all_attempts_without_replay(self):
        gui = RoutedAgent("gui_jev", ["blocked"])
        general = RoutedAgent("general", ["cancel"])
        self.register(gui, general)
        with self.assertRaises(asyncio.CancelledError):
            await self.run_plan([signals(), signals(progress="blocked")])
        dispatch = next(
            row for row in read_records(self.ports.journal.path) if row["event"] == "dispatch_finished"
        )
        completed = dispatch["state"]["routing"]["attempts"]
        self.assertEqual([item["result"]["attempt_id"] for item in completed], [gui.requests[0].attempt_id])
        spent = self.ports.state.usage.model_calls
        result = await self.run_plan([], actions=[{"action": "SUCCESS"}])
        self.assertEqual(result.terminal_decision.action.value, "finish")
        history = self.f.model.contexts[0]["history"]
        self.assertEqual(len(history[0]["results"]), 2)
        self.assertEqual(history[0]["results"][-1]["exit_reason"], "interrupted")
        self.assertEqual(len(general.requests), 1)
        self.assertEqual(self.ports.state.usage.model_calls, spent + 1)
