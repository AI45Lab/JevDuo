"""selector（JEV 排序）与 compile（候选→协议动作）。"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from jevduo.gui.compile import gui_action
from jevduo.models.jev import JevOmniPolicy
from jevduo.models.ranking import ranking_dict
from jevduo.protocol.models import GuiAction, WaitAction

from .conftest import build_space, build_targets


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeOpener:
    """按顺序返回 health check 与 /rank 响应；记录请求（health 是 URL 串，rank 是 Request）。"""

    def __init__(self, rank_payloads):
        self.rank_payloads = list(rank_payloads)
        self.requests = []

    def open(self, url, timeout=None):
        full_url = getattr(url, "full_url", url)
        self.requests.append(full_url)
        if full_url.endswith("/"):
            return FakeResponse({"status": "ok"})
        return FakeResponse(self.rank_payloads.pop(0))


def _policy(tmp_path: Path, opener) -> JevOmniPolicy:
    del tmp_path
    return JevOmniPolicy("http://jev-fake", timeout_seconds=5, opener=opener)


class ScriptedOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, url, timeout=None):
        full_url = getattr(url, "full_url", url)
        self.requests.append(full_url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


def _single_candidate_space():
    targets = build_targets()
    space = build_space(targets)
    space.target_heads = {"click": space.target_heads["click"][:1]}
    space.controls = {}
    return targets, space


def test_rank_recovers_after_url_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("jevduo.models.jev.time.sleep", lambda seconds: None)
    targets, space = _single_candidate_space()
    opener = ScriptedOpener([
        urllib.error.URLError("timed out"),
        {"status": "ok"},
        {"scores": [0.9], "used_image": True},
    ])
    ranking = _policy(tmp_path, opener).rank(
        stage_goal="t", intent="i", active_window="", targets=targets,
        action_space=space, recent_actions=[],
    )
    assert ranking.winner is not None
    assert len(opener.requests) == 3


def test_rank_refreshes_endpoint_during_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr("jevduo.models.jev.time.sleep", lambda seconds: None)
    targets, space = _single_candidate_space()
    opener = ScriptedOpener([
        urllib.error.URLError("old endpoint unavailable"),
        {"status": "ok"},
        {"scores": [0.8], "used_image": True},
    ])
    endpoints = iter(["http://jev-old", "http://jev-new"])
    policy = JevOmniPolicy(
        "http://jev-old", timeout_seconds=5, opener=opener,
        endpoint_provider=lambda: next(endpoints),
    )
    ranking = policy.rank(
        stage_goal="t", intent="i", active_window="", targets=targets,
        action_space=space, recent_actions=[],
    )
    assert ranking.winner is not None
    assert policy.endpoint == "http://jev-new"
    assert opener.requests == ["http://jev-old/", "http://jev-new/", "http://jev-new/rank"]


def test_rank_retries_transient_http_error(tmp_path, monkeypatch):
    monkeypatch.setattr("jevduo.models.jev.time.sleep", lambda seconds: None)
    targets, space = _single_candidate_space()
    opener = ScriptedOpener([
        urllib.error.HTTPError("http://jev-fake/", 503, "unavailable", {}, None),
        {"status": "ok"},
        {"scores": [0.7], "used_image": True},
    ])
    ranking = _policy(tmp_path, opener).rank(
        stage_goal="t", intent="i", active_window="", targets=targets,
        action_space=space, recent_actions=[],
    )
    assert ranking.winner is not None
    assert len(opener.requests) == 3


def test_rank_does_not_retry_fatal_http_error(tmp_path):
    targets, space = _single_candidate_space()
    opener = ScriptedOpener([
        urllib.error.HTTPError("http://jev-fake/", 401, "unauthorized", {}, None),
    ])
    with pytest.raises(urllib.error.HTTPError):
        _policy(tmp_path, opener).rank(
            stage_goal="t", intent="i", active_window="", targets=targets,
            action_space=space, recent_actions=[],
        )
    assert len(opener.requests) == 1


def test_rank_picks_highest_scored_candidate(tmp_path):
    targets = build_targets()
    space = build_space(targets)
    # 受限空间：2 个 click 候选 → operation 阶段 1 个选项，action 阶段 2 个选项。
    space.target_heads = {"click": space.target_heads["click"][:2]}
    space.controls = {}
    opener = FakeOpener([
        {"scores": [1.0], "used_image": True},    # operation 排序（单选项）
        {"scores": [0.2, 0.8], "used_image": True},  # action 排序
    ])
    ranking = _policy(tmp_path, opener).rank(
        stage_goal="任务", intent="打开", active_window="", targets=targets,
        action_space=space, recent_actions=[], screenshot=Path("/tmp/s.png"),
    )
    assert ranking.winner.operation == "click"
    assert ranking.winner.id == ranking.action_ranked[0]["id"]
    assert ranking.action_ranked[0]["score"] == 0.8
    assert ranking.target_margin == pytest.approx(0.8 - 0.2)
    assert ranking.used_image is True
    assert ranking_dict(ranking)["score_type"] == "fast_model_choice_softmax"


def test_rank_single_candidate_skips_action_request(tmp_path):
    targets = build_targets()
    space = build_space(targets)
    space.target_heads = {"click": space.target_heads["click"][:1]}
    space.controls = {}
    opener = FakeOpener([{"scores": [0.9], "used_image": False}])
    ranking = _policy(tmp_path, opener).rank(
        stage_goal="任务", intent="打开", active_window="", targets=targets,
        action_space=space, recent_actions=[],
    )
    assert ranking.winner is not None
    assert ranking.action_ranked[0]["score"] == 1.0  # 单候选捷径
    assert len([url for url in opener.requests if not url.endswith("/")]) == 1


def test_rank_rejects_score_count_mismatch(tmp_path):
    targets = build_targets()
    space = build_space(targets)
    space.target_heads = {"click": space.target_heads["click"][:2]}
    space.controls = {}
    opener = FakeOpener([
        {"scores": [1.0], "used_image": False},
        {"scores": [0.5], "used_image": False},  # 2 候选只给 1 个分数
    ])
    with pytest.raises(ValueError, match="score count"):
        _policy(tmp_path, opener).rank(
            stage_goal="任务", intent="打开", active_window="", targets=targets,
            action_space=space, recent_actions=[],
        )


# ------------------------------------------------------------------ compile
def _target_by_name(name):
    return next(t for t in build_targets() if t.name == name)


def test_compile_click_uses_target_center():
    from jevduo.gui.candidates import ActionCandidate

    target = _target_by_name("确定")
    winner = ActionCandidate(
        id="click:x", operation="click", target_id=target.id,
        description="点击", metadata={"semantic_key": target.semantic_key},
    )
    action = gui_action(winner, target)
    assert isinstance(action, GuiAction)
    assert action.action_type == "click"
    assert action.parameters == {"x": 100 + 40, "y": 200 + 12}


def test_compile_slider_maps_to_fractional_click():
    from jevduo.gui.candidates import ActionCandidate

    target = _target_by_name("缩放")
    winner = ActionCandidate(
        id="set_slider:x", operation="set_slider", target_id=target.id,
        description="滑块", metadata={"fraction": 0.5},
    )
    action = gui_action(winner, target)
    assert action.action_type == "click"
    assert action.parameters["x"] == 300 + round((140 - 1) * 0.5)


def test_compile_keypress_sequence_and_named_key():
    from jevduo.gui.candidates import ActionCandidate

    seq = ActionCandidate(
        id="KEY_SEQUENCE:ctrl+l", operation="keypress",
        description="d", metadata={"key_sequence": [["ctrl", "l"], ["enter"]]},
    )
    assert gui_action(seq, None).parameters == {"sequence": [["ctrl", "l"], ["enter"]]}
    named = ActionCandidate(
        id="KEYPRESS_ENTER", operation="keypress", description="回车", metadata={}
    )
    assert gui_action(named, None).parameters == {"sequence": [["enter"]]}


def test_compile_scroll_and_type_and_wait():
    from jevduo.gui.candidates import ActionCandidate

    up = ActionCandidate(
        id="SCROLL_UP", operation="scroll", description="上滚",
        metadata={"scroll_x": 300, "scroll_y": 150},
    )
    assert gui_action(up, None).parameters == {"x": 300, "y": 150, "dy": 5}
    down = ActionCandidate(
        id="SCROLL_DOWN:a11y:s:1", operation="scroll", description="下滚",
        metadata={"scroll_x": 1, "scroll_y": 2},
    )
    assert gui_action(down, None).parameters["dy"] == -5
    target = _target_by_name("A1")
    typed = ActionCandidate(
        id="type_text:x", operation="type_text", target_id=target.id, description="输入",
        metadata={"text": "42", "input_mode": "replace"},
    )
    params = gui_action(typed, target).parameters
    assert params["text"] == "42" and params["input_mode"] == "replace"
    assert params["click"] == [610 + 40, 90 + 12] and params["press_f2"] is True
    focused = ActionCandidate(
        id="TYPE_FOCUSED:0", operation="type_text", description="焦点输入",
        metadata={"text": "cmd", "input_mode": "insert", "focused_input": {"role": "terminal"}},
    )
    assert gui_action(focused, None).parameters["click"] is None
    wait = ActionCandidate(id="WAIT", operation="wait", description="等待", metadata={})
    assert isinstance(gui_action(wait, None), WaitAction)


def test_compile_unsupported_returns_none():
    from jevduo.gui.candidates import ActionCandidate

    fallback = ActionCandidate(
        id="VISUAL_FALLBACK", operation="visual_fallback", description="视觉回退", metadata={}
    )
    assert gui_action(fallback, None) is None
    click_without_target = ActionCandidate(
        id="click:missing", operation="click", description="无目标", metadata={}
    )
    assert gui_action(click_without_target, None) is None


def test_rank_stages_screenshot_to_read_view_path(tmp_path, monkeypatch):
    """截图暂存：adpc 侧截图复制到 evobox 写目录，payload 传 JEV 读视角路径。"""
    write_dir = tmp_path / "evobox-write" / "stage"
    read_dir = "/mnt/shared-storage-gpfs2/fake-read/stage"
    shot = tmp_path / "adpc" / "step_00.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")

    captured = {}

    class CaptureOpener:
        def open(self, request, timeout=None):
            url = getattr(request, "full_url", request)
            if str(url).endswith("/"):
                return FakeResponse({"status": "ok"})
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"scores": [1.0], "used_image": True})

    policy = JevOmniPolicy("http://jev-fake", timeout_seconds=5, opener=CaptureOpener(),
                           image_stage=(str(write_dir), read_dir))
    targets = build_targets()
    space = build_space(targets)
    space.target_heads = {"click": space.target_heads["click"][:1]}
    space.controls = {}
    policy.rank(stage_goal="t", intent="i", active_window="", targets=targets,
                action_space=space, recent_actions=[], screenshot=shot)
    staged = list(write_dir.glob("step_00_*.png"))
    assert len(staged) == 1 and staged[0].read_bytes() == b"png"
    assert captured["payload"]["image_path"].startswith(read_dir + "/step_00_")
