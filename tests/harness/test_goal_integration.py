"""Synthetic compatibility tests against main's semantic pipeline and JEV recovery."""

import base64
import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from tandem.config import SessionConfig
from tandem.gui.candidates import ActionCandidate
from tandem.memory.state import TaskMemory
from tandem.models.jev import JevOmniPolicy
from tandem.models.ranking import Ranking
from tandem.models.slow import SlowModelClient
from tandem.protocol.models import BrowserSnapshot
from tandem.runtime.goal_stage import ObservationRequired
from tandem.verification.goal_verifier import verify_goal

from .conftest import build_space, build_targets, gui_receipt, make_observation
from .test_goal_memory import session
from .test_goal_stage import plan, requirements
from .test_selector_compile import ScriptedOpener
from .test_slow_transport import _Response


def test_goal_passes_cdp_enriched_semantics_to_planner_and_selector(tmp_path):
    s = session()
    s.begin("Enter the receipt date", requirements())
    tree = ('<desktop-frame><application name="Google Chrome" active="true">'
            '<entry name="Address and search bar" screencoord="(10,40)" size="(600,30)"/>'
            '<entry editable="true" screencoord="(250,293)" size="(218,18)"/>'
            '</application></desktop-frame>')
    observation = make_observation(tmp_path, 0, tree_xml=tree)
    snapshot = tmp_path / "browser.json"
    snapshot.write_text(json.dumps({"elements": [{
        "index": 1, "role": "entry", "name": "Receipt Date", "description": "dd/mm/yyyy",
        "value": "", "x": 250, "y": 293, "width": 218, "height": 18,
        "label_source": "dom_label", "operations": ["click", "type_text"],
    }]}))
    observation = replace(observation, browser_snapshot=BrowserSnapshot(
        present=True, source="cdp", path=str(snapshot), node_count=1))

    def planner(*args, **kwargs):
        target = next(t for t in args[2] if t.name == "Receipt Date")
        assert target.source == "a11y+dom" and target.scope == "web_content"
        assert target.id.startswith("a11y:") and (target.x, target.y) == (250, 293)
        return {**plan(), "inputs": [{"label": "Receipt Date", "target_hint": "Receipt Date",
                                      "text": "11/12/2025", "input_mode": "replace"}]}

    s.components.planner = planner
    with pytest.raises(ObservationRequired, match="planner_returned"):
        s.act(observation)

    def rank(**kwargs):
        winner = kwargs["action_space"].target_heads["type_text"][0]
        target = next(t for t in kwargs["targets"] if t.id == winner.target_id)
        assert target.name == "Receipt Date" and target.description == "dd/mm/yyyy"
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    action = s.act(observation)
    assert action.action_type == "type" and action.parameters["click"] == [359, 302]


def test_joint_goal_choices_retain_main_endpoint_recovery(monkeypatch):
    monkeypatch.setattr("tandem.models.jev.time.sleep", lambda seconds: None)
    targets = build_targets()
    space = build_space(targets)
    space.target_heads = {"click": space.target_heads["click"][:1]}
    space.controls = {"done": space.controls["done"]}
    click, done = space.candidates()
    opener = ScriptedOpener([
        urllib.error.URLError("old endpoint unavailable"), {"status": "ok"},
        {"scores": [0.9, 0.1], "used_image": True}, {"status": "ok"},
        {"scores": [0.1, 0.9], "used_image": True},
    ])
    endpoints = iter(["http://old", "http://new", "http://new"])
    policy = JevOmniPolicy("http://old", opener=opener, joint_actions=True,
                           endpoint_provider=lambda: next(endpoints))
    ranking = policy.rank(stage_goal="Goal", intent="Intent", active_window="Chrome",
                          targets=targets, action_space=space, recent_actions=[])
    assert ranking.operation_ranked[0]["operation"] == click.operation
    assert ranking.winner is done and policy.endpoint == "http://new"
    assert opener.requests[-2:] == ["http://new/", "http://new/rank"]


def test_goal_image_budget_and_verify_override_do_not_change_step_transport(tmp_path):
    encoded = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(encoded, format="PNG")
    image_url = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode()
    for mode in ("step", "goal"):
        config = SessionConfig(execution_mode=mode, slow_max_tokens=32768,
                               slow_verify_max_tokens=8192, slow_reasoning_effort="max",
                               slow_verify_reasoning_effort="low")
        client = SlowModelClient({"base_url": "https://test.invalid"}, config, tmp_path / "audit.jsonl")
        messages = [{"role": "user", "content": [{"type": "image_url",
                     "image_url": {"url": image_url}}]}]
        with patch("urllib.request.urlopen", return_value=_Response()) as opener:
            client.json_request(messages, bool, role="verify_stage")
        payload = json.loads(opener.call_args.args[0].data)
        sent_url = payload["messages"][0]["content"][0]["image_url"]["url"]
        assert sent_url.startswith("data:image/jpeg" if mode == "goal" else "data:image/png")
        assert payload["max_tokens"] == 8192 and payload["reasoning_effort"] == "low"


