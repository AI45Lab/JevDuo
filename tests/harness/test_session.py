"""JevDuoSession：planner_led 决策状态机（组件全 mock，场景级）。"""

from __future__ import annotations

import json

import pytest

from jevduo.config import SessionConfig
from jevduo.protocol.models import CliAction, FailAction, FinishAction, GuiAction, WaitAction
from jevduo.runtime.session import JevDuoSession

from .conftest import (
    FixedSelector,
    NullSelector,
    ScriptedPlanner,
    ScriptedVerifier,
    cli_receipt,
    gui_receipt,
    make_components,
    make_observation,
    valid_verifier_response,
)


def _gui_plan(**overrides):
    base = {
        "done": False,
        "task_status": "running",
        "current_goal": "点击确定",
        "expected_observation": "对话框发生变化",
        "action_proposals": [
            {"operation": "click", "target_id": "a11y:t0:0", "target_name": "确定",
             "target_role": "push_button"}
        ],
    }
    base.update(overrides)
    return base


def _done_plan(status="completed"):
    return {"done": True, "task_status": status, "current_goal": "任务完成",
            "expected_observation": ""}


def _session(plans, responses, selector=None):
    planner = ScriptedPlanner(list(plans))
    verifier = ScriptedVerifier(list(responses))
    return (
        JevDuoSession(make_components(planner, verifier, selector or FixedSelector())),
        planner,
        verifier,
    )


def test_gui_happy_path_to_finish(tmp_path):
    session, planner, verifier = _session(
        [_gui_plan(), _done_plan()],
        [
            valid_verifier_response(),
            valid_verifier_response(task_status="completed", requirement_checks=[]),
        ],
    )
    session.begin("整理表格", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, GuiAction) and action.action_type == "click"
    assert action.parameters == {"x": 140, "y": 212}  # 确定: (100,200)+(80,24) 中心
    assert action.target_id == "a11y:t0:0"
    terminal = session.act(make_observation(tmp_path, 1, receipt=gui_receipt()))
    assert isinstance(terminal, FinishAction) and terminal.outputs == ()
    # 验证调用带 BEFORE/CURRENT，终态验证带 terminal_claim
    assert verifier.calls[0]["previous"] is not None
    assert verifier.calls[1]["terminal_claim"]["done"] is True
    assert session.recent[0]["operation"] == "click"
    assert planner.calls[1]["recent_tail"][0]["operation"] == "click"
    routes = [event["route"] for event in session.events]
    assert routes == ["planner", "gui", "gui_verified", "planner", "terminal"]


def test_cli_route_with_receipt_verification(tmp_path):
    session, _, verifier = _session(
        [_gui_plan(route="cli", command="ls -la", action_proposals=None), _done_plan()],
        [
            valid_verifier_response(),
            valid_verifier_response(task_status="completed", requirement_checks=[]),
        ],
    )
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, CliAction)
    assert action.command == "ls -la" and action.want_observation == "screenshot"
    assert action.timeout_s == 90
    terminal = session.act(
        make_observation(tmp_path, 1, receipt=cli_receipt(stdout="a.txt"))
    )
    assert isinstance(terminal, FinishAction)
    assert verifier.calls[0]["execution_result"]["stdout"] == "a.txt"
    assert session.recent[0]["operation"] == "cli"
    assert session.verified_focus is None


def test_cli_failure_forces_not_achieved(tmp_path):
    session, _, _verifier = _session(
        [_gui_plan(route="cli", command="oops", action_proposals=None), _done_plan()],
        [valid_verifier_response(), valid_verifier_response(task_status="completed")],
    )
    session.begin("整理", [])
    session.act(make_observation(tmp_path, 0))
    session.act(make_observation(tmp_path, 1, receipt=cli_receipt(returncode=127, stderr="not found")))
    entry = session.recent[0]
    assert entry["outcome"] == "not_achieved"
    assert entry["verification"]["task_status"] == "incomplete"


