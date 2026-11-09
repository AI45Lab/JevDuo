"""Explicit mock integration: real tool contracts, no VM, hosted model or desktop input."""

import asyncio
import io
import json
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from PIL import Image

import recorder
from jev_gui.choice import LocalChoicePolicy
from jev_gui.desktop import ActionBudget, ActionLimit, DesktopPort
from jev_gui.executor import LocalFrame
from jev_gui.semantics import _native_input_owner
from jev_gui.tools import Card, Terminal, install


def config():
    return {"backend": "jev", "fast_model": {"endpoint": "http://mock.invalid"}}


def test_registry_exposes_only_jev_gui_independent_cli_and_readonly_tools(tmp_path):
    from northcua.agents.desktop.agentkit import desktop_agent_spec
    from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor
    from northcua.runtime.mcp.registry import NorthCUAMCPRegistry

    owner = install(config(), tmp_path, 10, time.time() + 60)
    try:
        contracts = NorthCUAMCPRegistry.tool_contracts("DesktopNativeActionExecutor")
        assert {c.tool_id for c in contracts} == {"note", "screenshot", "terminal", "gui_goal"}
        exposed = {tool.contract.tool_id for tool in desktop_agent_spec().tools if tool.is_model_visible}
        assert exposed == {"note", "screenshot", "terminal", "gui_goal"}
        executor = NorthCUADeviceCommandExecutor(allowed_server_names={"DesktopNativeActionExecutor"})
        result = asyncio.run(executor._execute_command(
            "mock-click", "click", {"x": 1, "y": 1}, server_name="DesktopNativeActionExecutor"))
        assert result.status.value == "failure"
        assert owner.budget.actions == 0
    finally:
        owner.close()
    assert "click" in {c.tool_id for c in NorthCUAMCPRegistry.tool_contracts("DesktopNativeActionExecutor")}


def test_cli_is_independent_of_selector_and_common_gui_automation_is_rejected(tmp_path, monkeypatch):
    import jev_gui.receipts as receipts

    owner = install(config(), tmp_path, 3, time.time() + 60)
    seen = []
    monkeypatch.setattr(receipts, "run_terminal", lambda backend, params, remaining: (
        seen.append(params) or {"action_status": "acknowledged", "stdout": "ok", "seconds": 0.1}))
    owner.port = SimpleNamespace(backend=object())
    try:
        result = owner.terminal(Terminal(command="printf ok"))
        assert result["stdout"] == "ok" and len(seen) == 1
        assert owner.statistics()["cli_actions"] == 1 and owner.statistics()["jev_calls"] == 0
        with pytest.raises(ValueError, match="GUI automation"):
            owner.terminal(Terminal(command="python -c 'import pyautogui; pyautogui.click(1,1)'"))
        assert owner.budget.actions == 1 and len(seen) == 1
    finally:
        owner.close()


def test_gui_card_selects_each_primitive_and_shares_cli_action_budget(tmp_path):
    owner = install(config(), tmp_path, 2, time.time() + 60)
    sent = []
    backend = SimpleNamespace(keypress=lambda **params: sent.append(params))
    port = DesktopPort(backend, tmp_path, owner.emit, owner.budget)
    png = io.BytesIO()
    Image.new("RGB", (100, 100)).save(png, "PNG")

    async def observe(reactive=False):
        port.frame = LocalFrame(png.getvalue(), "explicit-mock.png", time.monotonic(), 100, 100, [], "mock")
        return port.frame

    def choose(**kwargs):
        return next(c for c in kwargs["candidates"] if c.id == "specified_keys"), {
            "latency_ms": 2, "used_image": True}

    port.observe = observe
    owner.port = port
    owner.selector = SimpleNamespace(choose=choose)
    try:
        card = Card(goal="Mock two keys", operations=[
            {"kind": "keys", "intent": "Press Escape", "success_condition": "Menu closed", "keys": ["esc"]},
            {"kind": "keys", "intent": "Press Tab", "success_condition": "Focus moved", "keys": ["tab"]},
        ])
        result = asyncio.run(owner.gui_goal(card))
        assert result["actions"] == 2 and sent == [{"keys": ["esc"]}, {"keys": ["tab"]}]
        assert result["goal_status"] == "needs_review"
        assert owner.statistics()["jev_calls"] == 2
        with pytest.raises(ActionLimit, match="max_steps"):
            owner.terminal(Terminal(command="must not execute"))
        events = [json.loads(line) for line in (tmp_path / "jev_actions.jsonl").read_text().splitlines()]
        assert len([e for e in events if e["route"] == "action"]) == 2
        assert all(e["owner"] == "jev" for e in events if e["route"] == "action")
    finally:
        owner.close()


def test_deadline_stops_before_any_input():
    budget = ActionBudget(10, time.time() - 1)
    with pytest.raises(ActionLimit, match="wall_time_limit"):
        budget.reserve("gui")
    assert budget.actions == 0