def test_goal_memory_and_stage_verifier_are_replaceable():
    s = session()
    factory = Mock(return_value=TaskMemory("Edit", requirements()))
    s.components.memory_factory = factory
    s.components.stage_verifier = Mock()
    s.begin("Edit", requirements())
    factory.assert_called_once_with("Edit", requirements())
    assert s.goal.memory is factory.return_value


def _shell_observation(directory, *, clock="20:00", window="Editor", color="white", step=0):
    directory.mkdir()
    tree = f'''<desktop-frame foreground_title="{window}" foreground_class='["editor"]'>
      <application name="editor"><frame name="{window}" active="true"/></application>
      <application name="gnome-shell">
        <text name="Clock" focusable="true" screencoord="(950,0)" size="(100,24)">{clock}</text>
        <push_button name="Activities" screencoord="(0,0)" size="(80,24)"/>
      </application></desktop-frame>'''
    observation = make_observation(directory, step, tree_xml=tree)
    Image.new("RGB", (64, 64), color).save(observation.screenshot.path)
    return observation


def _visual(point=(20, 40)):
    return ActionCandidate("visual", "click", "Visible application launcher", source="visual_grounder",
                           metadata={"visual_point": list(point)})


def test_clock_change_after_grounding_does_not_starve_jev(tmp_path):
    s = session()
    s.begin("Open the application", requirements())
    s.components.grounder = Mock(return_value=[_visual()])

    def rank(**kwargs):
        assert "Activities" in [t.name for t in kwargs["targets"]]  # Still actionable.
        winner = kwargs["action_space"].target_heads["visual"][0]
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = Mock(side_effect=rank)
    before = _shell_observation(tmp_path / "before")
    with pytest.raises(ObservationRequired, match="planner_returned"):
        s.act(before)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(before)
    refreshed = _shell_observation(tmp_path / "after", clock="20:02")
    action = s.act(refreshed)
    assert action.parameters == {"x": 20, "y": 40}
    s.components.grounder.assert_called_once()
    s.components.selector.rank.assert_called_once()
    assert s.goal.visual and s.goal.visual_frame  # Keep valid targets until the scene actually changes.


@pytest.mark.parametrize("change", ["window", "pixels", "size"])
def test_changed_scene_discards_pending_visual_coordinates(tmp_path, change):
    s = session()
    s.begin("Open the application", requirements())
    s.components.grounder = Mock(side_effect=[[_visual()], [_visual((30, 45))]])
    s.components.selector.rank = Mock(side_effect=lambda **kw: Ranking(
        kw["action_space"].target_heads["visual"][0], [], [], 0, 0, 1, True))
    before = _shell_observation(tmp_path / "before")
    with pytest.raises(ObservationRequired):
        s.act(before)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(before)
    after = _shell_observation(tmp_path / "after", window="Viewer" if change == "window" else "Editor",
                               color="black" if change == "pixels" else "white")
    if change == "size":
        Image.new("RGB", (80, 64), "white").save(after.screenshot.path)
    # A changed scene needs one new grounding, never the old point or a replayed GUI action.
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(after)
    s.components.selector.rank.assert_not_called()
    assert s.act(after).parameters == {"x": 30, "y": 45}
    assert s.components.grounder.call_count == 2


@pytest.mark.parametrize("budget", ["calls", "elapsed"])
def test_exhausted_grounding_keeps_existing_legal_actions_available(tmp_path, monkeypatch, budget):
    s = session()
    s.begin("Open the application", requirements())
    s.components.grounder = Mock(return_value=[_visual()])
    s.components.selector.rank = Mock(side_effect=lambda **kw: Ranking(
        next(c for c in kw["action_space"].candidates() if c.operation == "keypress"),
        [], [], 0, 0, 1, True))
    client = Mock()
    s.components.stage_verifier = lambda *a, **kw: verify_goal(client, *a, **kw)
    now = [100.0]
    monkeypatch.setattr("tandem.runtime.goal_stage.time.monotonic", lambda: now[0])

    def ground(*args):
        if budget == "elapsed":
            now[0] += s.config.goal_recovery_max_seconds
        return [_visual()]

    s.components.grounder.side_effect = ground
    first = _shell_observation(tmp_path / "first")
    with pytest.raises(ObservationRequired):
        s.act(first)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(first)
    if budget == "calls":
        second = _shell_observation(tmp_path / "second", window="Second")
        with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
            s.act(second)
    changed = _shell_observation(tmp_path / "third", window="Third")
    assert s.act(changed).kind == "gui"
    assert s.components.grounder.call_count == (1 if budget == "elapsed" else 2)
    s.components.selector.rank.assert_called_once()
    client.json_request.assert_not_called()
    assert s.goal.memory.active_stage is not None
    assert s.goal.memory.active_blocker["kind"] == "capability_exhausted"
    assert not any(e.get("route") == "stage_verified" for e in s.events)
    assert not any(c.operation == "visual_fallback" for c in
                   s.components.selector.rank.call_args.kwargs["action_space"].candidates())


