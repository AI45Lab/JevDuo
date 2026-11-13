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

pytest.importorskip("jev_desktop", reason="optional gui_jev integration requires agent-desktop/jev")

from northcua.agentkit.orchestration.contracts import OrchestrationState, SubagentRequest
from northcua.agentkit.orchestration.runtime import Journal, RunBudget, ScopedRuntime
from northcua.agents.desktop.subagents import jev
from northcua.core.config import DesktopPolicyConfig
from northcua.core.config_base import GuiJevConfig, OrchestrationConfig

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
        self.set_failure, self.bad_snapshot = None, False
        self.model_ready = self.model_release = self.write_ready = self.write_release = None
        self.statuses = []
        self.cleanup_failure = False
        self.no_window = False
        self.snapshot_tree = None

    async def __call__(self, request):
        body = json.loads(request.content)
        self.urls.append(str(request.url))
        if request.url.host == "jev.example":
            self.decisions.append(body)
            if self.statuses:
                return httpx.Response(self.statuses.pop(0), json={})
            question = next(iter(body["questions"]))
            if self.model_ready is not None and len(self.decisions) >= self.model_pause_after:
                self.model_ready.set()
                await self.model_release.wait()
            choice = next(self.application_choices if question == "application" else self.choices)
            answer = {"type": "choice", "choice": choice, "confidence": 0, "probabilities": {choice: 1}}
            return httpx.Response(200, json={"answers": {question: answer}})
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
            result = success({"text": self.clipboard})
        elif command == "clipboard-set":
            if self.cleanup_failure and args[1] == "original clipboard":
                result = failure()
            else:
                self.clipboard = args[1]
        elif command == "press":
            self.value = self.clipboard
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
        self.assertEqual(snapshots, [(20000, 30), (40000, 45)])
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
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertFalse(any(c[0] in ("launch", "focus-window", "snapshot") for c in self.http.commands))

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
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.http.commands[-1][0], "launch")
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
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.http.commands[-1], ["cursor-overlay", "disable"])
        commands = list(self.http.commands)
        self.http.model_release.set()
        await asyncio.sleep(0)
        self.assertEqual(self.http.commands, commands)

    async def test_cancel_waits_for_submitted_write_before_teardown(self):
        self.http.write_ready, self.http.write_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.agent.run(self.request, self.scoped))
        await asyncio.wait_for(self.http.write_ready.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.http.write_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertEqual([c[0] for c in self.http.commands].count("set-value"), 1)
        self.assertEqual(self.http.commands[-1][0], "set-value")

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

    async def run_pipeline(self, *, max_model_calls=100):
        harness = fixtures.OrchestrationTest()
        await harness.asyncSetUp()
        self.addCleanup(harness.doCleanups)
        harness.config.desktop.backend = "osworld"
        harness.config.desktop.osworld.vm_ip = "current-vm"
        harness.config.orchestration.gui_jev.enabled = True
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
                [
                    {"action": "CONTINUE", "contract": self.request.contract.model_dump()},
                    {"action": "SUCCESS"},
                ]
            )
        return harness, result

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
