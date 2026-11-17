"""Regression cases from the failed OSWorld batch; no VM or model requests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import requests
from northcua.agentinfra.providers.chat_response_capture import EmptyModelResponseError, _ChatResponseAbort
from northcua.agentkit.orchestration.contracts import OrchestrationState, SubagentResult
from northcua.agentkit.runtime import AgentRoundRuntimeSpec
from northcua.core.budget import AgentStepBudget
from northcua.transport.ws.local_step.single_model import ModelStepLLMTimeoutError

import setup_transport
from tests import test_orchestration as fixtures


class DecisionRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.OrchestrationTest()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.make_runtime([])
        self.ports = self.runtime.step_runtime.step_executor
        self.fixture.rounds.state.orchestration = OrchestrationState(round_num=1, started_at=time.time())
        self.ports.observation_ref = lambda: {
            "observation_id": "current-id", "screenshot_path": "/evidence/current.png",
        }
        self.context = SimpleNamespace(prompt=[], cursor=SimpleNamespace(session_step=1))

    def decision(self, **overrides):
        return json.dumps({"action": "SUCCESS", "summary": "done", "verification": "current image",
                           "observation_id": "current-id", "evidence_refs": ["current-id"], **overrides})

    def replies(self, values):
        responses = iter(values)

        async def infer(messages, charge, context):
            charge()
            return next(responses)

        self.ports.infer = AsyncMock(side_effect=infer)

    async def test_exact_current_image_reference_is_normalized_without_another_model_call(self):
        self.replies([self.decision(evidence_refs=["/evidence/current.png"])])
        result = await self.ports.run_model_step(self.context)
        self.assertIn("current-id", result.evidence_refs)
        self.assertEqual(self.ports.state.usage.model_calls, 1)

    async def test_invalid_contract_is_corrected_before_any_dispatch(self):
        self.replies([self.decision(contract={"allowed_operations": None}), self.decision()])
        result = await self.ports.run_model_step(self.context)
        self.assertEqual(result.action, "SUCCESS")
        self.assertEqual(self.ports.state.usage.model_calls, 2)
        self.assertEqual(self.fixture.device.calls, [])

    async def test_stale_observation_cannot_be_normalized_into_success(self):
        self.replies([self.decision(observation_id="old", evidence_refs=["/evidence/current.png"])] * 3)
        with self.assertRaisesRegex(ValueError, "stale observation"):
            await self.ports.run_model_step(self.context)
        self.assertEqual(self.ports.state.usage.model_calls, 3)

    async def test_observation_retry_reads_again_without_dispatching_an_action(self):
        observation = SimpleNamespace(
            context=SimpleNamespace(observation_metadata={}, current_artifacts=[]),
            snapshot=SimpleNamespace(clean_screenshot_path="/missing/image.png"),
        )
        observer = AsyncMock(side_effect=[RuntimeError("COMMAND_TRANSPORT_FAILED"), observation])
        with patch.object(self.ports, "observer", SimpleNamespace(collect=observer)), patch(
            "northcua.agents.desktop.runtime.orchestration.asyncio.sleep", new_callable=AsyncMock,
        ):
            result = await self.ports.capture(self.context)
        self.assertIs(result, observation)
        self.assertEqual(observer.await_count, 2)
        self.assertEqual(self.fixture.device.calls, [])

    async def test_model_timeout_is_error_and_never_a_scoring_budget_stop(self):
        self.ports.pipeline = AsyncMock(side_effect=TimeoutError("LLM call timed out after 180.0 seconds."))
        result = await self.runtime.run(AgentRoundRuntimeSpec(
            query="open the dialog", budget=AgentStepBudget(max_round_steps=4),
            fallback_round_steps=4, begin_new_round=False,
        ))
        self.assertEqual(result.terminal_decision.action.value, "error")
        self.assertNotEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(self.fixture.device.calls, [])

    async def test_enclosing_run_deadline_remains_a_scoring_budget_stop(self):
        async def delayed_pipeline(context):
            await asyncio.sleep(5)

        self.fixture.config.orchestration.max_seconds = 0.2
        self.ports.pipeline = AsyncMock(side_effect=delayed_pipeline)
        result = await self.runtime.run(AgentRoundRuntimeSpec(
            query="open the dialog", budget=AgentStepBudget(max_round_steps=4),
            fallback_round_steps=4, begin_new_round=False,
        ))
        self.assertEqual(self.ports.pipeline.await_count, 1)
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted",
                         result.terminal_decision.final_answer)
        self.assertEqual(result.terminal_decision.final_answer, "wall_time_limit")

    async def test_parent_recovers_partial_result_from_timeout_cancellation_cause(self):
        agent = fixtures.FakeSubagent()

        async def interrupted(request, runtime):
            cause = asyncio.CancelledError()
            cause.subagent_result = SubagentResult(
                subgoal_id=request.contract.subgoal_id, attempt_id=request.attempt_id,
                outcome="yielded", exit_reason="interrupted", summary="confirmed output retained",
            )
            raise TimeoutError() from cause

        agent.run = interrupted
        self.fixture.factory.subagents.register(agent)
        result = await self.fixture.run_script([
            {"action": "CONTINUE", "contract": fixtures.contract().model_dump()},
            {"action": "FAIL"},
        ])
        handoff = result.steps[0].orchestration.subagent_result
        self.assertEqual(handoff.summary, "confirmed output retained")
        self.assertEqual(handoff.outcome, "yielded")
        self.assertEqual(self.fixture.device.calls, [])


class ModelTimeoutRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.OrchestrationTest()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config.orchestration.model_timeout_retries = 2
        self.requests = []

    async def run_with(self, faults, actions=None):
        runtime = self.fixture.make_runtime(actions or [{"action": "SUCCESS"}])
        ports = runtime.step_runtime.step_executor
        original = self.fixture.model.run_step
        slow_count = 0
        child_count = 0

        async def send(**kwargs):
            nonlocal slow_count, child_count
            kwargs["before_provider_call"]()
            path = kwargs["context"]["logical_agent_path"]
            self.requests.append((path, kwargs["build_envelope"]([], None)))
            if path == "orchestrator":
                fault = faults.get(slow_count)
                slow_count += 1
            else:
                fault = faults.get(("child", child_count))
                child_count += 1
            if fault == "delay":
                await asyncio.sleep(5)
            elif fault:
                raise fault
            return await original(**{**kwargs, "before_provider_call": lambda: None})

        ports.model = SimpleNamespace(run_step=send)
        return await runtime.run(AgentRoundRuntimeSpec(
            query="open the dialog", budget=AgentStepBudget(max_round_steps=4),
            fallback_round_steps=4, begin_new_round=False,
        ))

    @staticmethod
    def timeout():
        return ModelStepLLMTimeoutError("LLM call timed out after 600.0 seconds.")

    async def test_timeout_then_success_reuses_request_and_charges_each_attempt(self):
        result = await self.run_with({0: self.timeout()})
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.model_calls, 2)
        failures = list(fixtures.read_records(self.fixture.store.root / "provider_failures.jsonl"))
        self.assertEqual(failures[0]["error_type"], "ModelStepLLMTimeoutError")
        self.assertIsNone(failures[0]["response"])
        self.assertIsNone(failures[0]["usage"])
        self.assertEqual(self.fixture.device.calls, [])

    async def test_two_timeouts_can_recover_on_third_attempt(self):
        result = await self.run_with({0: self.timeout(), 1: self.timeout()})
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(self.requests), 3)

    async def test_mixed_empty_and_timeout_failures_share_three_attempts(self):
        result = await self.run_with({
            0: self.timeout(),
            1: _ChatResponseAbort(EmptyModelResponseError("empty"), retryable=True),
            2: self.timeout(),
        })
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result.terminal_decision.action.value, "error")

    async def test_exhausted_retries_remain_error_not_a_scored_budget_stop(self):
        result = await self.run_with({i: self.timeout() for i in range(3)})
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result.terminal_decision.action.value, "error")
        self.assertIn("ModelStepLLMTimeoutError", result.terminal_decision.final_answer)
        self.assertNotEqual(result.terminal_decision.reason.value, "budget_exhausted")

    async def test_global_budget_prevents_second_send(self):
        self.fixture.config.orchestration.max_model_calls = 1
        result = await self.run_with({0: self.timeout()})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")

    async def test_timeout_retries_can_be_disabled_independently(self):
        self.fixture.config.orchestration.model_timeout_retries = 0
        result = await self.run_with({0: self.timeout()})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.terminal_decision.action.value, "error")

    async def test_unclassified_error_is_not_retried(self):
        result = await self.run_with({0: RuntimeError("provider configuration invalid")})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.terminal_decision.action.value, "error")

    async def test_outer_deadline_is_not_retried_as_model_timeout(self):
        self.fixture.config.orchestration.max_seconds = 0.05
        result = await self.run_with({0: "delay"})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")

    async def test_verification_timeout_does_not_replay_completed_child_action(self):
        child = fixtures.FakeSubagent()
        self.fixture.factory.subagents.register(child)
        result = await self.run_with({1: self.timeout()}, [
            {"action": "CONTINUE", "contract": fixtures.contract().model_dump()},
            {"action": "SUCCESS"},
        ])
        self.assertEqual(result.status.value, "success")
        self.assertEqual(child.calls, 1)
        self.assertEqual(len(self.fixture.device.calls), 1)
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.model_calls, 4)

    async def test_child_local_budget_blocks_retry_and_returns_control_to_parent(self):
        child = fixtures.FakeSubagent()
        self.fixture.factory.subagents.register(child)
        result = await self.run_with({("child", 0): self.timeout()}, [
            {"action": "CONTINUE", "contract": fixtures.contract(budget={
                "max_model_calls": 1, "max_tool_calls": 1, "max_seconds": 5,
            }).model_dump()},
            {"action": "SUCCESS"},
        ])
        self.assertEqual(result.status.value, "success")
        handoff = result.steps[0].orchestration.subagent_result
        self.assertEqual(handoff.usage.model_calls, 1)
        self.assertEqual(handoff.exit_reason, "BudgetExceeded")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.fixture.device.calls), 1)


class SetupTransportTest(unittest.TestCase):
    def test_lost_submission_response_never_repeats_the_setup_write(self):
        submissions = []
        responses = iter([{"done": False}, {"done": True, "result": {
            "returncode": 0, "output": "extracted", "error": "",
        }}])

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, url, *, json, timeout):
                if "subprocess.Popen" in json["command"][2]:
                    submissions.append(json)
                    raise requests.ConnectionError("lost response after delivery")
                result = next(responses)
                return SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"output": __import__("json").dumps(result)})

        controller = SimpleNamespace(http_server="http://task-vm:5000", client_password="test-only")
        with patch.object(setup_transport.requests, "Session", Session), patch.object(
            setup_transport.time, "sleep",
        ):
            result = setup_transport.execute_background(controller, ["true"], timeout=60, check=True)
        self.assertEqual(result["output"], "extracted")
        self.assertEqual(len(submissions), 1)

    def test_runner_records_output_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = {"root": directory, "command": ["sh", "-c", "echo evidence; exit 7"],
                    "shell": False, "timeout": 5}
            subprocess.run(["python3", "-c", setup_transport._RUNNER, json.dumps(spec)], check=True)
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(result["returncode"], 7)
            self.assertEqual(result["output"], "evidence\n")
