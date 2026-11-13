"""Actual SDK response capture and bounded orchestration recovery, without a VM."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace

import httpx
from nexau.archs.llm.llm_config import LLMConfig
from nexau.core.messages import Message, Role, TextBlock
from northcua.agentinfra.context.adapters.provider_guard import (
    ProviderContextBudgetGuard,
    install_provider_context_budget_guard,
)
from northcua.agentinfra.nexau_compat.adapters.openai_chat import (
    capture_chat_responses,
    chat_capture_caller,
    register_chat_capture_caller,
    wrap_openai_chat_client,
)
from northcua.agentinfra.nexau_compat.adapters.providers import LegacyLLMCallRequest, NexAUProviderAdapter
from northcua.agentkernel.tracing.capture import TraceCaptureService
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.agentkit.orchestration.runtime import read_records
from northcua.agentkit.runtime import AgentRoundRuntimeSpec
from northcua.core.budget import AgentStepBudget
from openai import AsyncOpenAI, OpenAI

from tests import test_orchestration as fixtures


def completion(content="", *, finish="stop", reasoning=None, tools=None, refusal=None):
    message = {"role": "assistant", "content": content, "tool_calls": tools, "refusal": refusal}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chat-test",
        "object": "chat.completion",
        "created": 1,
        "model": "scripted",
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
        "upstream_extra": {"termination": "retained"},
    }


class ChatCaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_single_tool_setting_reaches_actual_sdk_request(self):
        bodies = []

        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=completion("ok"))

        config = LLMConfig(model="scripted", api_type="openai_chat_completion",
                           api_key="test-only", base_url="https://unused.invalid/v1",
                           parallel_tool_calls=False)
        adapter = NexAUProviderAdapter()
        async with AsyncOpenAI(api_key="test-only", max_retries=0, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
        )) as client:
            caller = adapter.create_llm_caller(
                openai_client=object(), async_openai_client=client, llm_config=config,
            )
            self.addCleanup(caller.shutdown_thread_pool)
            await adapter.call_llm_async(caller, LegacyLLMCallRequest(
                messages=[Message(role=Role.USER, content=[TextBlock(text="decide")])],
                tool_call_mode="structured",
                tools=[{"name": "finish_task", "description": "Return a claim",
                        "input_schema": {"type": "object", "properties": {}}}],
            ))
        self.assertEqual(len(bodies), 1)
        self.assertIs(bodies[0].get("parallel_tool_calls"), False)
        self.assertEqual(bodies[0]["tools"][0]["function"]["name"], "finish_task")

    async def test_async_capture_does_not_block_event_loop(self):
        release = threading.Event()

        async def create(**kwargs):
            return SimpleNamespace(model_dump=lambda **kw: {"id": "test"})

        def capture(payload):
            self.assertTrue(release.wait(0.3), "capture blocked the event-loop timer")

        client = wrap_openai_chat_client(
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        )
        timer = asyncio.get_running_loop().call_later(0.01, release.set)
        try:
            with capture_chat_responses(capture):
                await client.chat.completions.create()
        finally:
            timer.cancel()

    async def test_real_nexau_adapter_captures_before_reasoning_only_normalization(self):
        adapter = NexAUProviderAdapter()
        captured = []
        llm = LLMConfig(
            model="scripted",
            api_type="openai_chat_completion",
            api_key="test",
            base_url="https://unused.invalid/v1",
        )
        client = AsyncOpenAI(
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json=completion(reasoning="still thinking", finish="length")
                    )
                )
            ),
        )
        caller = adapter.create_llm_caller(openai_client=object(), async_openai_client=client, llm_config=llm)
        self.addCleanup(caller.shutdown_thread_pool)
        async with client:
            with capture_chat_responses(captured.append):
                result = await adapter.call_llm_async(
                    caller,
                    LegacyLLMCallRequest(
                        messages=[Message(role=Role.USER, content=[TextBlock(text="decide")])],
                    ),
                )
        self.assertEqual(result.content, "")
        self.assertEqual(result.reasoning_content, "still thinking")
        self.assertEqual(captured[0]["choices"][0]["finish_reason"], "length")
        self.assertEqual(len(captured), 1)

    async def test_async_sdk_preserves_envelope_extras_and_scope(self):
        bodies, captured = [], []

        def handle(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=completion("ok", finish="length"))

        async with AsyncOpenAI(
            api_key="test-secret",
            base_url="https://unused.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as raw:
            client = wrap_openai_chat_client(raw)
            self.assertIs(wrap_openai_chat_client(client), client)
            with capture_chat_responses(captured.append):
                response = await client.chat.completions.create(model="scripted", messages=[])
            await client.chat.completions.create(model="scripted", messages=[])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], response.model_dump(mode="json"))
        self.assertEqual(captured[0]["choices"][0]["finish_reason"], "length")
        self.assertEqual(captured[0]["upstream_extra"], {"termination": "retained"})
        self.assertNotIn("test-secret", json.dumps(captured))
        self.assertEqual(bodies, [{"model": "scripted", "messages": []}] * 2)

    async def test_sync_sdk_capture_and_callback_abort_preserve_identity(self):
        class Abort(BaseException):
            pass

        def abort(payload):
            self.assertEqual(payload["usage"]["total_tokens"], 17)
            raise Abort()

        with OpenAI(
            api_key="test",
            base_url="https://unused.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion("ok")))
            ),
        ) as raw:
            client = wrap_openai_chat_client(raw)
            with capture_chat_responses(abort), self.assertRaises(Abort):
                client.chat.completions.create(model="scripted", messages=[])
            # Context reset after failure; no callback leaks into the next call.
            response = client.chat.completions.create(model="scripted", messages=[])
            self.assertEqual(response.choices[0].message.content, "ok")

    async def test_stream_is_passed_through_without_consumption(self):
        sentinel = object()
        raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: sentinel)))
        captured = []
        with capture_chat_responses(captured.append):
            result = wrap_openai_chat_client(raw).chat.completions.create(stream=True)
        self.assertIs(result, sentinel)
        self.assertEqual(captured, [])

    async def test_concurrent_scopes_do_not_mix_responses(self):
        async def run(label):
            async def create(**kwargs):
                await asyncio.sleep(0)
                return SimpleNamespace(model_dump=lambda **kw: {"id": label})

            client = wrap_openai_chat_client(
                SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            )
            records = []
            with capture_chat_responses(records.append):
                await client.chat.completions.create()
            return records

        self.assertEqual(await asyncio.gather(run("a"), run("b")), [[{"id": "a"}], [{"id": "b"}]])

    async def test_nested_thread_and_inherited_task_scopes(self):
        response = SimpleNamespace(model_dump=lambda **kw: {"id": "test"})
        client = wrap_openai_chat_client(
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
        )
        outer, inner = [], []
        resume = asyncio.Event()

        async def inherited():
            await resume.wait()
            client.chat.completions.create()

        with capture_chat_responses(outer.append):
            with capture_chat_responses(inner.append):
                await asyncio.to_thread(client.chat.completions.create)
            client.chat.completions.create()
            child = asyncio.create_task(inherited())
        resume.set()
        await child
        self.assertEqual(outer, [{"id": "test"}])
        self.assertEqual(inner, [{"id": "test"}])

    async def test_auxiliary_caller_is_excluded_even_inside_model_step_capture(self):
        records = []
        caller, summarizer = object(), object()
        response = SimpleNamespace(model_dump=lambda **kw: {"id": "selected"})
        client = wrap_openai_chat_client(
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
        )
        with capture_chat_responses(records.append):
            register_chat_capture_caller(caller)
            with chat_capture_caller(summarizer):
                client.chat.completions.create()
            with chat_capture_caller(caller):
                await asyncio.to_thread(client.chat.completions.create)
        self.assertEqual(records, [{"id": "selected"}])


class SDKModel:
    """Real SDK, NexAU caller and provider budget guard; scripted HTTP only."""

    def __init__(self, http_handler):
        self.adapter = NexAUProviderAdapter()
        self.llm = LLMConfig(
            model="scripted",
            api_type="openai_chat_completion",
            api_key="test",
            base_url="https://unused.invalid/v1",
        )
        self.client = AsyncOpenAI(
            api_key="test",
            max_retries=0,
            base_url="https://unused.invalid/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(http_handler)),
        )
        self.caller = self.adapter.create_llm_caller(
            openai_client=object(),
            async_openai_client=self.client,
            llm_config=self.llm,
            retry_attempts=5,  # Empty-response signal must bypass all five internal tries.
        )

    async def close(self):
        await self.client.close()
        self.caller.shutdown_thread_pool()

    async def run_step(self, **kwargs):
        install_provider_context_budget_guard(
            self.caller,
            ProviderContextBudgetGuard(model="scripted"),
            before_dispatch=kwargs["before_provider_call"],
        )
        messages = kwargs["build_envelope"]([], None)
        result = await self.adapter.call_llm_async(self.caller, LegacyLLMCallRequest(messages=messages))
        state = TraceCaptureState(llm_config=self.llm)
        TraceCaptureService(self.llm)._after_model(
            SimpleNamespace(model_response=result, original_response=result.content), state
        )
        return SimpleNamespace(response_text=result.content or "", trace_state=state)


class EmptyResponseRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.OrchestrationTest()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.requests = []
        self.responses = []

    def model(self, responses):
        queue = iter(responses)

        async def handle(request):
            body = json.loads(request.content)
            self.requests.append(body)
            payload = next(queue)
            if callable(payload):
                payload = payload(body)
            if isinstance(payload, BaseException):
                raise payload
            self.responses.append(payload)
            return httpx.Response(200, json=payload)

        model = SDKModel(handle)
        self.addAsyncCleanup(model.close)
        return model

    @staticmethod
    def success(body):
        content = body["messages"][-1]["content"]
        payload = json.loads(content if isinstance(content, str) else content[0]["text"])
        identity = payload["observation"]["observation_id"]
        return completion(
            json.dumps(
                {
                    "action": "SUCCESS",
                    "summary": "verified",
                    "verification": "current image",
                    "observation_id": identity,
                    "evidence_refs": [identity],
                }
            )
        )

    async def run_with(self, responses):
        runtime = self.fixture.make_runtime([])
        runtime.step_runtime.step_executor.model = self.model(responses)
        return await runtime.run(
            AgentRoundRuntimeSpec(
                query="open the dialog",
                budget=AgentStepBudget(max_round_steps=4),
                fallback_round_steps=4,
                begin_new_round=False,
            )
        )

    def receipts(self):
        return list(read_records(self.fixture.store.root / "provider_responses.jsonl"))

    async def test_reasoning_only_then_success_preserves_all_bodies_and_counts(self):
        result = await self.run_with([completion(reasoning="Peter ..."), self.success])
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.model_calls, 2)
        receipts = self.receipts()
        self.assertEqual([r["attempt"] for r in receipts], [1, 2])
        self.assertEqual(len({r["inference_id"] for r in receipts}), 1)
        self.assertEqual(receipts[0]["response"]["choices"][0]["message"]["reasoning_content"], "Peter ...")
        self.assertEqual(receipts[0]["response"]["usage"]["completion_tokens"], 7)
        trace = list(read_records(self.fixture.store.llm_trace_log_path))[-1]["llm_trace"]
        self.assertEqual(trace["stop_reason"], "stop")
        self.assertEqual(trace["response"]["provider_response"], receipts[-1]["response"])
        self.assertEqual((self.fixture.store.root / "provider_responses.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.fixture.device.calls, [])

    async def test_pure_empty_and_whitespace_stop_at_three_despite_nexau_retries(self):
        result = await self.run_with([completion(), completion("  \n"), completion(reasoning="loop")])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.receipts()), 3)
        self.assertEqual(self.fixture.rounds.state.orchestration.usage.model_calls, 3)
        self.assertEqual(result.status.value, "error")
        self.assertIn("EmptyModelResponseError", result.terminal_decision.final_answer)
        self.assertIn("attempt=3", result.terminal_decision.final_answer)
        self.assertIn("finish_reason=stop", result.terminal_decision.final_answer)

    async def test_retry_can_be_disabled(self):
        self.fixture.config.orchestration.empty_response_retries = 0
        result = await self.run_with([completion()])
        self.assertEqual(result.status.value, "error")
        self.assertEqual(len(self.requests), 1)

    async def test_global_budget_blocks_retry_before_second_send(self):
        self.fixture.config.orchestration.max_model_calls = 1
        result = await self.run_with([completion(), self.success])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(len(self.receipts()), 1)

    async def test_refusal_and_filter_do_not_retry(self):
        result = await self.run_with([completion(refusal="refused", finish="content_filter")])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.receipts()[0]["finish_reason"], "content_filter")
        self.assertEqual(result.status.value, "error")

    async def test_invalid_nonempty_json_has_bounded_decision_recovery(self):
        result = await self.run_with([completion("not JSON")] * 3)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("ValidationError", result.terminal_decision.final_answer)
        self.assertEqual(len(self.receipts()), 3)

    async def test_tool_response_is_not_retried_or_executed_by_slow(self):
        result = await self.run_with(
            [
                completion(
                    tools=[
                        {
                            "id": "call-test",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"},
                        }
                    ],
                    finish="tool_calls",
                )
            ]
        )
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Slow model attempted tool execution", result.terminal_decision.final_answer)
        self.assertEqual(self.receipts()[0]["finish_reason"], "tool_calls")
        self.assertEqual(self.fixture.device.calls, [])

    async def test_cancellation_propagates_with_prior_empty_receipt(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_with([completion(), asyncio.CancelledError()])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.receipts()), 1)

    async def test_torn_receipt_fails_without_model_retry(self):
        path = self.fixture.store.root / "provider_responses.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"incomplete":')
        result = await self.run_with([self.success])
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Provider response capture failed", result.terminal_decision.final_answer)
        self.assertEqual(path.read_text(), '{"incomplete":')

    async def test_length_finish_is_retained_on_every_attempt(self):
        result = await self.run_with([completion(reasoning="loop", finish="length")] * 3)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([r["finish_reason"] for r in self.receipts()], ["length"] * 3)
        self.assertIn("finish_reason=length", result.terminal_decision.final_answer)

    async def test_subgoal_limit_and_parent_verification_reserve_survive_retry(self):
        self.fixture.factory.subagents.register(fixtures.FakeSubagent())
        self.fixture.config.orchestration.max_model_calls = 3

        def delegate(body):
            payload = json.loads(self.success(body)["choices"][0]["message"]["content"])
            payload.update(
                action="CONTINUE",
                contract=fixtures.contract(
                    budget={"max_model_calls": 1, "max_tool_calls": 1, "max_seconds": 30},
                ).model_dump(),
            )
            return completion(json.dumps(payload))

        result = await self.run_with([delegate, completion(reasoning="empty subgoal answer"), self.success])
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(self.requests), 3)  # Parent + child + reserved parent verification.
        state = self.fixture.rounds.state.orchestration
        self.assertEqual(state.usage.model_calls, 3)
        self.assertEqual(state.previous_result.usage.model_calls, 1)
        self.assertEqual(state.previous_result.exit_reason, "BudgetExceeded")
        self.assertEqual(len(self.fixture.device.calls), 1)  # No tool replay while retrying the model.
        self.assertTrue(self.receipts()[1]["agent_path"].startswith("subagents/fake/"))

    async def test_elapsed_wall_budget_prevents_retry_send(self):
        self.fixture.config.orchestration.max_seconds = 100

        def expire(body):
            self.fixture.rounds.state.orchestration.started_at = time.time() - 101
            return completion()

        result = await self.run_with([expire, self.success])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.terminal_decision.reason.value, "budget_exhausted")
        self.assertEqual(len(self.receipts()), 1)