def test_no_action_limit_triggers_fail(tmp_path):
    session, planner, _ = _session(
        [_gui_plan(), _gui_plan(), _gui_plan()],
        [],
        selector=NullSelector(),
    )
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, FailAction) and action.reason == "no_action_limit"
    assert len(session.recent) == 3
    assert all(item["outcome"] == "no_action" for item in session.recent)
    assert len(planner.calls) == 3


def test_done_probe_and_finish_with_outputs(tmp_path):
    requirements = [{"id": "R1", "requirement": "保存报表", "output_path": "/home/user/报表.xlsx"}]
    session, _, verifier = _session(
        [_done_plan()],
        [valid_verifier_response(task_status="completed", requirement_checks=[
            {"id": "R1", "status": "met", "evidence": "文件存在"}])],
    )
    session.begin("整理", requirements)
    probe = session.act(make_observation(tmp_path, 0))
    assert isinstance(probe, CliAction)
    assert "报表.xlsx" in probe.command and probe.want_observation == "none"
    assert probe.timeout_s == 30
    exists = json.dumps({"/home/user/报表.xlsx": True})
    terminal = session.act(
        make_observation(tmp_path, 1, receipt=cli_receipt(stdout=exists))
    )
    assert isinstance(terminal, FinishAction)
    assert terminal.outputs == ("/home/user/报表.xlsx",)
    assert verifier.calls[0]["output_files"] == {"/home/user/报表.xlsx": True}
    assert verifier.calls[0]["terminal_claim"]["task_status"] == "completed"


def test_done_probe_missing_file_rejects_and_replans(tmp_path):
    requirements = [{"id": "R1", "requirement": "保存报表", "output_path": "/home/user/报表.xlsx"}]
    replan = _gui_plan(
        action_proposals=[
            # 重规划发生在 step 1，引用当帧 target（真实 planner 只会看到当帧 visible_controls）
            {"operation": "click", "target_id": "a11y:t1:0", "target_name": "确定",
             "target_role": "push_button"}
        ]
    )
    session, planner, _ = _session(
        [_done_plan(), replan],
        [
            valid_verifier_response(task_status="incomplete"),
            valid_verifier_response(),
        ],
    )
    session.begin("整理", requirements)
    probe = session.act(make_observation(tmp_path, 0))
    assert isinstance(probe, CliAction)
    missing = json.dumps({"/home/user/报表.xlsx": False})
    action = session.act(make_observation(tmp_path, 1, receipt=cli_receipt(stdout=missing)))
    assert isinstance(action, GuiAction)  # 完成被拒 → 重新规划出 GUI 动作
    assert session.done_probe is None
    assert session.recent[0]["operation"] == "completion_rejected"
    assert len(planner.calls) == 2


def test_budget_exhaustion_short_circuits(tmp_path):
    session, planner, _ = _session([_gui_plan()], [])
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0, steps=0))
    assert isinstance(action, FailAction) and action.reason == "max_steps"
    assert planner.calls == []
    wall = session.act(make_observation(tmp_path, 1, wall=0.0))
    assert isinstance(wall, FailAction) and wall.reason == "wall_time_limit"


def test_completion_rejected_replans_within_same_observation(tmp_path):
    session, planner, _ = _session(
        [_done_plan(), _gui_plan()],
        [valid_verifier_response(task_status="incomplete")],
    )
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, GuiAction)
    assert len(planner.calls) == 2
    assert session.recent[0]["operation"] == "completion_rejected"


