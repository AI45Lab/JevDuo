"""User clarification service with real orchestration and SDK, scripted HTTP only."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from northcua.agentkit.orchestration.contracts import SubgoalIntent
from northcua.agentkit.orchestration.runtime import read_records
from northcua.agents.desktop.subagents.benchmark_user import BenchmarkUser
from northcua.core.config_base import FastRouterConfig
from openai import APIStatusError, OpenAI

from tests import test_orchestration as fixtures
from tests.test_chat_responses import completion


class UserServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.OrchestrationTest()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bodies = []
        self.failure = False
        self.ready = self.release = None
        client_class = httpx.Client

        def handle(request):
            self.bodies.append(json.loads(request.content))
            if self.ready is not None:
                self.ready.set()
                self.assertTrue(self.release.wait(3))
            if self.failure:
                return httpx.Response(503, json={"error": {"message": "unavailable"}})
            body = completion("I have an updated certificate in the provided folder.")
            body["usage"]["prompt_tokens_details"] = {"cached_tokens": 4}
            return httpx.Response(200, json=body)

        patcher = patch("northcua.agents.desktop.subagents.benchmark_user.httpx",
                        SimpleNamespace(Client=lambda **kwargs: client_class(
                            transport=httpx.MockTransport(handle), **kwargs)))
        patcher.start()
        self.addCleanup(patcher.stop)
        facade = SimpleNamespace()

        class Backend:
            def __init__(self):
                self._client = OpenAI(
                    api_key="private-test-key", base_url="https://unused.invalid/v1", max_retries=0,
                    http_client=client_class(transport=httpx.MockTransport(handle)),
                )

            def chat(self, messages):
                reply = self._client.chat.completions.create(model="scripted", messages=messages)
                return reply.choices[0].message.content

        facade.create_backend = lambda config: Backend()

        def generate(messages, options=None):
            self.assertEqual(options["retry_attempts"], 1)
            return facade.create_backend(options).chat(messages)

        facade.generate_chat = generate

        class Simulator:
            def respond(self, question):
                return facade.generate_chat([
                    {"role": "system", "content": "private fixture knowledge: updated certificate"},
                    {"role": "user", "content": question},
                ])

        self.agent = BenchmarkUser(Simulator(), facade, {"direct_api": True},
                                   self.fixture.root / "interactions")

    async def test_router_can_ask_user_and_observe_answer_without_changing_jev_inputs(self):
        self.fixture.factory.subagents.register(self.agent)
        contract = fixtures.contract(
            agent_id="user", allowed_tools=["ask_user"],
            agent_input={"question": "The funds are insufficient; is there an update?"},
        )
        result = await self.fixture.run_script([
            {"action": "CONTINUE", "contract": contract.model_dump()}, {"action": "SUCCESS"},
        ])
        self.assertEqual(result.status.value, "success")
        self.assertEqual(self.fixture.device.calls, [])
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.model_calls, 2)
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.tool_calls, 1)
        self.assertEqual(len(self.bodies), 1)
        self.assertNotIn("private fixture knowledge", json.dumps(self.fixture.model.contexts))
        self.assertIn("updated certificate", json.dumps(self.fixture.model.contexts[1]))
        ledger = list(read_records(self.agent.log_dir / "user_simulator_usage.jsonl"))
        self.assertEqual([r["event"] for r in ledger], ["request", "response"])
        self.assertEqual(ledger[1]["agent_id"], "user_simulator")
        self.assertEqual(ledger[1]["tokens"]["cache_input_tokens"], 4)
        self.assertFalse((self.agent.log_dir / "model_usage.jsonl").exists())
        raw_log = (self.agent.log_dir / "user_simulator_responses.jsonl").read_text()
        self.assertNotIn("private-test-key", raw_log)
        self.assertEqual(len(list(read_records(self.agent.log_dir / "conversation.jsonl"))), 2)

    async def test_fast_router_dispatches_explicit_clarification_without_a_routing_model_call(self):
        config = self.fixture.config.orchestration
        config.fast_router = FastRouterConfig(enabled=True, verification_reserve_seconds=0)
        config.max_model_calls = 2
        self.fixture.factory.subagents.register(self.agent)
        question = "Which certificate should I use?"
        intent = SubgoalIntent(
            subgoal_id="clarify-certificate", goal="Identify the correct certificate",
            success_criteria=["The user identifies the certificate"],
            budget={"max_model_calls": 0, "max_tool_calls": 1, "max_seconds": 10},
            user_question=question,
        )
        with patch("northcua.agents.desktop.runtime.fast_router.DecisionClient.from_env") as router:
            result = await self.fixture.run_script([
                {"action": "CONTINUE", "contract": intent.model_dump()}, {"action": "SUCCESS"},
            ])

        self.assertEqual(result.status.value, "success")
        router.assert_not_called()
        self.assertEqual(self.fixture.device.calls, [])
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.model_calls, 2)
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.tool_calls, 1)
        self.assertEqual(len(self.bodies), 1)
        self.assertEqual(self.bodies[0]["messages"][-1]["content"], question)
        cycle = result.steps[0].orchestration
        self.assertEqual(cycle.routing[0].reason, "user_clarification")
        self.assertEqual(cycle.attempts[0].contract.allowed_tools, ["ask_user"])
        self.assertTrue(self.fixture.model.contexts[0]["can_ask_user"])
        answer = self.fixture.model.contexts[1]["previous_result"]["summary"]["answer"]
        self.assertIn("updated certificate", answer)

    async def test_user_api_failure_is_logged_without_fabricated_reply_or_retry(self):
        self.failure = True
        request = SimpleNamespace(session_id="s", attempt_id="a", round_num=1)
        original_backend = self.agent.model_client.create_backend
        original_generate = self.agent.model_client.generate_chat
        with self.assertRaises(APIStatusError):
            await self.agent.ask({"question": "A missing detail?"}, request=request)
        self.assertEqual(len(self.bodies), 1)
        self.assertIs(self.agent.model_client.create_backend, original_backend)
        self.assertIs(self.agent.model_client.generate_chat, original_generate)
        conversation = list(read_records(self.agent.log_dir / "conversation.jsonl"))
        self.assertEqual([r["event"] for r in conversation], ["question"])
        usage = list(read_records(self.agent.log_dir / "user_simulator_usage.jsonl"))
        self.assertEqual([r["event"] for r in usage], ["request", "failure"])

    async def test_cancellation_drains_one_submitted_question_and_preserves_returned_answer(self):
        self.ready, self.release = threading.Event(), threading.Event()
        request = SimpleNamespace(session_id="s", attempt_id="a", round_num=1)
        task = asyncio.create_task(self.agent.ask({"question": "Updated document?"}, request=request))
        self.assertTrue(await asyncio.to_thread(self.ready.wait, 3))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.bodies), 1)
        records = list(read_records(self.agent.log_dir / "conversation.jsonl"))
        self.assertEqual([r["event"] for r in records], ["question", "answer"])
        usage = list(read_records(self.agent.log_dir / "user_simulator_usage.jsonl"))
        self.assertEqual([r["event"] for r in usage], ["request", "response"])

    async def test_expired_question_does_not_call_user_model(self):
        request = SimpleNamespace(session_id="s", attempt_id="a", round_num=1)
        with self.assertRaisesRegex(TimeoutError, "expired before dispatch"):
            await self.agent.ask({"question": "Do not send expired request"}, request=request, max_seconds=-1)
        self.assertEqual(self.bodies, [])

    def test_service_is_unregistered_without_explicit_private_spec(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("JEVDUO_USER_SIMULATOR_SPEC", None)
            self.fixture.make_runtime([])
        self.assertNotIn("user", [s["agent_id"] for s in self.fixture.factory.subagents.descriptors()])
