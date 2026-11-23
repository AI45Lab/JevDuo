"""Real Jev loop and NorthCUA scoped handles with scripted HTTP, no GUI or model API."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from northcua.agentinfra.providers.token_usage import ModelUsageLedger

pytest.importorskip("jev_desktop", reason="optional gui_jev integration requires agent-desktop/jev")

from northcua.agentkit.orchestration.contracts import OrchestrationState, SubagentRequest
from northcua.agentkit.orchestration.runtime import Journal, RunBudget, ScopedRuntime, read_records
from northcua.agents.desktop.subagents import jev
from northcua.agents.desktop.subagents.jev_trajectory import JevTrajectory, TrajectoryWriteError
from northcua.core.config import DesktopPolicyConfig
from northcua.core.config_base import GuiJevConfig, GuiJevDecisionConfig, OrchestrationConfig

from tests import test_orchestration as fixtures


def success(data=None):
    return {"ok": True, "data": {"disposition": {"delivery": "delivered_verified"}, **(data or {})}}


def failure(*, safe=False):
    return {
        "ok": False,
        "error": {
            "code": "ACTION_FAILED",
            "disposition": {
                "delivery": "not_delivered" if safe else "delivery_uncertain",
                "retry": "safe" if safe else "unsafe",
            },
        },
    }


class ScriptedHTTP:
    def __init__(self):
        self.commands, self.decisions, self.urls = [], [], []
        self.raw_commands = []
        self.capabilities = ["SetValue"]
        self.choices = iter(["1", "TYPE_TEXT", "DONE"])
        self.application_choices = iter(["app_0"])
        self.window = {
            "id": "w1",
            "title": "Document",
            "app_name": "Fixture",
            "pid": 100,
            "process_instance": "fixture:1",
        }
        self.windows = [copy.deepcopy(self.window)]
        self.apps = [{"name": "Fixture", "pid": 100, "process_instance": "fixture:1"}]
        self.catalog = [{"id": "fixture.desktop", "name": "Fixture"}]
        self.launch_data = {
            "app": "Fixture",
            "pid": 100,
            "process_instance": "fixture:1",
            "window": self.window,
        }
        self.launch_failure = self.after_launch_windows = self.after_launch_apps = None
        self.focus_failure = None
        self.window_refreshes = None
        self.launched = False
        self.launch_ready = self.launch_release = None
        self.model_pause_after = 3
        self.value, self.clipboard = "private-old-value", "original clipboard"
        self.focused = False
        self.set_failure, self.bad_snapshot = None, False
        self.model_ready = self.model_release = self.write_ready = self.write_release = None
        self.paste_ready = self.paste_release = None
        self.statuses = []
        self.cleanup_failure = False
        self.no_window = False
        self.snapshot_tree = None
        self.chat_requests = []
        self.chat_finish_reason = "stop"
        self.chat_usage = None

    async def __call__(self, request):
        body = json.loads(request.content)
        self.urls.append(str(request.url))
        if request.url.host in {"jev.example", "chat.example"}:
            chat = request.url.host == "chat.example"
            if chat:
                self.chat_requests.append(copy.deepcopy(body))
                body = {**json.loads(body["messages"][1]["content"]), "model": body["model"]}
            self.decisions.append(body)
            if self.statuses:
                return httpx.Response(self.statuses.pop(0), json={})
            question = next(iter(body["questions"]))
            if self.model_ready is not None and len(self.decisions) >= self.model_pause_after:
                self.model_ready.set()
                await self.model_release.wait()
            if body["questions"][question]["type"] == "noul":
                answer = {"type": "noul", "noul": 0.0}
            else:
                choice = next(self.application_choices if question == "application" else self.choices)
                answer = {"type": "choice", "choice": choice, "confidence": 0, "probabilities": {choice: 1}}
            result = {"answers": {question: answer}}
            if chat:
                result = {"choices": [{"finish_reason": self.chat_finish_reason,
                                       "message": {"role": "assistant", "content": json.dumps(result)}}]}
                if self.chat_usage is not None:
                    result["usage"] = self.chat_usage
            return httpx.Response(200, json=result)
        assert request.url.host == "current-vm"
        assert body["shell"] is False
        argv = body["command"]
        if argv[0] == "python3":
            self.commands.append(["catalog"])
            assert argv[1:] == ["-c", jev.CATALOG]
            result = success({"apps": self.catalog})
            return httpx.Response(200, json={"output": json.dumps(result), "returncode": 0})
        assert argv[0] == "/vm/agent-desktop"
        self.raw_commands.append(copy.deepcopy(argv))
        args = argv[1:]
        headed = False
        while args:
            if args[0] in ("--session", "--agent-id"):
                args = args[2:]
            elif args[0] == "--headed":
                headed = True
                args = args[1:]
            else:
                break
        self.commands.append(copy.deepcopy(args))
        command = args[0]
        result = success()
        if command == "snapshot":
            result = success(
                {
                    "app": "Fixture",
                    "window": {
                        "id": args[args.index("--window-id") + 1] if "--window-id" in args else "w1",
                        "title": "Document",
                    },
                    "complete": True,
                    "tree": {
                        "role": "window",
                        "children": [
                            {
                                "ref_id": "@s1:e1",
                                "role": "textfield",
                                "name": "Body",
                                "available_actions": self.capabilities,
                                "value": self.value,
                                "states": ["focused"] if self.focused else [],
                                "bounds": {"x": 1, "y": 2, "width": 10, "height": 20},
                            }
                        ],
                    },
                }
            )
            if self.snapshot_tree is not None:
                result["data"]["tree"] = copy.deepcopy(self.snapshot_tree)
            if self.bad_snapshot:
                result["data"]["tree"]["children"][0]["name"] = "Different field"
        elif command == "list-surfaces":
            result = success({"surfaces": []})
        elif command == "set-value":
            if self.write_ready is not None:
                self.write_ready.set()
                await self.write_release.wait()
            if self.set_failure is not None:
                result = self.set_failure
            else:
                self.value = args[2]
                if headed and "SetValue" not in self.capabilities:
                    result = success({"details": {"route": "physical_text_replacement"}})
        elif command == "cursor-overlay" and args[1] == "disable" and self.cleanup_failure:
            result = failure()
        elif command == "clipboard-get":
            result = success({"type": "text", "text": self.clipboard})
        elif command == "clipboard-clear":
            self.clipboard = ""
        elif command == "clipboard-set":
            if self.cleanup_failure and args[1] == "original clipboard":
                result = failure()
            else:
                self.clipboard = args[2] if args[1] == "--" else args[1]
        elif command == "focus":
            self.focused = True
        elif command == "press":
            if args[1] == "ctrl+c":
                self.clipboard = self.value
            elif args[1] == "ctrl+v":
                self.value = self.clipboard
                if self.paste_ready is not None:
                    self.paste_ready.set()
                    await self.paste_release.wait()
        elif command == "get":
            result = success({"value": self.value})
        elif command == "session":
            result = success({"session_id": "cursor-session"})
        elif command == "status":
            result = success({"platform": "linux"})
        elif command == "list-apps":
            result = success({"apps": self.apps})
        elif command == "list-windows":
            if self.launched and self.window_refreshes is not None:
                self.windows = next(self.window_refreshes)
            result = {"ok": True, "data": self.windows}
        elif command == "focus-window":
            result = self.focus_failure or success()
        elif command == "launch":
            if self.launch_ready is not None:
                self.launch_ready.set()
                await self.launch_release.wait()
            self.no_window = False
            self.launched = True
            if self.after_launch_windows is not None:
                self.windows = self.after_launch_windows
            if self.after_launch_apps is not None:
                self.apps = self.after_launch_apps
            result = self.launch_failure or success(self.launch_data)
        if command == "snapshot" and self.no_window:
            result = {"ok": False, "error": {"code": "WINDOW_NOT_FOUND"}}
        return httpx.Response(
            200, json={"output": json.dumps(result), "returncode": 0 if result["ok"] else 1}
        )


class JevTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "TYPESAFE_API_KEY": "fixture-key",
                "TYPESAFE_BASE_URL": "https://jev.example/decisions",
                "TYPESAFE_MODEL": "configured-jev",
                "AGENT_DESKTOP_BIN": "/vm/agent-desktop",
                "AGENT_DESKTOP_VM_URL": "http://wrong-vm:9999",
                "AGENT_DESKTOP_SESSION": "configured-session",
                "AGENT_DESKTOP_AGENT_ID": "configured-agent",
                "JEV_VERIFY_MS": "17",
                "AGENT_DESKTOP_SNAPSHOT_TIMEOUT_MS": "4321",
                "TEXT_MODEL": "must-not-be-used",
                "JEVDUO_JEV_CHAT_API_KEY": "fixture-chat-key",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http = ScriptedHTTP()
        client_type = httpx.AsyncClient
        client_patch = patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(self.http), **kwargs),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        pause = patch.object(jev._Bridge, "sleep", lambda self, seconds: None)
        pause.start()
        self.addCleanup(pause.stop)
        self.writer = AsyncMock(
            return_value=json.dumps({"text": "  中文\nsecond line  "}, ensure_ascii=False)
        )
        self.agent = self.make_agent()
        self.request = SubagentRequest(
            session_id="s",
            round_num=1,
            attempt_id="a",
            task="write a paragraph",
            contract=fixtures.contract(
                agent_id="gui_jev",
                goal="Fill Body",
                success_criteria=["Body holds the requested paragraph"],
                allowed_tools=["agent_desktop"],
                agent_input={"window_id": "w1"},
                budget={"max_model_calls": 10, "max_tool_calls": 100, "max_seconds": 10},
            ),
            observation={"observation_id": "o1"},
        )
        state = OrchestrationState(round_num=1, started_at=time.time())
        self.journal = Journal(self.root / "orchestration.jsonl")
        self.budget = RunBudget(OrchestrationConfig(max_seconds=30), state, self.journal)

        async def infer(prompt, charge):
            charge()
            return await self.writer(prompt)

        self.scoped = ScopedRuntime(
            self.request,
            self.budget,
            AsyncMock(return_value={"observation_id": "post-subagent"}),
            lambda name, args: self.agent.environment.execute(args),
            infer,
            lambda name, args: self.agent.environment.execute(args, cleanup=True),
        )
        self.addAsyncCleanup(self.scoped.close)

    def make_agent(self, **config):
        return jev.JevSubagent(
            GuiJevConfig(enabled=True, **config),
            DesktopPolicyConfig(backend="osworld", osworld={"vm_ip": "current-vm", "server_port": 5000}),
        )

    def chat_agent(self, **config):
        decision = {"backend": "chat_completions", "base_url": "https://chat.example/v1", "model": "k3.1-pro"}
        decision.update(config.pop("decision", {}))
        return self.make_agent(decision=decision, **config)

    def enable_trajectory(self, *, screenshots=True):
        from PIL import Image

        trace = JevTrajectory(self.root / "jev-trace", {"attempt_id": self.request.attempt_id},
                              screenshots=screenshots, secrets=("fixture-key", "fixture-chat-key"))
        count = 0

        async def observe():
            nonlocal count
            count += 1
            image = self.root / f"trace-observation-{count}.png"
            Image.new("RGB", (8, 8), (count % 255, 0, 0)).save(image)
            return {"observation_id": f"trace-observation-{count}", "screenshot_path": str(image)}

        self.scoped.trajectory = trace
        self.scoped._observe = observe
        return trace

    def trajectory_records(self, trace):
        return [json.loads(line) for line in trace.path.read_text().splitlines()]

    async def test_trajectory_persists_choices_native_results_and_archive_only_images(self):
        trace = self.enable_trajectory()
        self.agent = self.chat_agent()
        result = await self.agent.run(self.request, self.scoped)
        records = self.trajectory_records(trace)
        requests = [r for r in records if r["event"] == "decision_requested"]
        self.assertEqual([list(r["request"]["questions"]) for r in requests],
                         [["target"], ["operation"], ["target"]])
        replies = [r for r in records if r["event"] == "decision_returned"]
        self.assertEqual([r["decision_id"] for r in requests], [r["decision_id"] for r in replies])
        self.assertEqual(len([r for r in records if r["event"] == "model_response"]), 3)
        action = next(r for r in records if r["event"] == "action_started")
        self.assertEqual(action["decision_ids"], [r["decision_id"] for r in requests[:2]])
        self.assertEqual(action["arguments"]["text"], "  中文\nsecond line  ")
        images = [r for r in records if r["event"] == "screenshot"]
        self.assertEqual([r["phase"] for r in images], ["before_action", "after_action"])
        self.assertTrue(all(Path(r["observation"]["screenshot_path"]).is_file() for r in images))
        self.assertTrue(all(r["policy_input"] is False for r in images))
        self.assertTrue(all(r["action_id"] == action["action_id"] for r in images))
        native = [r for r in records if r["event"] == "native_started"]
        finished = [r for r in records if r["event"] == "native_finished"]
        self.assertEqual({r["native_call_id"] for r in native}, {r["native_call_id"] for r in finished})
        self.assertEqual(sum("set-value" in r["argv"] for r in native), 1)
        payloads = json.dumps(self.http.chat_requests)
        for image in images:
            self.assertNotIn(image["observation"]["screenshot_path"], payloads)
        self.assertEqual(result.outcome, "achieved")
        self.assertIn(str(trace.path), result.evidence_refs)
        self.assertEqual(self.scoped.usage.model_calls, 4)
        self.assertEqual(self.scoped.last_observation["observation_id"], "trace-observation-3")
        self.assertEqual(records[-1]["result"]["usage"]["model_calls"], 4)

    async def test_trajectory_invalid_chat_choice_keeps_raw_reply_and_never_acts(self):
        trace = self.enable_trajectory()
        self.agent = self.chat_agent()
        self.http.choices = iter(["999"])
        with self.assertRaisesRegex(RuntimeError, "no valid Jev answer"):
            await self.agent.run(self.request, self.scoped)
        records = self.trajectory_records(trace)
        self.assertTrue(any(r["event"] == "decision_failed" for r in records))
        raw = next(r for r in records if r["event"] == "model_response")["response_body"]
        self.assertIn("999", raw)
        self.assertFalse(any(r["event"] == "action_started" for r in records))
        self.assertEqual(records[-1]["event"], "attempt_finished")
        self.assertFalse(any(c[0] == "set-value" for c in self.http.commands))

    async def test_trajectory_native_uncertainty_is_durable_without_replay(self):
        trace = self.enable_trajectory()
        self.http.set_failure = failure()
        result = await self.agent.run(self.request, self.scoped)
        records = self.trajectory_records(trace)
        action = next(r for r in records if r["event"] == "action_finished")
        self.assertEqual(action["result"]["delivery"], "delivery_uncertain")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)
        self.assertTrue(json.loads(result.summary)["unsafe_to_replay"])
        self.assertEqual(result.outcome, "failed")

    async def test_trajectory_cancel_retains_native_receipt_before_stopping(self):
        trace = self.enable_trajectory()
        self.http.write_ready, self.http.write_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.write_ready.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.http.write_release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 10)
        records = self.trajectory_records(trace)
        receipt = next(r for r in records if r["event"] == "native_interrupted")
        self.assertEqual(receipt["delivery"], "delivered_verified")
        self.assertTrue(receipt["result"]["ok"])
        self.assertTrue(any(r["event"] == "screenshot_unavailable" and r["phase"] == "after_action"
                            for r in records))
        self.assertEqual(records[-1]["status"], "cancelled")
        self.assertTrue(json.loads(caught.exception.subagent_result.summary)["unresolved_delivery"])
        self.assertEqual(self.http.commands[-1][0], "set-value")

    async def test_trajectory_screenshot_failure_does_not_repeat_or_feed_policy(self):
        trace = self.enable_trajectory()
        observe = self.scoped._observe
        calls = 0

        async def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("screenshot failed")
            return await observe()

        self.scoped._observe = fail_once
        result = await self.agent.run(self.request, self.scoped)
        records = self.trajectory_records(trace)
        self.assertTrue(any(r["event"] == "screenshot_unavailable" and r["phase"] == "before_action"
                            for r in records))
        self.assertEqual(len([r for r in records if r["event"] == "screenshot"]), 1)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)

    async def test_trajectory_save_failure_after_write_preserves_replay_guard(self):
        trace = self.enable_trajectory()
        original = trace.record
        failed = False

        def record(event, payload):
            nonlocal failed
            if event == "native_finished" and self.http.value != "private-old-value" and not failed:
                failed = True
                raise TrajectoryWriteError(payload.get("result"))
            return original(event, payload)

        with patch.object(trace, "record", side_effect=record):
            with self.assertRaises(RuntimeError) as caught:
                await self.agent.run(self.request, self.scoped)
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertTrue(summary["unresolved_delivery"])
        self.assertEqual(summary["interrupted_action"]["delivery"], "delivered_verified")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)

    async def test_trajectory_cancel_before_dispatch_does_not_claim_an_unknown_write(self):
        trace = self.enable_trajectory()
        original = self.scoped._observe
        ready, release = asyncio.Event(), asyncio.Event()

        async def observe():
            ready.set()
            await release.wait()
            return await original()

        self.scoped._observe = observe
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(ready.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 10)
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertFalse(summary["unresolved_delivery"])
        self.assertNotIn("interrupted_action", summary)
        action = next(r for r in self.trajectory_records(trace) if r["event"] == "action_finished")
        self.assertFalse(action["dispatch_invoked"])
        self.assertFalse(any(c[0] == "set-value" for c in self.http.commands))

    async def test_trajectory_storage_failure_cannot_prevent_cursor_cleanup(self):
        trace = self.enable_trajectory()
        self.agent = self.make_agent(options={"cursor": True})
        original = trace.record

        def record(event, payload):
            if event == "native_started" and "disable" in payload.get("argv", []):
                raise TrajectoryWriteError()
            return original(event, payload)

        with patch.object(trace, "record", side_effect=record):
            result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(self.http.commands[-1], ["cursor-overlay", "disable"])
        self.assertEqual(self.scoped.usage.cleanup_tool_calls, 1)

    async def test_trajectory_stream_keeps_all_turns_beyond_summary_limit(self):
        trace = self.enable_trajectory(screenshots=False)
        self.agent = self.make_agent(limits={"steps": 55, "calls": 150, "stalled_turns": 100})
        self.http.capabilities = ["Click"]
        self.http.choices = iter([item for _ in range(45) for item in ("1", "CLICK")] + ["DONE"])
        self.request.contract.budget.max_model_calls = 150
        self.request.contract.budget.max_tool_calls = 1000
        self.budget.config.max_model_calls = 200
        self.budget.config.max_seconds = 120
        self.scoped.deadline = time.monotonic() + 120
        result = await self.agent.run(self.request, self.scoped)
        records = self.trajectory_records(trace)
        turns = [r["data"]["turn"] for r in records if r["event"] == "loop_event" and "turn" in r["data"]]
        self.assertEqual(len(turns), 45)
        self.assertEqual(turns[-1]["step"], 45)
        summary = json.loads(result.summary)
        self.assertTrue(summary["turns_truncated"])
        self.assertLessEqual(len(summary.get("turns", [])), 40)
        self.assertFalse(any(r["event"] == "screenshot" for r in records))

    async def test_chat_backend_preserves_requests_and_shared_field_writer(self):
        with patch.dict(os.environ):
            os.environ.pop("TYPESAFE_API_KEY", None)
            self.agent = self.chat_agent()
            result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.writer.assert_awaited_once()
        self.assertEqual(self.scoped.usage.model_calls, 4)
        self.assertEqual(len(self.http.chat_requests), 3)
        for raw, original in zip(self.http.chat_requests, self.http.decisions, strict=True):
            self.assertEqual(json.loads(raw["messages"][1]["content"]),
                             {k: v for k, v in original.items() if k != "model"})
            self.assertEqual(raw["model"], "k3.1-pro")
            self.assertFalse(raw["stream"])
            self.assertEqual(raw["response_format"], {"type": "json_object"})
            self.assertNotIn("tools", raw)
        self.assertTrue(all(url.startswith("https://chat.example/v1/chat/completions")
                            for url in self.http.urls if "current-vm" not in url))
        self.assertNotIn("fixture-chat-key", result.summary)

    async def test_chat_backend_also_selects_applications_and_answers_noul(self):
        self.agent = self.chat_agent(options={"confirm_destructive": True})
        self.request.contract.agent_input = {}
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        names = [next(iter(r["questions"])) for r in self.http.decisions]
        self.assertIn("application", names)
        self.assertIn("destructive", names)
        self.assertFalse(any(url.startswith("https://jev.example") for url in self.http.urls))

    async def test_chat_invalid_choice_or_truncated_completion_never_writes(self):
        for choice, reason in (("999", "stop"), ("1", "length")):
            with self.subTest(choice=choice, reason=reason):
                self.agent = self.chat_agent()
                self.http.choices = iter([choice])
                self.http.chat_finish_reason = reason
                with self.assertRaisesRegex(RuntimeError, "no valid Jev answer"):
                    await self.agent.run(self.request, self.scoped)
        self.assertEqual(len(self.http.chat_requests), 2)
        self.assertFalse(any(c[0] in {"set-value", "type", "press", "launch"} for c in self.http.commands))
        self.writer.assert_not_awaited()

    async def test_chat_retry_attempts_are_each_budgeted(self):
        self.agent = self.chat_agent(decision={"max_retries": 2})
        self.http.statuses = [429, 503]
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(len(self.http.chat_requests), 5)
        self.assertEqual(self.scoped.usage.model_calls, 6)
        self.assertIn('"model": "k3.1-pro"', self.journal.path.read_text())

    async def test_chat_usage_ledger_covers_retries_even_without_trajectory_recording(self):
        self.agent = self.chat_agent(decision={"max_retries": 2})
        self.http.statuses = [429, 503]
        self.http.chat_usage = {"prompt_tokens": 100, "completion_tokens": 30,
                                "prompt_tokens_details": {"cached_tokens": 80}}
        path = self.root / "model_usage.jsonl"
        self.scoped.model_usage = ModelUsageLedger(path, {"agent_id": "gui_jev", "purpose": "selection"})
        await self.agent.run(self.request, self.scoped)
        records = list(read_records(path))
        requests = [row for row in records if row["event"] == "request"]
        responses = [row for row in records if row["event"] == "response"]
        self.assertEqual(len(requests), 5)
        self.assertEqual(len(responses), 5)
        self.assertEqual([row["http_status"] for row in responses], [429, 503, 200, 200, 200])
        self.assertEqual(len({row["call_id"] for row in requests}), 5)
        self.assertTrue(all(row["tokens"]["input_tokens"] is None for row in responses[:2]))
        self.assertEqual(sum(row["tokens"]["input_tokens"] for row in responses[2:]), 300)
        self.assertEqual(sum(row["tokens"]["cache_input_tokens"] for row in responses[2:]), 240)
        self.assertNotIn("fixture-chat-key", path.read_text())

    async def test_chat_default_zero_retries_and_cancel_remain_bounded(self):
        self.agent = self.chat_agent()
        self.http.statuses = [429]
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            await self.agent.run(self.request, self.scoped)
        self.assertEqual(len(self.http.chat_requests), 1)
        self.http.model_ready, self.http.model_release = asyncio.Event(), asyncio.Event()
        self.http.model_pause_after = 1
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.model_ready.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertFalse(any(c[0] in {"set-value", "type", "press", "launch"} for c in self.http.commands))

    async def test_real_loop_preserves_jev_choices_and_uses_slow_writer(self):
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(self.http.value, "  中文\nsecond line  ")
        self.assertEqual(
            [list(r["questions"]) for r in self.http.decisions], [["target"], ["operation"], ["target"]]
        )
        self.assertTrue(all(r["model"] == "configured-jev" for r in self.http.decisions))
        self.writer.assert_awaited_once()
        self.assertEqual(self.scoped.usage.model_calls, 4)
        self.assertTrue(all("wrong-vm" not in url for url in self.http.urls))
        self.assertEqual(self.agent.desktop_config.verify_ms, 17)
        self.assertIn("4321", self.http.commands[0])
        self.assertEqual(self.agent.desktop_config.session, "configured-session")
        self.assertNotIn("second line", result.summary)
        self.assertNotIn("second line", self.journal.path.read_text())
        self.assertFalse(any(c[0] in ("list-apps", "list-windows", "launch") for c in self.http.commands))

    async def test_native_exception_retains_completed_turns_without_weakening_exception_semantics(self):
        self.http.choices = iter(["1", "TYPE_TEXT"])
        with self.assertRaises(RuntimeError) as caught:
            await self.agent.run(self.request, self.scoped)
        partial = caught.exception.subagent_result
        summary = json.loads(partial.summary)
        self.assertEqual(summary["turns"][0]["operation"], "TYPE_TEXT")
        self.assertEqual(partial.outcome, "failed")
        self.assertNotIn("second line", partial.summary)

    async def test_post_launch_inventory_exception_retains_the_launch_event(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1"])
        self.http.window_refreshes = iter([])
        with self.assertRaises(RuntimeError) as caught:
            await self.agent.run(self.request, self.scoped)
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertEqual(summary["turns"][0]["operation"], "LAUNCH")
        self.assertTrue(summary["turns"][0]["ok"])

    async def test_blocked_feedback_retains_context_choices_and_supported_operations(self):
        self.http.choices = iter(["BLOCKED"])
        self.agent = self.make_agent(options={"values": False})
        result = await self.agent.run(self.request, self.scoped)
        summary = json.loads(result.summary)
        self.assertEqual(summary["decision_probabilities"], {"BLOCKED": 1})
        self.assertIn("TYPE_TEXT", summary["available_operations"])
        self.assertIn("visible_context", summary["screen"])
        self.assertNotIn("private-old-value", result.summary)

    async def test_final_observation_failure_retains_completed_child_progress(self):
        with patch.object(self.scoped, "observe", side_effect=RuntimeError("capture failed")):
            with self.assertRaises(RuntimeError) as caught:
                await self.agent.run(self.request, self.scoped)
        partial = caught.exception.subagent_result
        summary = json.loads(partial.summary)
        self.assertEqual(summary["turns"][0]["operation"], "TYPE_TEXT")
        self.assertEqual(summary["stop"], "exception")
        self.assertEqual(partial.outcome, "failed")

    async def test_requested_snapshot_timeout_is_executable(self):
        self.request.contract.agent_input["snapshot_timeout_ms"] = 22000
        self.http.choices = iter(["DONE"])
        await self.agent.run(self.request, self.scoped)
        snapshot = next(command for command in self.http.commands if command[0] == "snapshot")
        self.assertEqual(snapshot[snapshot.index("--timeout-ms") + 1], "22000")

    async def test_framework_budget_tracks_automatic_snapshot_recovery(self):
        self.request.contract.agent_input["snapshot_timeout_ms"] = 20000
        self.http.choices = iter(["DONE"])
        original = self.http.__call__
        snapshots = []

        async def server(request):
            body = json.loads(request.content)
            argv = body.get("command", [])
            if "snapshot" not in argv:
                return await original(request)
            budget = int(argv[argv.index("--timeout-ms") + 1])
            snapshots.append((budget, request.extensions["timeout"]["read"]))
            if request.extensions["timeout"]["read"] < budget / 1000:
                raise httpx.ReadTimeout("recovery budget expired before native deadline", request=request)
            response = await original(request)
            if len(snapshots) == 1:
                envelope = response.json()
                native = json.loads(envelope["output"])
                native["data"].update(complete=False, identity_unproven=True)
                envelope["output"] = json.dumps(native)
                return httpx.Response(200, json=envelope)
            return response

        with patch.object(ScriptedHTTP, "__call__", lambda instance, request: server(request)):
            result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        # The newer SDK first rereads within the original identity-settle budget.
        # A complete second read needs no deadline escalation.
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], (20000, 30))
        self.assertTrue(0 < snapshots[1][0] <= 20000)
        self.assertGreater(snapshots[1][1], snapshots[1][0] / 1000)
        self.assertFalse(any(c[0] == "set-value" for c in self.http.commands))

    async def test_transport_diagnostics_survive_native_observation_exception(self):
        original = self.http.__call__

        async def server(request):
            body = json.loads(request.content)
            if "snapshot" in body.get("command", []):
                raise httpx.ReadTimeout("private-secret-url-and-text", request=request)
            return await original(request)

        with patch.object(ScriptedHTTP, "__call__", lambda instance, request: server(request)):
            with self.assertRaises(RuntimeError) as caught:
                await self.agent.run(self.request, self.scoped)
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertEqual(summary["blocked_reason"], "REMOTE_EXECUTION_UNCONFIRMED")
        self.assertEqual(len(summary["execution_diagnostics"]), 2)
        self.assertTrue(all(d["failure_kind"] == "timeout" for d in summary["execution_diagnostics"]))
        self.assertEqual(summary["recovery"]["snapshot_timeout_ms"], 4321)
        self.assertEqual(self.scoped.usage.model_calls, 0)
        self.assertNotIn("private-secret", json.dumps(summary))
        self.assertNotIn("private-secret", self.journal.path.read_text())

    async def test_supplied_text_and_no_values_reuse_original_options(self):
        self.agent = self.make_agent(options={"text": ["supplied"], "values": False})
        await self.agent.run(self.request, self.scoped)
        self.writer.assert_not_awaited()
        self.assertEqual(self.http.value, "supplied")
        self.assertNotIn("private-old-value", json.dumps(self.http.decisions))

    async def test_container_selection_binds_observed_label_without_consuming_supplied_text(self):
        self.request.contract.agent_input["text"] = ["reserved text for another field"]
        self.http.snapshot_tree = {
            "role": "window",
            "children": [
                {
                    "ref_id": "@s1:e1",
                    "role": "listbox",
                    "name": "Language",
                    "available_actions": ["Select"],
                    "children": [
                        {
                            "ref_id": "@s1:e2",
                            "role": "option",
                            "name": "English",
                            "available_actions": ["Select"],
                        },
                        {
                            "ref_id": "@s1:e3",
                            "role": "option",
                            "name": "French",
                            "available_actions": ["Select"],
                        },
                    ],
                }
            ],
        }
        self.http.choices = iter(["1", "SELECT", "2", "DONE"])
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(
            next(c for c in self.http.commands if c[0] == "select"), ["select", "@s1:e1", "French"]
        )
        self.writer.assert_not_awaited()
        self.assertEqual(self.http.decisions[-1]["state"]["text_input"]["remaining_values"], 1)
        self.assertEqual(self.scoped.usage.model_calls, 4)

    async def test_application_selection_launches_from_an_already_open_window(self):
        self.request.contract.agent_input = {"app": "unverified planner alias"}
        self.http.application_choices = iter(["app_1", "app_0"])
        self.http.choices = iter(["DONE"])
        self.agent = self.make_agent(launch_args={"fixture.desktop": ["--force-renderer-accessibility"]})
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertIn(["catalog"], self.http.commands)
        launches = [c for c in self.http.commands if c[0] == "launch"]
        self.assertEqual(len(launches), 1)
        self.assertIn("--arg=--force-renderer-accessibility", launches[0])
        launch_index = self.http.commands.index(launches[0])
        self.assertIn(["list-apps"], self.http.commands[launch_index + 1 :])
        self.assertIn(["list-windows"], self.http.commands[launch_index + 1 :])
        self.assertFalse(any(c[0] == "focus-window" for c in self.http.commands))
        self.assertEqual(self.http.decisions[1]["state"]["phase"], "after_launch")
        self.assertEqual(self.http.decisions[1]["state"]["running_applications"], self.http.apps)
        self.assertEqual(self.scoped.usage.model_calls, 3)
        self.writer.assert_not_awaited()

    async def test_unpinned_null_app_selects_live_window_without_launch(self):
        self.request.contract.agent_input = {"app": None}
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertFalse(any(c[0] == "launch" for c in self.http.commands))
        snapshots = [c for c in self.http.commands if c[0] == "snapshot"]
        self.assertTrue(all(c[c.index("--app") + 1] == "Fixture" for c in snapshots))
        self.assertTrue(all(c[c.index("--window-id") + 1] == "w1" for c in snapshots))
        self.assertEqual(self.scoped.usage.model_calls, 5)
        self.assertIn("application_selections", result.summary)

    async def test_invalid_application_choice_cannot_launch(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_999"])
        with self.assertRaises(ValueError):
            await self.agent.run(self.request, self.scoped)
        self.assertFalse(any(c[0] in ("launch", "focus-window", "set-value") for c in self.http.commands))

    async def test_uncertain_launch_is_not_repeated_or_followed_by_other_actions(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1"])
        self.http.launch_failure = failure()
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "action_failed")
        self.assertEqual(sum(c[0] == "launch" for c in self.http.commands), 1)
        self.assertFalse(any(c[0] in ("focus-window", "snapshot", "set-value") for c in self.http.commands))
        self.assertEqual(json.loads(result.summary)["error"]["code"], "ACTION_FAILED")

    async def test_uncertain_launch_cannot_replay_under_a_new_subgoal(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1", "app_1"])
        self.http.launch_failure = failure()
        first = await self.agent.run(self.request, self.scoped)
        self.request.history = [{"subagent_result": first.model_dump(mode="json")}]
        self.request.contract.subgoal_id = "new-launch-attempt"
        second = await self.agent.run(self.request, self.scoped)
        self.assertEqual(second.exit_reason, "unsafe_launch_replay_blocked")
        self.assertEqual(sum(command[0] == "launch" for command in self.http.commands), 1)

    async def test_launch_without_process_identity_refuses_arbitrary_window(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1"])
        self.http.launch_data = {"app": "Fixture"}
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "launch_identity_unconfirmed")
        self.assertFalse(any(c[0] == "focus-window" for c in self.http.commands))

    async def test_reused_pid_and_unrelated_window_cannot_satisfy_launch(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1"])
        self.http.after_launch_windows = [dict(self.http.window, process_instance="new-process")]
        original = jev._Desktop
        ticks = iter([0, 21])
        with patch.object(jev, "_Desktop", lambda *a, **kw: original(*a, **kw, clock=lambda: next(ticks))):
            result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "launched_app_has_no_observable_window")
        self.assertFalse(any(c[0] == "focus-window" for c in self.http.commands))
        self.assertEqual(sum(c[0] == "launch" for c in self.http.commands), 1)

    async def test_unresolved_global_delivery_blocks_another_launch(self):
        self.request.contract.agent_input = {}
        self.request.history = [{"subagent_result": {"summary": json.dumps({"unresolved_delivery": True})}}]
        self.http.application_choices = iter(["app_1"])
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "unsafe_launch_replay_blocked")
        self.assertFalse(any(command[0] == "launch" for command in self.http.commands))

    async def test_post_launch_jev_selects_one_of_multiple_verified_windows(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1", "app_1"])
        self.http.choices = iter(["DONE"])
        self.http.after_launch_windows = [
            dict(self.http.window, pid=999),
            self.http.window,
            dict(self.http.window, id="w2", title="Second document"),
        ]
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertFalse(any(c[0] == "focus-window" for c in self.http.commands))
        after = self.http.decisions[1]["questions"]["application"]["criteria"]
        self.assertEqual({after[k]["id"] for k in after if k.startswith("app_")}, {"w1", "w2"})

    async def test_preflight_selection_consumes_original_jev_model_limit(self):
        self.request.contract.agent_input = {}
        self.agent = self.make_agent(limits={"calls": 1})
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "yielded")
        self.assertEqual(self.scoped.usage.model_calls, 1)
        self.assertFalse(any(c[0] == "snapshot" for c in self.http.commands))

    async def test_pinned_missing_window_never_switches_or_launches(self):
        self.http.no_window = True
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "window_closed")
        self.assertFalse(any(c[0] in ("list-apps", "list-windows", "launch") for c in self.http.commands))

    async def test_selected_window_enters_loop_without_eager_focus(self):
        self.request.contract.agent_input = {}
        self.http.focus_failure = failure()
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertFalse(any(c[0] == "focus-window" for c in self.http.commands))
        self.assertTrue(any(c[0] == "set-value" for c in self.http.commands))

    async def test_native_activation_refusal_is_blocked_and_never_replays_input(self):
        self.request.contract.agent_input = {}
        self.http.set_failure = failure()
        self.http.set_failure["error"]["details"] = {
            "cause": "frame_not_active",
            "physical_delivery_started": False,
        }
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.exit_reason, "window_activation_unconfirmed")
        self.assertEqual(json.loads(result.summary)["error"], self.http.set_failure["error"])
        self.assertEqual(sum(c[0] == "set-value" for c in self.http.commands), 1)

    async def test_started_or_unknown_activation_delivery_stays_failed(self):
        for details in (
            {"cause": "frame_not_active", "physical_delivery_started": True},
            {"cause": "frame_not_active"},
        ):
            with self.subTest(details=details):
                self.http.choices = iter(["1", "TYPE_TEXT", "DONE"])
                self.http.set_failure = failure()
                self.http.set_failure["error"]["details"] = details
                result = await self.agent.run(self.request, self.scoped)
                self.assertEqual(result.outcome, "failed")
                self.assertEqual(result.exit_reason, "action_failed")

    async def test_launch_waits_for_verified_inventory_without_replaying_launch(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1", "app_0"])
        self.http.choices = iter(["DONE"])
        self.http.window_refreshes = iter([[], [self.http.window]])
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(sum(c[0] == "launch" for c in self.http.commands), 1)
        self.assertEqual(sum(c[0] == "list-windows" for c in self.http.commands), 3)

    async def test_cancel_application_selection_never_launches(self):
        self.request.contract.agent_input = {}
        self.http.model_pause_after = 1
        self.http.model_ready, self.http.model_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.model_ready.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        self.assertFalse(any(c[0] in ("launch", "focus-window", "snapshot") for c in self.http.commands))
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertFalse(summary["unresolved_delivery"])
        self.assertEqual(summary["unsafe_to_replay"], [])

    async def test_cancel_during_launch_joins_delivery_and_does_not_continue(self):
        self.request.contract.agent_input = {}
        self.http.application_choices = iter(["app_1"])
        self.http.launch_ready, self.http.launch_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.launch_ready.wait(), 3)
        task.cancel()
        await asyncio.sleep(0.03)
        self.assertFalse(task.done())
        self.http.launch_release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.http.commands[-1][0], "launch")
        self.assertEqual(sum(c[0] == "launch" for c in self.http.commands), 1)
        partial = caught.exception.subagent_result
        self.assertEqual(json.loads(partial.summary)["interrupted_action"]["operation"], "LAUNCH")
        self.request.history = [{"subagent_result": partial.model_dump(mode="json")}]
        self.request.contract.subgoal_id = "new-launch-after-cancel"
        self.http.application_choices = iter(["app_1"])
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "unsafe_launch_replay_blocked")
        self.assertEqual(sum(c[0] == "launch" for c in self.http.commands), 1)

    async def test_changed_field_after_text_generation_stops_before_write(self):
        async def generate(prompt):
            self.http.bad_snapshot = True
            return json.dumps({"text": "generated"})

        self.writer.side_effect = generate
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "screen_changed_during_text_generation")
        self.assertFalse(any(c[0] == "set-value" for c in self.http.commands))

    async def test_incomplete_writer_response_stops_before_write(self):
        self.writer.return_value = '{"text":"truncated'
        with self.assertRaises(RuntimeError):
            await self.agent.run(self.request, self.scoped)
        self.assertFalse(any(c[0] == "set-value" for c in self.http.commands))

    async def test_unknown_write_delivery_never_replays_or_pastes(self):
        self.http.set_failure = failure()
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "action_failed")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)
        self.assertFalse(any(c[0] in ("press", "clipboard-set") for c in self.http.commands))

    async def test_safe_refusal_does_not_retry_or_paste(self):
        self.http.set_failure = failure(safe=True)
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "action_failed")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)
        self.assertFalse(any(c[0].startswith("clipboard-") or c[0] == "press" for c in self.http.commands))

    async def test_headed_field_without_setvalue_uses_shared_slow_text(self):
        self.agent = self.make_agent(desktop={"headed": True})
        self.http.capabilities = ["Click", "SetFocus"]
        self.writer.return_value = json.dumps({"text": "同步 patched input"})
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(self.http.value, "同步 patched input")
        self.writer.assert_awaited_once()
        writes = [argv for argv in self.http.raw_commands if "set-value" in argv]
        self.assertEqual(len(writes), 1)
        self.assertIn("--headed", writes[0])
        self.assertIn("physical_text_replacement", result.summary)
        self.assertEqual(self.scoped.usage.model_calls, 4)
        self.assertFalse(any(c[0].startswith("clipboard-") for c in self.http.commands))

    async def test_clipboard_refusal_backend_uses_bridge_and_exact_value_evidence(self):
        self.agent = self.make_agent(desktop={"headed": True})
        self.http.capabilities = ["Click", "SetFocus"]
        self.http.set_failure = failure(safe=True)
        self.http.set_failure["error"]["code"] = "PLATFORM_NOT_SUPPORTED"
        text = "--image=/tmp/literal\n中文\tsecond line"
        self.writer.return_value = json.dumps({"text": text}, ensure_ascii=False)
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        self.assertEqual(self.http.value, text)
        self.assertIn(["clipboard-set", "--", text], self.http.commands)
        self.assertEqual(sum(c[0] == "press" and c[1] == "ctrl+v" for c in self.http.commands), 1)
        turn = json.loads(result.summary)["turns"][0]
        self.assertEqual(turn["route"], "clipboard_replace")
        self.assertEqual(turn["verification_scope"], "element_value")
        self.assertEqual(turn["application_commit"], "not_verified")
        self.assertNotIn(text, result.summary)

    async def test_invalid_resource_binding_stops_before_models_or_tools(self):
        for paths in (["relative.txt"], ["/tmp/a", "/tmp/a"], ["/tmp/a\nb"]):
            with self.subTest(paths=paths):
                self.request.contract.agent_input["resources"] = paths
                with self.assertRaises(ValueError):
                    await self.agent.run(self.request, self.scoped)
        self.assertEqual(self.http.commands, [])
        self.assertEqual(self.http.decisions, [])

    async def test_cancel_during_clipboard_paste_joins_and_retains_unresolved_write(self):
        self.agent = self.make_agent(desktop={"headed": True})
        self.http.capabilities = ["Click", "SetFocus"]
        self.http.set_failure = failure(safe=True)
        self.http.set_failure["error"]["code"] = "PLATFORM_NOT_SUPPORTED"
        self.http.paste_ready, self.http.paste_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.paste_ready.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.http.paste_release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertTrue(summary["unresolved_delivery"])
        self.assertEqual(summary["interrupted_action"]["operation"], "TYPE_TEXT")
        self.assertEqual(sum(c[0] == "press" and c[1] == "ctrl+v" for c in self.http.commands), 1)
        self.assertEqual(self.http.commands[-1][0], "press")
        self.assertNotIn("second line", caught.exception.subagent_result.summary)

    async def test_resource_choice_binds_literal_argument_and_preserves_uncertain_receipt(self):
        path = '/tmp/a "quoted" $(literal).txt'
        self.agent = self.make_agent(desktop={"headed": True}, options={"reconcile_observed_popups": True})
        self.request.contract.agent_input["resources"] = [path]
        self.http.choices = iter(["WINDOW", "OPEN_RESOURCE:0"])
        self.http.launch_failure = failure()
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.exit_reason, "action_failed")
        launches = [c for c in self.http.commands if c[0] == "launch"]
        self.assertEqual(launches, [["launch", "Fixture", "--arg=" + path, "--timeout", "5000"]])
        summary = json.loads(result.summary)
        self.assertEqual(summary["turns"][0]["details"]["application_commit"], "not_verified")
        self.assertEqual(summary["unsafe_to_replay"][0]["operation"], "OPEN_RESOURCE:*")
        self.assertIsNone(summary["unsafe_to_replay"][0]["window_id"])

    async def test_production_options_run_through_scope_review_and_completion_check(self):
        import yaml

        config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
        options = config["harness"]["orchestration"]["gui_jev"]["options"]
        self.agent = self.make_agent(desktop={"headed": True}, options=options)
        self.http.choices = iter(["1", "TYPE_TEXT", "within_request", "DONE", "satisfied"])
        result = await self.agent.run(self.request, self.scoped)
        self.assertEqual(result.outcome, "achieved")
        questions = [next(iter(r["questions"])) for r in self.http.decisions]
        self.assertIn("goal_scope", questions)
        self.assertIn("completion", questions)
        self.assertFalse(any("--owned-by" in c for c in self.http.commands))
        self.assertFalse(any(c[0] == "screenshot" for c in self.http.commands))

    def test_keys_limit_accepts_all_new_defaults_and_clipboard_backend_can_be_disabled(self):
        self.assertEqual(jev.JevInput(keys=list(self.agent.options.keys)).keys, list(self.agent.options.keys))
        self.assertGreater(len(self.agent.options.keys), 100)
        self.assertFalse(self.make_agent(desktop={"clipboard_text": False}).desktop_config.clipboard_text)
        with self.assertRaises(ValueError):
            jev.JevInput(keys=["tab"] * 193)

    async def test_budget_exhaustion_after_cursor_enable_still_disables_overlay(self):
        self.agent = self.make_agent(options={"cursor": True, "text": ["supplied"]})
        self.request.contract.budget.max_tool_calls = 4
        with self.assertRaisesRegex(RuntimeError, "subgoal_tool_calls_limit"):
            await self.agent.run(self.request, self.scoped)
        self.assertEqual(self.http.commands[-1], ["cursor-overlay", "disable"])
        self.assertEqual(self.scoped.usage.tool_calls, 4)
        self.assertEqual(self.budget.state.usage.cleanup_tool_calls, 1)

    async def test_cancel_joins_loop_and_disables_cursor(self):
        self.agent = self.make_agent(options={"cursor": True})
        self.http.model_ready, self.http.model_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.model_ready.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.http.commands[-1], ["cursor-overlay", "disable"])
        commands = list(self.http.commands)
        self.http.model_release.set()
        await asyncio.sleep(0)
        self.assertEqual(self.http.commands, commands)
        summary = json.loads(caught.exception.subagent_result.summary)
        self.assertEqual(summary["turns"][-1]["operation"], "TYPE_TEXT")
        self.assertFalse(summary["unresolved_delivery"])

    async def test_cancel_waits_for_submitted_write_before_teardown(self):
        self.http.write_ready, self.http.write_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.write_ready.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.http.write_release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)
        self.assertEqual(self.http.commands[-1][0], "set-value")
        partial = caught.exception.subagent_result
        summary = json.loads(partial.summary)
        self.assertTrue(summary["unresolved_delivery"])
        self.assertEqual(summary["interrupted_action"]["delivery"], "delivered_verified")
        self.assertNotIn("中文", partial.summary)

    async def expire_during_write(self, run):
        self.http.write_ready, self.http.write_release = asyncio.Event(), asyncio.Event()
        owner = asyncio.current_task()
        timeout = asyncio.timeout(None)

        async def expire_during_write():
            await self.http.write_ready.wait()
            timeout.reschedule(asyncio.get_running_loop().time() - 1)
            while not owner.cancelling():
                await asyncio.sleep(0)
            self.http.write_release.set()

        trigger = asyncio.create_task(expire_during_write())
        try:
            async with timeout:
                return await run()
        finally:
            self.http.write_release.set()
            trigger.cancel()
            await asyncio.gather(trigger, return_exceptions=True)

    async def test_timeout_preserves_delivery_and_blocks_new_subgoal_write(self):
        with self.assertRaises(TimeoutError) as caught:
            await self.expire_during_write(lambda: self.agent.run(self.request, self.scoped))
        partial = caught.exception.__cause__.subagent_result
        summary = json.loads(partial.summary)
        self.assertTrue(summary["unsafe_to_replay"])
        request = self.request.model_copy(deep=True)
        request.attempt_id = "new-attempt"
        request.contract.subgoal_id = "new-subgoal"
        request.history = [{"subagent_result": partial.model_dump(mode="json")}]
        scoped = ScopedRuntime(
            request, self.budget, self.scoped._observe, self.scoped._execute,
            self.scoped._infer, self.scoped._cleanup,
        )
        self.addAsyncCleanup(scoped.close)
        self.http.choices = iter(["1", "TYPE_TEXT", "DONE"])
        result = await self.agent.run(request, scoped)
        self.assertEqual(result.exit_reason, "unsafe_write_replay_blocked")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)

    async def test_timeout_preserves_native_uncertain_and_explicit_non_delivery(self):
        for safe in (False, True):
            with self.subTest(safe=safe):
                self.http.choices = iter(["1", "TYPE_TEXT", "DONE"])
                self.http.set_failure = failure(safe=safe)
                with self.assertRaises(TimeoutError) as caught:
                    await self.expire_during_write(lambda: self.agent.run(self.request, self.scoped))
                summary = json.loads(caught.exception.__cause__.subagent_result.summary)
                self.assertEqual(summary["interrupted_action"]["delivery"],
                                 "not_delivered" if safe else "delivery_uncertain")
                self.assertEqual(summary["unresolved_delivery"], not safe)

    async def test_slow_receives_timeout_evidence_and_new_contract_cannot_repeat_write(self):
        original = jev.JevSubagent.run
        calls = 0

        async def run(agent, request, runtime):
            nonlocal calls
            calls += 1

            def call():
                return original(agent, request, runtime)

            return await self.expire_during_write(call) if calls == 1 else await call()

        first = self.request.contract.model_dump()
        second = {**first, "subgoal_id": "new-subgoal-after-timeout"}
        self.http.choices = iter(["1", "TYPE_TEXT", "1", "TYPE_TEXT"])
        with patch.object(jev.JevSubagent, "run", run):
            harness, _ = await self.run_pipeline(decisions=[
                {"action": "CONTINUE", "contract": first},
                {"action": "CONTINUE", "contract": second},
                {"action": "SUCCESS"},
            ])
        previous = harness.model.contexts[1]["previous_result"]
        self.assertTrue(json.loads(previous["summary"])["unresolved_delivery"])
        self.assertEqual(harness.model.contexts[2]["previous_result"]["exit_reason"],
                         "unsafe_write_replay_blocked")
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)

    async def test_cancelled_write_survives_disk_resume_and_blocks_new_subgoal(self):
        await self.cancel_and_resume_write()

    async def test_cancelled_write_remains_blocked_after_an_intervening_cycle(self):
        await self.cancel_and_resume_write(intervene=True)

    async def test_cancelled_explicit_non_delivery_remains_retryable_after_resume(self):
        await self.cancel_and_resume_write(safe=True)

    async def cancel_and_resume_write(self, *, intervene=False, safe=False):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        harness.config.desktop.backend = "osworld"
        harness.config.desktop.osworld.vm_ip = "current-vm"
        harness.config.orchestration.gui_jev.enabled = True
        first = self.request.contract.model_copy(deep=True)
        first.agent_input["text"] = ["cancelled write"]
        self.http.write_ready, self.http.write_release = asyncio.Event(), asyncio.Event()
        self.http.set_failure = failure(safe=True) if safe else None
        task = asyncio.create_task(harness.run_script([
            {"action": "CONTINUE", "contract": first.model_dump()},
        ]))
        try:
            await asyncio.wait_for(self.http.write_ready.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.http.write_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            self.http.write_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cancelled = list(read_records(harness.store.query_dir() / "orchestration.jsonl"))[-1]["state"]
        before_resume = harness.rounds.state
        # The session checkpoint predates the interrupted dispatch. Recovery must read the journal.
        self.assertIsNone(harness.sessions.session.context["orchestration"])
        self.http.write_ready = self.http.write_release = None
        self.http.set_failure = None
        self.http.choices = iter(["1", "TYPE_TEXT", "DONE"])
        second = first.model_copy(deep=True)
        second.subgoal_id = "new-subgoal-after-cancellation"
        actions = []
        if intervene:
            harness.factory.subagents.register(fixtures.FakeSubagent())
            actions.append({"action": "CONTINUE", "contract": fixtures.contract(
                subgoal_id="intervening-cycle").model_dump()})
        actions.extend([{"action": "CONTINUE", "contract": second.model_dump()}, {"action": "SUCCESS"}])
        resumed = await harness.run_script(actions)
        self.assertEqual(resumed.status.value, "success")
        self.assertIsNot(harness.rounds.state, before_resume)
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 2 if safe else 1)
        previous = harness.model.contexts[0]["previous_result"]
        self.assertEqual(previous["exit_reason"], "CancelledError")
        self.assertEqual(previous["attempt_id"], cancelled["pending"]["attempt_id"])
        self.assertEqual(cancelled["previous_result"]["summary"], previous["summary"])
        self.assertEqual(previous["usage"], cancelled["pending_usage"])
        self.assertNotIn("cancelled write", previous["summary"])
        self.assertEqual(json.loads(previous["summary"])["unresolved_delivery"], not safe)
        self.assertEqual(harness.model.contexts[-1]["previous_result"]["exit_reason"],
                         "done" if safe else "unsafe_write_replay_blocked")

    async def test_cursor_setup_budget_failure_can_disable_overlay(self):
        self.agent = self.make_agent(options={"cursor": True})
        self.request.contract.budget.max_tool_calls = 1
        with self.assertRaisesRegex(RuntimeError, "subgoal_tool_calls_limit"):
            await self.agent.run(self.request, self.scoped)
        self.assertEqual(self.http.commands[-1], ["cursor-overlay", "disable"])
        self.assertEqual(self.scoped.usage.cleanup_tool_calls, 1)

    async def test_cleanup_lane_rejects_regular_actions(self):
        with self.assertRaises(ValueError):
            await self.scoped.cleanup("agent_desktop", {"argv": ["click", "@s1:e1"]})
        self.assertEqual(self.http.commands, [])

    async def test_wall_deadline_does_not_prevent_cursor_cleanup(self):
        self.agent = self.make_agent(options={"cursor": True})
        self.http.model_ready, self.http.model_release = asyncio.Event(), asyncio.Event()
        self.scoped.deadline = time.monotonic() + 1
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.agent.run(self.request, self.scoped), 3)
        self.assertEqual(self.http.commands[-1], ["cursor-overlay", "disable"])
        self.assertEqual(self.scoped.usage.cleanup_tool_calls, 1)

    async def test_cleanup_failure_is_recorded_and_not_reported_as_achieved(self):
        self.agent = self.make_agent(options={"cursor": True})
        self.http.cleanup_failure = True
        with self.assertRaisesRegex(RuntimeError, "cursor cleanup"):
            await self.agent.run(self.request, self.scoped)
        records = [json.loads(line) for line in self.journal.path.read_text().splitlines()]
        self.assertTrue(any(r["event"] == "cleanup_finished" and not r["ok"] for r in records))

    async def test_external_model_budget_and_retry_attempts_are_counted(self):
        self.http.statuses = [503]
        self.request.contract.budget.max_model_calls = 1
        with self.assertRaisesRegex(RuntimeError, "subgoal_model_calls_limit"):
            await self.agent.run(self.request, self.scoped)
        self.assertEqual(len(self.http.decisions), 1)
        self.assertEqual(self.scoped.usage.model_calls, 1)

    async def run_pipeline(self, *, max_model_calls=100, decisions=None, selector=None):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        harness.config.desktop.backend = "osworld"
        harness.config.desktop.osworld.vm_ip = "current-vm"
        harness.config.orchestration.gui_jev.enabled = True
        if selector is not None:
            harness.config.orchestration.gui_jev.decision = GuiJevDecisionConfig.model_validate(selector)
        harness.config.orchestration.max_model_calls = max_model_calls
        original = fixtures.ScriptedModel.run_step

        async def with_writer(model, **kwargs):
            response = await original(model, **kwargs)
            if response.response_text == "local model response":
                response.response_text = json.dumps({"text": "full pipeline"})
                response.trace_state.assistant_text = response.response_text
            return response

        with patch.object(fixtures.ScriptedModel, "run_step", with_writer):
            result = await harness.run_script(
                decisions or [
                    {"action": "CONTINUE", "contract": self.request.contract.model_dump()},
                    {"action": "SUCCESS"},
                ]
            )
        return harness, result

    async def test_chat_backend_flows_through_factory_and_parent_verification(self):
        with patch.dict(os.environ):
            os.environ.pop("TYPESAFE_API_KEY", None)
            harness, result = await self.run_pipeline(selector={
                "backend": "chat_completions", "model": "k3.1-pro", "base_url": "https://chat.example/v1",
            })
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(self.http.value, "full pipeline")
        self.assertEqual(len(self.http.chat_requests), 3)
        self.assertEqual(harness.rounds.state.orchestration.usage.model_calls, 6)
        previous = harness.model.contexts[-1]["previous_result"]
        self.assertEqual(previous["outcome"], "achieved")
        self.assertNotEqual(
            harness.model.contexts[0]["observation"], harness.model.contexts[-1]["observation"],
        )

    async def test_full_pipeline_registers_plugin_then_verifies_fresh_observation(self):
        harness, result = await self.run_pipeline()
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(len(harness.factory.subagents.descriptors()), 1)
        self.assertEqual(harness.model.contexts[-1]["previous_result"]["outcome"], "achieved")
        self.assertNotEqual(
            harness.model.contexts[0]["observation"], harness.model.contexts[1]["observation"]
        )
        self.assertEqual(harness.rounds.state.orchestration.usage.model_calls, 6)
        trace = harness.store.llm_trace_log_path.read_text()
        self.assertIn("subagents/gui_jev/", trace)
        self.assertIn("full pipeline", trace)
        before = harness.model.contexts[0]["observation"]["observation_id"]
        after = harness.model.contexts[1]["previous_result"]["last_observation"]["observation_id"]
        verification = harness.model.contexts[1]["observation"]["observation_id"]
        self.assertEqual(len({before, after, verification}), 3)

    async def test_plugin_cannot_consume_the_reserved_verification_model_call(self):
        harness, result = await self.run_pipeline(max_model_calls=5)
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(harness.rounds.state.orchestration.usage.model_calls, 5)
        self.assertEqual(len(self.http.decisions), 2)
        self.assertEqual(self.http.value, "full pipeline")
        self.assertEqual(harness.model.contexts[-1]["previous_result"]["outcome"], "failed")
        self.assertEqual(harness.model.contexts[-1]["previous_result"]["last_observation"], {})
        self.assertNotEqual(
            harness.model.contexts[0]["observation"], harness.model.contexts[1]["observation"]
        )

    async def test_application_selection_preserves_global_verification_reserve(self):
        self.request.contract.agent_input = {}
        harness, result = await self.run_pipeline(max_model_calls=6)
        self.assertEqual(result.terminal_decision.action.value, "finish")
        self.assertEqual(harness.rounds.state.orchestration.usage.model_calls, 6)
        self.assertEqual(len(self.http.decisions), 3)
        self.assertEqual(harness.model.contexts[-1]["previous_result"]["outcome"], "failed")

    async def test_bad_options_and_non_osworld_backend_are_rejected(self):
        with self.assertRaises(ValueError):
            self.make_agent(options={"text_model": "other"})
        with self.assertRaises(ValueError):
            jev.JevSubagent(GuiJevConfig(), DesktopPolicyConfig())

    async def test_enabled_missing_optional_package_has_clear_configuration_error(self):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        harness.config.orchestration.gui_jev.enabled = True
        with patch.dict(sys.modules):
            sys.modules.pop(jev.__name__, None)
            sys.modules["jev_desktop"] = None
            with self.assertRaisesRegex(ValueError, "optional jev-desktop package"):
                harness.make_runtime([])
        self.assertEqual(harness.observer.calls, 0)