def test_verified_focus_set_after_ctrl_l(tmp_path):
    plan = _gui_plan(
        current_goal="聚焦地址栏",
        action_proposals=[{"operation": "keypress", "key_sequence": [["ctrl", "l"]]}],
    )
    follow_up = _gui_plan(
        action_proposals=[
            {"operation": "click", "target_id": "a11y:t1:0", "target_name": "确定",
             "target_role": "push_button"}
        ]
    )
    session, planner, _ = _session(
        [plan, follow_up, _done_plan()],
        [
            valid_verifier_response(path_input_focused=True),
            valid_verifier_response(),
            valid_verifier_response(task_status="completed"),
        ],
    )
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, GuiAction) and action.action_type == "hotkey"
    assert action.parameters == {"sequence": [["ctrl", "l"]]}
    session.act(make_observation(tmp_path, 1, receipt=gui_receipt()))
    assert session.verified_focus is not None
    assert session.verified_focus["role"] == "entry"
    assert planner.calls[1]["verified_focus"] is not None


def test_missing_receipt_raises(tmp_path):
    session, _, _ = _session(
        [_gui_plan(route="cli", command="ls", action_proposals=None)],
        [valid_verifier_response()],
    )
    session.begin("整理", [])
    session.act(make_observation(tmp_path, 0))
    with pytest.raises(RuntimeError, match="执行回执"):
        session.act(make_observation(tmp_path, 1, receipt=None))


def test_wait_candidate_emits_wait_action(tmp_path):
    plan = _gui_plan(action_proposals=[{"operation": "wait"}])
    session, _, _ = _session([plan], [])
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, WaitAction)


def test_unavailable_visual_fallback_is_not_offered_to_selector(tmp_path):
    selector = FixedSelector()
    session, _, _ = _session([_gui_plan()], [], selector=selector)
    session.begin("整理", [])
    action = session.act(make_observation(tmp_path, 0))
    assert isinstance(action, GuiAction)
    assert "visual_fallback" not in selector.calls[0]["operations"]


def test_cli_timeout_clamped_to_protocol_limit():
    with pytest.raises(ValueError, match="cli_timeout_seconds"):
        SessionConfig(cli_timeout_seconds=200)
    assert SessionConfig(cli_timeout_seconds=120).cli_timeout_seconds == 120


def test_truncated_reasoning_raises_actionable_error(tmp_path, monkeypatch):
    """finish_reason=length（推理吃满 max_tokens、正文为空）必须报可定位的错误。"""
    import jevduo.models.slow as providers_module
    from jevduo.config import SessionConfig
    from jevduo.models.slow import SlowModelClient

    monkeypatch.setattr(providers_module.time, "sleep", lambda seconds: None)

    class TruncatingOpener:
        def __call__(self, request, timeout=None):
            return _FakeRawResponse()

    class _FakeRawResponse:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 4099},
            }).encode()

    client = SlowModelClient({"base_url": "http://x"}, SessionConfig(),
                        tmp_path / "audit.jsonl")
    import urllib.request
    opener_holder = TruncatingOpener()
    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = opener_holder
    try:
        with pytest.raises(ValueError, match="raise slow_max_tokens"):
            client.json_request([{"role": "user", "content": "hi"}], lambda v: True,
                                role="plan_step")
    finally:
        urllib.request.urlopen = original_urlopen


def test_kimi_payload_controls_reasoning_and_tokens(tmp_path):
    """payload 携带 reasoning_effort / enable_thinking 与翻倍后的 max_tokens。"""
    import base64
    import urllib.request

    from jevduo.config import SessionConfig
    from jevduo.models.slow import SlowModelClient

    captured = {}

    class _Resp:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "{\"done\": true}"},
                             "finish_reason": "stop"}],
            }).encode()

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode())
        return _Resp()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        config = SessionConfig(slow_reasoning_effort="low")
        client = SlowModelClient({"base_url": "http://x"}, config, tmp_path / "a.jsonl")
        client.json_request([{"role": "user", "content": "hi"}], lambda v: True, role="r")
        assert captured["payload"]["max_tokens"] == 16384
        assert captured["payload"]["reasoning_effort"] == "low"
        client_off = SlowModelClient({"base_url": "http://x"}, SessionConfig(slow_reasoning_effort="off"),
                                tmp_path / "a.jsonl")
        client_off.json_request([{"role": "user", "content": "hi"}], lambda v: True, role="r")
        assert captured["payload"]["enable_thinking"] is False
        assert "reasoning_effort" not in captured["payload"]
    finally:
        urllib.request.urlopen = original
    del base64