def test_budget_boundary_escapes_real_mcp_dispatch_without_sending_another_command(tmp_path):
    from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor

    owner = install(config(), tmp_path, 2, time.time() + 60)
    owner.budget.gui_actions = 2
    try:
        with pytest.raises(ActionLimit, match="max_steps"):
            owner.before_step(3)
        executor = NorthCUADeviceCommandExecutor(allowed_server_names={"DesktopNativeActionExecutor"})
        with pytest.raises(ActionLimit, match="max_steps"):
            asyncio.run(executor._execute_command(
                "mock-budget", "terminal", {"command": "must not execute"},
                server_name="DesktopNativeActionExecutor"))
        assert owner.budget.actions == 2
    finally:
        owner.close()


def test_cards_reject_unknown_fields_bool_budgets_and_incomplete_operations():
    from pydantic import ValidationError

    operation = {"kind": "fill", "intent": "Fill field", "success_condition": "Value matches",
                 "target_id": "observed:1", "text": "Exact"}
    assert Card(goal="Fill", operations=[operation]).operations[0].text == "Exact"
    for changes in ({"max_actions": True}, {"unexpected": 1}):
        with pytest.raises(ValidationError):
            Card.model_validate({"goal": "Fill", "operations": [operation], **changes})
    with pytest.raises(ValidationError):
        Card.model_validate({"goal": "Fill", "operations": [{**operation, "text": None}]})
    json.dumps(Card.model_json_schema())


def test_background_modal_cannot_own_a_foreground_editor():
    root = ET.fromstring('''<desktop foreground_title="Document - Editor">
      <application name="Chrome"><frame name="Background page">
        <dialog name="Restore pages?" visible="true" showing="true"><text focused="true"/></dialog>
      </frame></application>
      <application name="Editor"><frame name="Document - Editor" active="true"/></application>
    </desktop>''')
    assert _native_input_owner(root) is None
    root.set("foreground_title", "Restore pages?")
    assert _native_input_owner(root).get("name") == "Restore pages?"


def test_stale_choice_cannot_execute_and_endpoint_discovery_refreshes(tmp_path):
    endpoint = tmp_path / "service.json"
    endpoint.write_text('{"base_url":"http://first.invalid"}')
    seen = []

    class Opener:
        def open(self, request, **kwargs):
            seen.append(request.full_url)
            return io.BytesIO(b'{"scores":[0.8,0.2],"used_image":true,"stale":true}')

    from jev_gui.policy import local_candidates

    policy = LocalChoicePolicy(str(endpoint), opener=Opener())
    png = io.BytesIO()
    Image.new("RGB", (16, 16)).save(png, "PNG")
    candidates = local_candidates("mock", [], reactive=True)[:2]
    for address in ("http://first.invalid", "http://second.invalid"):
        endpoint.write_text(json.dumps({"base_url": address}))
        winner, _ = policy.choose(goal="mock", screenshot=png.getvalue(), candidates=candidates, recent=[])
        assert winner is None
    assert seen == ["http://first.invalid/rank", "http://second.invalid/rank"]


def test_jev_api_failure_escapes_ordinary_tool_retry_handlers():
    from jev_gui.choice import ChoiceUnavailable
    from jev_gui.policy import local_candidates

    class Opener:
        def open(self, request, **kwargs):
            raise TimeoutError("explicit mock model timeout")

    png = io.BytesIO()
    Image.new("RGB", (16, 16)).save(png, "PNG")
    policy = LocalChoicePolicy(endpoint="http://mock.invalid", opener=Opener())
    with pytest.raises(ChoiceUnavailable, match="TimeoutError"):
        policy.choose(goal="mock", screenshot=png.getvalue(),
                      candidates=local_candidates("mock", [], reactive=True), recent=[])
    assert not issubclass(ChoiceUnavailable, Exception)