def test_clock_only_changes_do_not_reset_failed_action_memory(tmp_path):
    s = session()
    s.begin("Open the application", requirements())
    s.components.selector.rank = Mock(side_effect=lambda **kw: Ranking(
        next(c for c in kw["action_space"].candidates() if c.operation == "keypress"),
        [], [], 0, 0, 1, True))
    first = _shell_observation(tmp_path / "first")
    with pytest.raises(ObservationRequired):
        s.act(first)
    s.act(first)
    refreshed = replace(_shell_observation(tmp_path / "after", clock="20:03", step=1),
                        last_receipt=gui_receipt())
    s.act(refreshed)
    assert s.goal.no_effect == 1
    assert len(s.goal.failed_actions) == 1 and not s.goal.history[-1]["relevant_change"]


def test_consumed_visual_cache_expires_when_pixels_change_without_a11y_change(tmp_path):
    s = session()
    s.begin("Edit the current slide", requirements())
    s.components.grounder = Mock(side_effect=[[_visual()], [_visual((30, 45))]])
    s.components.selector.rank = Mock(side_effect=lambda **kw: Ranking(
        kw["action_space"].target_heads["visual"][0], [], [], 0, 0, 1, True))
    before = _shell_observation(tmp_path / "before")
    with pytest.raises(ObservationRequired, match="planner_returned"):
        s.act(before)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(before)
    assert s.act(before).parameters == {"x": 20, "y": 40}
    after = replace(_shell_observation(tmp_path / "after", color="black", step=1),
                    last_receipt=gui_receipt())
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(after)
    assert s.components.grounder.call_count == 2
    assert any(e.get("route") == "recovery_cache_invalidated" for e in s.events)
    assert s.act(after).parameters == {"x": 30, "y": 45}


def test_same_scene_new_stage_does_not_inherit_old_grounding_suppression(tmp_path):
    s = session()
    s.begin("Edit two portions of the same slide", requirements())
    s.components.planner = lambda *a, **kw: {**plan(), "max_actions": 1}
    s.components.grounder = Mock(return_value=[_visual()])
    s.components.selector.rank = Mock(side_effect=lambda **kw: Ranking(
        kw["action_space"].target_heads["visual"][0], [], [], 0, 0, 1, True))
    before = _shell_observation(tmp_path / "before")
    with pytest.raises(ObservationRequired):
        s.act(before)
    with pytest.raises(ObservationRequired):
        s.act(before)
    s.act(before)
    s.goal.recovery_calls = s.config.goal_recovery_max_calls
    s.goal.recovery_seconds = s.config.goal_recovery_max_seconds
    after = replace(_shell_observation(tmp_path / "after", step=1), last_receipt=gui_receipt())
    with pytest.raises(ObservationRequired, match="stage_verification_returned"):
        s.act(after)
    with pytest.raises(ObservationRequired, match="planner_returned"):
        s.act(after)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(after)
    assert s.components.grounder.call_count == 2
    assert s.goal.recovery_calls == 1 and s.goal.recovery_seconds < 1


def test_waiting_outside_grounder_does_not_consume_visual_recovery_time(tmp_path, monkeypatch):
    s = session()
    s.begin("Open the application", requirements())
    now = [100.0]
    monkeypatch.setattr("tandem.runtime.goal_stage.time.monotonic", lambda: now[0])
    s.components.grounder = Mock(return_value=[_visual()])
    before = _shell_observation(tmp_path / "before")
    with pytest.raises(ObservationRequired):
        s.act(before)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(before)
    now[0] += 10000
    after = _shell_observation(tmp_path / "after", window="Another application")
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(after)
    assert s.goal.recovery_calls == 2 and s.goal.recovery_seconds == 0


def test_shell_menu_toggle_is_not_task_progress_or_a_recovery_budget_reset(tmp_path):
    s = session()
    s.begin("Import an image into the editor", requirements())
    s.components.selector.rank = Mock(side_effect=lambda **kw: Ranking(
        next(c for c in kw["action_space"].candidates() if c.operation == "click"),
        [], [], 0, 0, 1, True))
    before = _shell_observation(tmp_path / "before")
    with pytest.raises(ObservationRequired):
        s.act(before)
    s.act(before)
    s.goal.recovery_calls = 1
    after = _shell_observation(tmp_path / "after", step=1)
    path = after.accessibility_tree.path
    tree = Path(path).read_text().replace('</application></desktop-frame>',
        '<menu_item name="Quit" screencoord="(90,30)" size="(120,30)"/>'
        '</application></desktop-frame>')
    Path(path).write_text(tree)
    s.act(replace(after, last_receipt=gui_receipt()))
    assert s.goal.no_effect == 1 and s.goal.recovery_calls == 1
    assert s.goal.history[-1]["observed_change"]
    assert not s.goal.history[-1]["relevant_change"]
