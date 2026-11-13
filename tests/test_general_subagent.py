"""General fallback: native tool/result replay, guest routing, budgets and interruption."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from nexau.archs.main_sub.execution.hooks import AfterModelHookInput, BeforeModelHookInput
from nexau.archs.main_sub.execution.model_response import ModelResponse, ModelToolCall
from nexau.core.messages import ImageBlock, Message, Role, ToolResultBlock, ToolUseBlock
from northcua.agentinfra.providers.provider_payloads import build_provider_request_payload
from northcua.agentkit.orchestration.contracts import OrchestrationState, SubagentRequest, SubagentResult
from northcua.agentkit.orchestration.runtime import Journal, RunBudget, ScopedRuntime
from northcua.agents.desktop.runtime.contracts import DesktopRuntimeOptions
from northcua.agents.desktop.subagents.general import GeneralSubagent, active_screenshots, observation_message
from northcua.agents.desktop.subagents.general_environment import (
    _WRAPPER,
    TOOL_MODELS,
    GeneralEnvironment,
    validate_gui_code,
)
from northcua.agents.desktop.subagents.general_model import assistant_message, configure_tools
from northcua.core.config import DesktopPolicyConfig
from northcua.core.config_base import GeneralSubagentConfig, OrchestrationConfig
from northcua.core.errors import MCPRegistryError
from northcua.core.models import AssistantToolCallRecord
from northcua.transport.ws.agent_config import RuntimeAgentConfigBuilder
from PIL import Image

from tests import test_orchestration as fixtures


def tool(name, arguments, identity="call-1"):
    return Message(role=Role.ASSISTANT, content=[ToolUseBlock(id=identity, name=name, input=arguments)])


class GeneralSubagentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.picture = self.root / "screen.png"
        Image.new("RGB", (64, 48), "white").save(self.picture)
        self.desktop = DesktopPolicyConfig(backend="osworld", osworld={"vm_ip": "current-task-vm"})
        self.agent = GeneralSubagent(GeneralSubagentConfig(enabled=True), self.desktop)
        self.request = SubagentRequest(
            session_id="session",
            round_num=1,
            attempt_id="attempt",
            task="Draw a shape and save it",
            contract=fixtures.contract(
                agent_id="general",
                allowed_tools=["execute_pyautogui_code", "run_command", "wait_for_state"],
                budget={"max_model_calls": 5, "max_tool_calls": 3, "max_seconds": 10},
            ),
            observation={"observation_id": "before", "screenshot_path": str(self.picture)},
        )
        self.state = OrchestrationState(round_num=1, started_at=time.time())
        self.budget = RunBudget(
            OrchestrationConfig(max_model_calls=10), self.state, Journal(self.root / "log.jsonl")
        )
        self.prompts, self.surfaces = [], []
        self.observer = AsyncMock(
            return_value={"observation_id": "after", "screenshot_path": str(self.picture)}
        )
        self.executor = AsyncMock(
            return_value={
                "ok": True,
                "stdout": "真实输出\n",
                "stderr": "",
                "returncode": 0,
                "timed_out": False,
            }
        )

    def runtime(self, responses):
        replies = iter(responses)

        async def infer(messages, tools, charge):
            charge()
            self.prompts.append(messages)
            self.surfaces.append({item["name"] for item in tools})
            return next(replies)

        runtime = ScopedRuntime(
            self.request,
            self.budget,
            self.observer,
            self.executor,
            AsyncMock(),
            reserve_model_calls=1,
            infer_tools=infer,
        )
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def test_gui_and_shell_replay_real_results_with_correlated_ids_and_images(self):
        runtime = self.runtime(
            [
                tool("execute_pyautogui_code", {"code": "pyautogui.dragTo(10, 20, duration=0.2)"}, "drag"),
                tool("run_command", {"command": "ls /home/user/Desktop"}, "inspect"),
                tool("finish_task", {"status": "success", "answer": "saved"}, "finish"),
            ]
        )
        result = await self.agent.run(self.request, runtime)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(self.executor.await_count, 2)
        self.assertEqual(self.observer.await_count, 2)
        self.assertEqual(runtime.usage.model_calls, 3)
        self.assertEqual(runtime.usage.tool_calls, 2)
        content = [block for message in self.prompts[-1] for block in message.content]
        outputs = [block for block in content if isinstance(block, ToolResultBlock)]
        self.assertEqual([block.tool_use_id for block in outputs], ["drag", "inspect"])
        self.assertEqual(json.loads(outputs[0].content)["stdout"], "真实输出\n")
        self.assertTrue(
            any(
                isinstance(block, ImageBlock) and block.url.startswith("data:image/png;") for block in content
            )
        )

    async def test_gui_mode_and_contract_both_gate_shell_before_execution(self):
        for gui_mode in (True, False):
            with self.subTest(gui_mode=gui_mode):
                agent = GeneralSubagent(
                    GeneralSubagentConfig(mode="gui" if gui_mode else "hybrid"), self.desktop
                )
                if not gui_mode:
                    self.request.contract.allowed_tools.remove("run_command")
                runtime = self.runtime(
                    [
                        tool("run_command", {"command": "forbidden"}),
                        tool("finish_task", {"status": "failure", "answer": "no shell"}),
                    ]
                )
                result = await agent.run(self.request, runtime)
                self.assertEqual(result.outcome, "blocked")
                self.assertNotIn("run_command", self.surfaces[-1])
                self.executor.assert_not_awaited()
                self.assertEqual(runtime.usage.tool_calls, 0)

    async def test_invalid_gui_syntax_is_reported_as_unexecuted_tool_result(self):
        self.executor.side_effect = SyntaxError("invalid GUI code")
        runtime = self.runtime(
            [
                tool("execute_pyautogui_code", {"code": "pyautogui.click("}, "invalid"),
                tool("finish_task", {"status": "failure", "answer": "invalid code"}),
            ]
        )
        await self.agent.run(self.request, runtime)
        outputs = [
            block
            for message in self.prompts[-1]
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        self.assertEqual(outputs[0].tool_use_id, "invalid")
        self.assertEqual(json.loads(outputs[0].content)["delivery"], "not_sent")

    async def test_unknown_delivery_exits_without_a_second_model_or_action(self):
        self.executor.return_value = {"ok": False, "delivery": "unknown", "unsafe_to_replay": True}
        runtime = self.runtime([tool("run_command", {"command": "one write"})])
        result = await self.agent.run(self.request, runtime)
        self.assertEqual(result.exit_reason, "unsafe_to_replay")
        self.assertTrue(json.loads(result.summary)["unresolved_delivery"])
        self.assertEqual((runtime.usage.model_calls, self.executor.await_count), (1, 1))

    async def test_multi_call_response_executes_nothing_and_returns_each_skipped_id(self):
        runtime = self.runtime(
            [
                Message(
                    role=Role.ASSISTANT,
                    content=[
                        ToolUseBlock(id="one", name="run_command", input={"command": "write"}),
                        ToolUseBlock(
                            id="two", name="finish_task", input={"status": "success", "answer": "done"}
                        ),
                    ],
                ),
                tool("finish_task", {"status": "failure", "answer": "blocked"}),
            ]
        )
        await self.agent.run(self.request, runtime)
        self.executor.assert_not_awaited()
        outputs = [block for message in self.prompts[-1] if message.role == Role.TOOL
                   for block in message.content if isinstance(block, ToolResultBlock)]
        self.assertEqual([block.tool_use_id for block in outputs], ["one", "two"])
        # Both receipts must survive the actual legacy Chat message projection.
        projected = [message._as_legacy_openai_chat_dict() for message in self.prompts[-1]
                     if message.role == Role.TOOL]
        self.assertEqual([message["tool_call_id"] for message in projected], ["one", "two"])
        self.assertTrue(all(json.loads(message["content"])["delivery"] == "not_sent"
                            for message in projected))
        self.assertTrue(all(block.is_error for block in outputs))

    async def test_one_action_followed_by_empty_screenshot_uses_one_effect_and_one_capture(self):
        runtime = self.runtime([
            Message(role=Role.ASSISTANT, content=[
                ToolUseBlock(id="action", name="run_command", input={"command": "single action"}),
                ToolUseBlock(id="capture", name="take_screenshot", input={}),
            ]),
            tool("finish_task", {"status": "success", "answer": "verified"}),
        ])
        result = await self.agent.run(self.request, runtime)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(self.executor.await_count, 1)
        self.assertEqual(self.observer.await_count, 1)
        self.assertEqual(runtime.usage.tool_calls, 1)
        projected = [message._as_legacy_openai_chat_dict() for message in self.prompts[-1]
                     if message.role == Role.TOOL]
        self.assertEqual([message["tool_call_id"] for message in projected], ["action", "capture"])
        self.assertEqual(json.loads(projected[1]["content"])["observation_id"], "after")

    async def test_appended_capture_never_replays_an_unknown_write(self):
        self.executor.return_value = {"ok": False, "delivery": "unknown", "unsafe_to_replay": True}
        runtime = self.runtime([Message(role=Role.ASSISTANT, content=[
            ToolUseBlock(id="action", name="run_command", input={"command": "single write"}),
            ToolUseBlock(id="capture", name="take_screenshot", input={}),
        ])])
        result = await self.agent.run(self.request, runtime)
        self.assertEqual(result.exit_reason, "unsafe_to_replay")
        self.assertEqual(self.executor.await_count, 1)
        self.observer.assert_not_awaited()
        self.assertEqual(runtime.usage.model_calls, 1)

    async def test_multiple_actions_reversed_capture_or_invalid_capture_execute_nothing(self):
        invalid = [
            [ToolUseBlock(id="first", name="run_command", input={"command": "one"}),
             ToolUseBlock(id="second", name="run_command", input={"command": "two"})],
            [ToolUseBlock(id="first", name="take_screenshot", input={}),
             ToolUseBlock(id="second", name="run_command", input={"command": "one"})],
            [ToolUseBlock(id="first", name="run_command", input={"command": "one"}),
             ToolUseBlock(id="second", name="take_screenshot", input={"unexpected": True})],
        ]
        for calls in invalid:
            with self.subTest(names=[call.name for call in calls]):
                self.state.usage.model_calls = 0
                self.request.contract.budget.max_model_calls = 2
                runtime = self.runtime([Message(role=Role.ASSISTANT, content=calls),
                                        tool("finish_task", {"status": "failure", "answer": "no action"})])
                await self.agent.run(self.request, runtime)
                self.executor.assert_not_awaited()
                self.observer.assert_not_awaited()

    async def test_model_budget_returns_to_parent_and_preserves_verification_reserve(self):
        self.budget.config.max_model_calls = 2
        runtime = self.runtime([tool("execute_pyautogui_code", {"code": "pyautogui.click(1, 2)"})])
        result = await self.agent.run(self.request, runtime)
        self.assertEqual(result.outcome, "yielded")
        self.assertEqual(self.state.usage.model_calls, 1)
        self.assertEqual(self.observer.await_count, 1)
        self.executor.assert_not_awaited()

    async def test_model_timeout_retains_confirmed_receipts_without_replaying_an_action(self):
        runtime = self.runtime([tool("run_command", {"command": "read input"})])
        original = runtime._infer_tools

        async def infer(messages, tools, charge):
            if runtime.usage.model_calls:
                charge()
                raise TimeoutError("scripted model timeout")
            return await original(messages, tools, charge)

        runtime._infer_tools = infer
        with self.assertRaises(TimeoutError) as caught:
            await self.agent.run(self.request, runtime)
        partial = caught.exception.subagent_result
        self.assertEqual(partial.outcome, "failed")
        self.assertEqual(partial.last_observation["observation_id"], "after")
        receipt = json.loads(partial.summary)["tool_results"][0]
        self.assertEqual(receipt["result"]["stdout"], "真实输出\n")
        self.assertEqual(self.executor.await_count, 1)
        self.assertEqual(runtime.usage.model_calls, 2)

    async def test_subgoal_deadline_preserves_partial_handoff_on_timeout_cause(self):
        runtime = self.runtime([tool("run_command", {"command": "read input"})])
        original = runtime._infer_tools

        async def infer(messages, tools, charge):
            if runtime.usage.model_calls:
                charge()
                await asyncio.sleep(5)
            return await original(messages, tools, charge)

        runtime._infer_tools = infer
        with self.assertRaises(TimeoutError) as caught:
            async with asyncio.timeout(0.1):
                await self.agent.run(self.request, runtime)
        partial = caught.exception.__cause__.subagent_result
        self.assertEqual(partial.outcome, "yielded")
        self.assertEqual(partial.last_observation["observation_id"], "after")
        self.assertEqual(json.loads(partial.summary)["tool_results"][0]["result"]["stdout"], "真实输出\n")
        self.assertEqual(self.executor.await_count, 1)

    async def test_observation_failure_retains_the_completed_action_receipt(self):
        self.observer.side_effect = TimeoutError("scripted observation failure")
        runtime = self.runtime([tool("run_command", {"command": "one write"})])
        with self.assertRaises(TimeoutError) as caught:
            await self.agent.run(self.request, runtime)
        partial = caught.exception.subagent_result
        self.assertEqual(partial.outcome, "failed")
        self.assertEqual(partial.last_observation["observation_id"], "before")
        receipt = json.loads(partial.summary)["tool_results"][0]
        self.assertEqual(receipt["result"]["stdout"], "真实输出\n")
        self.assertEqual(self.executor.await_count, 1)
        self.assertEqual(runtime.usage.model_calls, 1)

    async def test_missing_or_invalid_screenshot_prevents_any_model_or_action(self):
        self.picture.write_text("invalid image")
        runtime = self.runtime([])
        with self.assertRaises(OSError):
            await self.agent.run(self.request, runtime)
        self.assertEqual(self.state.usage.model_calls, 0)
        self.executor.assert_not_awaited()

    async def test_cancel_during_model_never_executes_following_tool(self):
        entered = asyncio.Event()

        async def infer(messages, tools, charge):
            charge()
            entered.set()
            await asyncio.Event().wait()

        runtime = self.runtime([])
        runtime._infer_tools = infer
        task = asyncio.create_task(self.agent.run(self.request, runtime))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.executor.assert_not_awaited()

    def test_gui_language_validation_does_not_allow_python_escape(self):
        validate_gui_code("import pyautogui\npyautogui.click(1, 2)\npyperclip.copy('中文')")
        for code in (
            "import os",
            "open('/tmp/write','w')",
            "pyautogui.os.system('id')",
            "pyautogui.click(*[1, 2])",
            "pyautogui.write(str(1))",
            "x = 1",
        ):
            with self.subTest(code=code), self.assertRaises((ValueError, SyntaxError)):
                validate_gui_code(code)

    def test_screenshot_window_retains_all_actual_tool_results(self):
        messages = []
        for index in range(8):
            messages.extend(
                [
                    tool("run_command", {"command": "one"}, str(index)),
                    Message(
                        role=Role.TOOL, content=[ToolResultBlock(tool_use_id=str(index), content="actual")]
                    ),
                    observation_message(self.request.observation),
                ]
            )
        projected = active_screenshots(messages)
        blocks = [block for message in projected for block in message.content]
        self.assertEqual(sum(isinstance(block, ImageBlock) for block in blocks), 3)
        self.assertEqual(sum(isinstance(block, ToolResultBlock) for block in blocks), 8)

    def test_malformed_native_arguments_fail_before_execution(self):
        state = SimpleNamespace(
            tool_executions=[],
            reasoning="",
            reasoning_encrypted_content="",
            assistant_text="",
            assistant_tool_calls=[AssistantToolCallRecord(id="x", name="run_command", arguments="[]")],
        )
        with self.assertRaises(ValueError):
            assistant_message(SimpleNamespace(trace_state=state, response_text=""))


class GeneralEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = GeneralEnvironment(
            DesktopPolicyConfig(
                backend="osworld",
                osworld={"vm_ip": "current-task-vm", "server_port": 5050},
            )
        )
        self.requests = []
        self.client = httpx.AsyncClient

    def response(self, request):
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "returncode": 0,
                "output": json.dumps(
                    {
                        "stdout": "actual",
                        "stderr": "failure detail",
                        "returncode": 3,
                        "timed_out": False,
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                    }
                ),
            },
        )

    def mock_http(self, handler):
        return patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: self.client(
                **kwargs,
                transport=httpx.MockTransport(handler),
            ),
        )

    async def test_gui_execution_endpoint_refuses_shell_without_posting(self):
        self.environment.mode = "gui"
        with self.mock_http(self.response), self.assertRaisesRegex(ValueError, "GUI mode"):
            await self.environment.execute("run_command", {"command": "forbidden"})
        self.assertEqual(self.requests, [])

    async def test_current_vm_routing_and_nonzero_result_preserve_real_diagnostics(self):
        with self.mock_http(self.response):
            result = await self.environment.execute("run_command", {"command": "command", "cwd": "/tmp"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["stderr"], "failure detail")
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(str(self.requests[0].url), "http://current-task-vm:5050/execute")
        payload = json.loads(self.requests[0].content)
        self.assertFalse(payload["shell"])
        spec = json.loads(payload["command"][-1])
        self.assertEqual(spec["argv"], ["bash", "-lc", "command"])
        self.assertEqual(spec["cwd"], "/tmp")

    async def test_post_transport_failure_is_unknown_and_not_retried(self):
        def lost(request):
            self.requests.append(request)
            raise httpx.ReadTimeout("lost reply", request=request)

        with self.mock_http(lost):
            result = await self.environment.execute("run_command", {"command": "one write"}, max_seconds=1.01)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["delivery"], "unknown")
        self.assertTrue(result["unsafe_to_replay"])

    async def test_cancel_joins_post_before_returning_and_does_not_replay(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(request):
            entered.set()
            await release.wait()
            return self.response(request)

        with self.mock_http(blocked):
            task = asyncio.create_task(self.environment.execute("run_command", {"command": "one write"}))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.requests), 1)

    async def test_guest_timeout_is_bounded_by_remaining_subgoal_time(self):
        with self.mock_http(self.response):
            await self.environment.execute("run_command", {"command": "one", "timeout": 90}, max_seconds=3)
        spec = json.loads(json.loads(self.requests[0].content)["command"][-1])
        self.assertLessEqual(spec["timeout"], 2)

    def test_guest_wrapper_output_truncation_and_timeout_with_isolated_children(self):
        with tempfile.TemporaryDirectory() as cwd:
            for argv, timed_out in ((["/usr/bin/printf", "x" * 40000], False), (["/bin/sleep", "5"], True)):
                with self.subTest(timed_out=timed_out):
                    spec = {"argv": argv, "cwd": cwd, "timeout": 1}
                    response = subprocess.run(
                        [sys.executable, "-c", _WRAPPER, json.dumps(spec)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    result = json.loads(response.stdout)
                    self.assertEqual(result["timed_out"], timed_out)
                    if not timed_out:
                        self.assertTrue(result["stdout_truncated"])
                        self.assertEqual(len(result["stdout"]), 32768)


class GeneralIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_general_tools_survive_runtime_build_and_reject_inline_execution(self):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        config = harness.service.agent_config.model_copy(deep=True)
        definitions = [
            {"name": name, "description": description, "parameters": model.model_json_schema()}
            for name, (model, description) in TOOL_MODELS.items()
        ]
        configure_tools(config, definitions)
        builder = RuntimeAgentConfigBuilder()
        config = await builder.build(agent_config=config)
        config = await builder.build(agent_config=config)
        self.assertEqual([item.name for item in config.tools], list(TOOL_MODELS))
        arguments = {
            "execute_pyautogui_code": {"code": "pyautogui.click(1, 2)"},
            "run_command": {"command": "echo test"},
            "take_screenshot": {},
            "wait_for_state": {"seconds": 1},
            "finish_task": {"status": "success", "answer": "done"},
        }
        for item, definition in zip(config.tools, definitions, strict=True):
            self.assertEqual(item.input_schema, definition["parameters"])
            with self.assertRaisesRegex(RuntimeError, "scoped runtime"):
                await item.implementation(**arguments[item.name])
        # The runtime still rejects projections that were modified after authoring.
        config.tools[0].description = "stale declaration"
        with self.assertRaises(MCPRegistryError):
            await builder.build(agent_config=config)

    async def test_interrupted_delivery_signal_survives_checkpoint_recovery(self):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        runtime = harness.factory.build_round_runtime(options=DesktopRuntimeOptions())
        await runtime.session_runtime.hydrate()
        ports = runtime.step_runtime.step_executor
        ports.state.pending = SubagentRequest(
            session_id="test",
            round_num=1,
            attempt_id="cancelled-attempt",
            task="test",
            contract=fixtures.contract(),
            observation={},
        )
        ports.state.previous_result = SubagentResult(
            subgoal_id="open-dialog",
            attempt_id="cancelled-attempt",
            outcome="blocked",
            exit_reason="unsafe_to_replay",
            summary='{"unresolved_delivery": true}',
        )
        ports.journal.write("dispatch_cancelled", ports.state)
        await runtime.session_runtime.hydrate()
        await ports.collect(SimpleNamespace(cursor=SimpleNamespace(session_step=1)))
        self.assertIsNone(ports.state.pending)
        self.assertEqual(ports.state.previous_result.exit_reason, "unsafe_to_replay")
        self.assertTrue(json.loads(ports.state.previous_result.summary)["unresolved_delivery"])

    async def test_nested_dispatch_deadline_preserves_unknown_delivery_and_never_replays(self):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        harness.config.desktop.backend = "osworld"
        harness.config.orchestration.general.enabled = True
        picture = harness.root / "screen.png"
        Image.new("RGB", (16, 16)).save(picture)
        original_collect = harness.observer.collect

        async def collect(*args, **kwargs):
            context = await original_collect(*args, **kwargs)
            context.current_clean_screenshot_path = str(picture)
            return context

        harness.observer.collect = collect
        original_step = fixtures.ScriptedModel.run_step
        posts = []
        native_calls = []

        async def infer(model, **kwargs):
            if not kwargs.get("configure_agent"):
                return await original_step(model, **kwargs)
            kwargs["before_provider_call"]()
            native_calls.append("requested")
            return SimpleNamespace(
                response_text="",
                trace_state=fixtures.TraceCaptureState(
                    assistant_tool_calls=[
                        AssistantToolCallRecord(
                            id="write",
                            name="run_command",
                            arguments=json.dumps({"command": "one write"}),
                        )
                    ],
                ),
            )

        async def hanging_post(request):
            posts.append(request)
            await asyncio.Event().wait()

        client = httpx.AsyncClient
        request_contract = fixtures.contract(
            agent_id="general",
            allowed_tools=["execute_pyautogui_code", "run_command"],
            budget={"max_model_calls": 2, "max_tool_calls": 1, "max_seconds": 1.5},
        )
        with (
            patch.object(fixtures.ScriptedModel, "run_step", infer),
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kwargs: client(
                    **kwargs,
                    transport=httpx.MockTransport(hanging_post),
                ),
            ),
        ):
            await harness.run_script(
                [
                    {"action": "CONTINUE", "contract": request_contract.model_dump()},
                    {"action": "FAIL"},
                ]
            )
        previous = harness.model.contexts[-1]["previous_result"]
        self.assertEqual(previous["outcome"], "blocked")
        self.assertEqual(previous["exit_reason"], "unsafe_to_replay")
        self.assertTrue(json.loads(previous["summary"])["unresolved_delivery"])
        self.assertEqual((len(posts), len(native_calls)), (1, 1))

    async def test_real_model_executor_serializes_native_tools_images_and_result_history(self):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        picture = harness.root / "screen.png"
        Image.new("RGB", (16, 16)).save(picture)

        class NativeTransport:
            def __init__(self):
                self.prepared = []
                self.failure = None

            async def prepare_agent(self, *, agent_config, **kwargs):
                agent_config = await RuntimeAgentConfigBuilder().build(agent_config=agent_config)
                self.prepared.append(agent_config)
                return SimpleNamespace(
                    config=agent_config,
                    tool_call_payload=[item.to_structured_definition() for item in agent_config.tools],
                    global_storage={},
                    executor=SimpleNamespace(
                        update_structured_tools=lambda tools: None,
                        llm_caller=SimpleNamespace(
                            _call_with_retry=self.provider, _call_with_retry_async=self.provider_async
                        ),
                    ),
                )

            def provider(self, _params):
                if self.failure is not None:
                    raise self.failure
                return ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            call_id="native-call",
                            name="run_command",
                            arguments={"command": "inspect"},
                        )
                    ]
                )

            async def provider_async(self, params):
                return self.provider(params)

            async def run_local_round(self, *, messages, agent_config, agent, **kwargs):
                before = BeforeModelHookInput(
                    agent_state=SimpleNamespace(), messages=messages, max_iterations=1, current_iteration=0
                )
                for hook in agent_config.before_model_hooks:
                    hook(before)
                response = agent.executor.llm_caller._call_with_retry(
                    SimpleNamespace(messages=messages, api_params={})
                )
                after = AfterModelHookInput(
                    agent_state=SimpleNamespace(),
                    messages=messages,
                    max_iterations=1,
                    current_iteration=0,
                    model_response=response,
                )
                for hook in agent_config.after_model_hooks:
                    hook(after)
                return ("", {}), agent

        transport = NativeTransport()
        harness.service.execution_transport = transport
        runtime = harness.factory.build_round_runtime(options=DesktopRuntimeOptions())
        await runtime.session_runtime.hydrate()
        ports = runtime.step_runtime.step_executor
        context = SimpleNamespace(
            cursor=SimpleNamespace(session_id="test", session_step=1, round_num=1, round_step=1), query="test"
        )
        messages = [observation_message({"observation_id": "image", "screenshot_path": str(picture)})]
        definition = [
            {
                "name": "run_command",
                "description": "VM shell",
                "parameters": TOOL_MODELS["run_command"][0].model_json_schema(),
            }
        ]
        first = await ports._infer_step(
            messages,
            lambda: ports.budget.charge("model_calls"),
            context,
            "subagents/general/attempt",
            definition,
        )
        reply = assistant_message(first)
        self.assertEqual(reply.content[0].name, "run_command")
        messages.extend(
            [
                reply,
                Message(
                    role=Role.TOOL,
                    content=[
                        ToolResultBlock(tool_use_id="native-call", content='{"stdout":"actual"}'),
                    ],
                ),
            ]
        )
        await ports._infer_step(
            messages,
            lambda: ports.budget.charge("model_calls"),
            context,
            "subagents/general/attempt",
            definition,
        )
        self.assertEqual(ports.state.usage.model_calls, 2)
        self.assertEqual(transport.prepared[0].tool_call_mode, "structured")
        self.assertIs(transport.prepared[0].llm_config.extra_params.get("parallel_tool_calls"), False)
        self.assertEqual(len(harness.service.agent_config.tools), 0)
        traces = [json.loads(line) for line in harness.store.llm_trace_log_path.read_text().splitlines()]
        request = traces[-1]["llm_trace"]["request"]
        self.assertIs(request.get("parallel_tool_calls"), False)
        self.assertIn("messages", request, request)
        self.assertEqual([item["name"] for item in request["tools"]], ["run_command"])
        wire = build_provider_request_payload(
            llm_config=harness.service.agent_config.llm_config,
            messages=messages,
            provider_tools=first.trace_state.provider_tools,
        ).request_payload
        self.assertEqual([item["function"]["name"] for item in wire["tools"]], ["run_command"])
        self.assertIn("data:image/png;base64,", json.dumps(request))
        self.assertEqual(request["messages"][-1]["tool_call_id"], "native-call")
        self.assertIn("actual", request["messages"][-1]["content"])
        transport.failure = TimeoutError("scripted model timeout")
        with self.assertRaises(TimeoutError):
            await ports._infer_step(
                messages, lambda: ports.budget.charge("model_calls"), context,
                "subagents/general/attempt", definition,
            )
        failure_path = harness.store.root / "provider_failures.jsonl"
        failure = json.loads(failure_path.read_text().splitlines()[-1])
        self.assertIs(failure["request_snapshot"]["parallel_tool_calls"], False)
        self.assertIn("actual", failure["request_snapshot"]["messages"][-1]["content"])
        self.assertIsNone(failure["response"])
        self.assertIsNone(failure["usage"])
        self.assertEqual(failure["error_type"], "TimeoutError")
        self.assertEqual(failure_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ports.state.usage.model_calls, 3)

    async def test_registered_fallback_runs_inside_parent_and_parent_verifies_new_observation(self):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        harness.config.desktop.backend = "osworld"
        harness.config.orchestration.general.enabled = True
        picture = harness.root / "screen.png"
        Image.new("RGB", (16, 16)).save(picture)
        original_collect = harness.observer.collect

        async def collect(*args, **kwargs):
            context = await original_collect(*args, **kwargs)
            context.current_clean_screenshot_path = str(picture)
            return context

        harness.observer.collect = collect
        execute = AsyncMock(return_value={"ok": True, "stdout": "actual result"})
        original_step = fixtures.ScriptedModel.run_step
        seen = []

        async def infer(model, **kwargs):
            if not kwargs.get("configure_agent"):
                return await original_step(model, **kwargs)
            kwargs["before_provider_call"]()
            seen.append(kwargs["build_envelope"]([], None))
            name, arguments = (
                ("run_command", {"command": "inspect"})
                if len(seen) == 1
                else ("finish_task", {"status": "success", "answer": "done"})
            )
            state = fixtures.TraceCaptureState(
                assistant_tool_calls=[
                    AssistantToolCallRecord(
                        id=f"call-{len(seen)}", name=name, arguments=json.dumps(arguments)
                    ),
                ]
            )
            return SimpleNamespace(response_text="", trace_state=state)

        request_contract = fixtures.contract(
            agent_id="general",
            allowed_tools=["execute_pyautogui_code", "run_command"],
            budget={"max_model_calls": 3, "max_tool_calls": 2, "max_seconds": 5},
        )
        with (
            patch.object(fixtures.ScriptedModel, "run_step", infer),
            patch.object(GeneralEnvironment, "execute", execute),
        ):
            result = await harness.run_script(
                [
                    {"action": "CONTINUE", "contract": request_contract.model_dump()},
                    {"action": "SUCCESS"},
                ]
            )
        self.assertEqual(result.status.value, "success")
        self.assertEqual(harness.factory.subagents.descriptors()[0]["agent_id"], "general")
        self.assertEqual(harness.model.contexts[-1]["previous_result"]["outcome"], "achieved")
        before = harness.model.contexts[0]["observation"]["observation_id"]
        after = harness.model.contexts[1]["observation"]["observation_id"]
        self.assertNotEqual(before, after)
        self.assertEqual(harness.rounds.state.orchestration.usage.model_calls, 4)
        self.assertEqual(harness.rounds.state.orchestration.usage.tool_calls, 1)
        self.assertEqual(harness.device.calls, [])
        self.assertIn("subagents/general/", harness.store.llm_trace_log_path.read_text())
        self.assertTrue(
            any(
                isinstance(block, ToolResultBlock) and "actual result" in block.content
                for message in seen[-1]
                for block in message.content
            )
        )
        execute.assert_awaited_once()

    def test_native_backend_rejected_and_config_does_not_register_implicitly(self):
        with self.assertRaisesRegex(ValueError, "desktop.backend=osworld"):
            GeneralSubagent(GeneralSubagentConfig(), DesktopPolicyConfig())
        self.assertFalse(OrchestrationConfig().general.enabled)
        for model, _description in TOOL_MODELS.values():
            self.assertEqual(model.model_json_schema()["additionalProperties"], False)