def test_metrics_keep_usage_missing_separate_and_count_every_child_action(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    (tmp_path / "llm_calls.jsonl").write_text(
        json.dumps({"llm_trace": {"usage": {"input_tokens": 20, "completion_tokens": 4}}})
        + '\n{"llm_trace":{}}\n')
    events = [
        {"route": "action", "operation": "click"}, {"route": "action", "operation": "terminal"},
        {"route": "gui", "ranking": {"latency_ms": 100}},
        {"route": "slow_model_call", "seconds": 2.5, "failed": False,
         "usage": {"input_tokens": 20, "completion_tokens": 4}},
        {"route": "slow_model_call", "seconds": 0.0, "failed": True, "usage": None},
    ]
    (agent / "jev_actions.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    recorder.write_execution_metrics(tmp_path, agent, {"seconds": 4.0, "logical_steps": 1})
    result = json.loads((tmp_path / "timing.json").read_text())
    assert result["slow_model"]["calls_missing_usage"] == 1
    assert result["slow_model"]["prompt_tokens"] == 20 and result["slow_model"]["seconds"] == 2.5
    assert result["physical_actions"] == 2 and result["logical_steps"] == 1


def test_metrics_count_normalized_cache_once_and_include_empty_response_retries(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    usage = {"input_tokens": 5243, "input_tokens_uncached": 5243,
             "completion_tokens": 625, "cache_read_tokens": 1024, "reasoning_tokens": 371}
    events = [{"route": "slow_model_call", "seconds": 1, "failed": False, "usage": usage} for _ in range(2)]
    (agent / "jev_actions.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    recorder.write_execution_metrics(tmp_path, agent, {})
    model = json.loads((tmp_path / "timing.json").read_text())["slow_model"]
    assert model["calls"] == 2 and model["calls_missing_usage"] == 0
    assert model["prompt_tokens"] == 2 * (5243 + 1024) and model["completion_tokens"] == 1250
    assert model["cached_tokens"] == 2048 and model["reasoning_tokens"] == 742


def test_model_tap_records_usage_before_runtime_rejects_an_empty_response(monkeypatch):
    from nexau.archs.main_sub.execution import llm_caller

    import task_run

    usage = {"input_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 20}
    response = SimpleNamespace(usage=SimpleNamespace(to_dict=lambda: usage))
    monkeypatch.setattr(llm_caller, "call_llm_with_different_client", lambda *args, **kwargs: response)
    observed = []
    task_run._install_tool_pair_repair(False, on_call=lambda *args: observed.append(args))
    assert llm_caller.call_llm_with_different_client(None, None, {}) is response
    assert len(observed) == 1 and observed[0][1:] == (False, usage)


def test_runtime_config_keeps_native_default_and_requires_a_jev_service(tmp_path):
    import yaml

    import settings

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    data = settings.load(root / "config.yaml")
    assert data["harness"]["local_gui"]["backend"] == "native"
    data["harness"]["local_gui"]["backend"] = "jev"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(settings.ConfigError, match="endpoint"):
        settings.load(path)
    data["harness"]["local_gui"]["fast_model"]["endpoint"] = "http://mock.invalid"
    path.write_text(yaml.safe_dump(data))
    assert settings.load(path)["harness"]["local_gui"]["backend"] == "jev"


def test_app_coordinate_contract_matches_jev_schema_without_changing_native_mode(tmp_path):
    from appcfg import app_config

    args = {"vm_ip": "mock-vm", "server_port": 5000, "max_steps": 8, "artifact_root": tmp_path}
    native = app_config(**args, harness={})
    assert native["desktop"]["coordinate_policy"]["input_space"] == "image_pixels"
    plugin = app_config(**args, harness={"local_gui": {"backend": "jev"}})
    assert plugin["desktop"]["coordinate_policy"] == {
        "input_space": "normalized_1000", "clamp_to_bounds": False}


def test_acknowledged_input_is_not_replayed_or_reported_failed_when_next_capture_fails():
    from jev_gui.executor import LocalExecutor

    calls, reads = [], []
    png = io.BytesIO()
    Image.new("RGB", (100, 100)).save(png, "PNG")

    async def observe(reactive):
        reads.append(1)
        if len(reads) > 1:
            raise TimeoutError("explicit mock capture timeout")
        return LocalFrame(png.getvalue(), "before.png", time.monotonic(), 100, 100, [], "mock")

    async def perform(name, params, reactive):
        calls.append(name)
        return {"action_status": "acknowledged"}

    def choose(**kwargs):
        return next(c for c in kwargs["candidates"] if c.id == "specified_keys"), {}

    runner = LocalExecutor(SimpleNamespace(choose=choose), observe, perform, lambda event: None)
    result = asyncio.run(runner.run({"goal": "Press Esc", "success_condition": "Menu closed",
        "operation_kind": "keys", "command_keys": ["esc"], "max_actions": 2}))
    assert calls == ["keypress"] and len(reads) == 2
    assert result["action_status"] == "acknowledged" and result["observation_status"] == "failed"
    assert result["screenshot_policy"] == "none" and not result["screenshot_path"]


def test_linux_clipboard_script_imports_gui_and_does_not_capture_daemon_pipes(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import sys

    from northcua_runtime_shared.desktop_surface.models import TextTargetKind

    copied, pasted = [], []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/xclip" if name == "xclip" else None)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: copied.append((command, kwargs)))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(hotkey=lambda *keys: pasted.append(keys)))
    owner = install(config(), tmp_path, 8, time.time() + 60)
    try:
        backend = owner.bind().backend

        def execute(script, timeout):
            exec(compile(script, "<explicit-mock-clipboard>", "exec"), {"__name__": "__main__"})
            return {"status": "success"}

        monkeypatch.setattr(backend, "_run_python", execute)
        backend.type_text("精确输入", TextTargetKind.EDITABLE)
        backend.type_text("printf ok", TextTargetKind.TERMINAL)
        assert pasted == [("ctrl", "v"), ("ctrl", "shift", "v")]
        assert len(copied) == 2 and copied[0][1]["input"] == "精确输入"
        assert all(kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
                   for _, kwargs in copied)
    finally:
        owner.close()