def test_begin_without_requirements_extracts_via_component():
    """requirements=None 走组件抽取（旧 runtime 行为）；显式 [] 不调用。"""
    calls = []

    def requirements_fn(instruction):
        calls.append(instruction)
        return [{"id": "R1", "requirement": "保存文件", "output_path": None}]

    planner = ScriptedPlanner([
        {"done": True, "task_status": "completed", "current_goal": "g", "expected_observation": ""},
    ])
    verifier = ScriptedVerifier([valid_verifier_response(task_status="completed")])
    components = make_components(planner, verifier, FixedSelector())
    components.requirements = requirements_fn
    session = JevDuoSession(components)
    session.begin("整理并保存", None)
    assert session.requirements == [{"id": "R1", "requirement": "保存文件", "output_path": None}]
    assert calls == ["整理并保存"]
    session.begin("整理并保存", [])
    assert session.requirements == [] and len(calls) == 1  # 显式 [] 不再抽取


def test_kimi_retries_escalate_reasoning_effort_on_empty_content(tmp_path):
    """空正文（思考通道吞掉全部输出）→ 重试自动升档 low→medium，不再原样重发。"""
    import urllib.request

    from jevduo.config import SessionConfig
    from jevduo.models.slow import SlowModelClient

    payloads = []
    responses = iter([
        # 第 1 次：stop 但 content 空、reasoning 有值
        {"choices": [{"message": {"content": "", "reasoning_content": "thinking..."},
                      "finish_reason": "stop"}]},
        # 第 2 次（medium）：正常 JSON
        {"choices": [{"message": {"content": '{"done": true}'},
                      "finish_reason": "stop"}]},
    ])

    class _Resp:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(next(responses)).encode()

    import jevduo.models.slow as providers_mod
    providers_mod.time.sleep = lambda seconds: None

    def fake_urlopen(request, timeout=None):
        payloads.append(json.loads(request.data.decode()))
        return _Resp()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        client = SlowModelClient({"base_url": "http://x"}, SessionConfig(slow_reasoning_effort="low"),
                                tmp_path / "a.jsonl")
        value = client.json_request([{"role": "user", "content": "hi"}],
                                    lambda v: v == {"done": True}, role="plan_step")
        assert value == {"done": True}
        assert payloads[0]["reasoning_effort"] == "low"
        assert payloads[1]["reasoning_effort"] == "medium"
    finally:
        urllib.request.urlopen = original


def test_extract_json_object_salvages_prose_wrapped_answers():
    from jevduo.models.slow import extract_json_object

    assert extract_json_object('```json\n{"done": true}\n```') == {"done": True}
    assert extract_json_object('Sure! Here is my plan:\n{"done": false, "route": "cli"}') == {
        "done": False, "route": "cli"}
    assert extract_json_object("The fixes were applied and saved, but I want to verify.") is None
    assert extract_json_object('{"a": {"b": 1}} trailing') == {"a": {"b": 1}}


def test_kimi_accepts_prose_wrapped_json(tmp_path):
    """正文 = 围栏外有叙述 + 合法 JSON：不再 JSONDecodeError，直接采纳。"""
    import urllib.request

    from jevduo.config import SessionConfig
    from jevduo.models.slow import SlowModelClient

    class _Resp:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": 'Plan follows:\n```json\n{"done": true}\n```'},
                              "finish_reason": "stop"}],
            }).encode()

    original = urllib.request.urlopen
    urllib.request.urlopen = lambda request, timeout=None: _Resp()
    try:
        client = SlowModelClient({"base_url": "http://x"}, SessionConfig(), tmp_path / "a.jsonl")
        assert client.json_request([{"role": "user", "content": "hi"}],
                                   lambda v: v == {"done": True}, role="r") == {"done": True}
    finally:
        urllib.request.urlopen = original
