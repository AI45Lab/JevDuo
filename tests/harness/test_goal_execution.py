"""Synthetic regressions derived from real failure patterns; not benchmark acceptance."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from tandem.gui.compile import gui_action
from tandem.gui.goal_candidates import foreground_tree, goal_candidates
from tandem.gui.targets import A11yTargetBuilder
from tandem.memory.context import planning_context
from tandem.models.ranking import Ranking
from tandem.planning.goal_planner import goal_error, plan_goal
from tandem.runtime.goal_stage import (
    ObservationRequired,
    _action_key,
    _cycle_actions,
    _loop_state,
    _target_effect,
)
from tandem.verification.goal_verifier import result_error

from .conftest import A11Y_XML, build_space, build_targets, gui_receipt, make_observation
from .test_goal_memory import fact, session
from .test_goal_stage import plan, requirements, result


def _space(tree, inputs=()):
    tree = foreground_tree(tree)
    targets = A11yTargetBuilder().build(tree, "test", 128, True, True, filter_occluded=True)
    return targets, goal_candidates(build_space(targets), targets, tree, inputs)


def test_receipt_path_detects_a_b_a_b_without_manually_seeding_attempts(tmp_path):
    s = session()
    s.begin("Edit", requirements())
    s.goal.memory.start(plan(), 0)
    chosen = []

    def rank(**kw):
        candidates = kw["action_space"].candidates()
        toggles = [c for c in candidates if c.operation == "click" and "启用" in c.description]
        if len(chosen) == 4:
            assert not toggles
            assert kw["action_space"].controls["return_to_planner"]
            winner = next(c for c in candidates if c.id == "KEYPRESS_ESCAPE")
        else:
            winner = toggles[0]
        chosen.append(winner)
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    for step in range(4):
        tree = A11Y_XML.replace('checked="false"', f'checked="{str(step % 2 == 1).lower()}"')
        s.act(make_observation(tmp_path, step, tree_xml=tree,
                               receipt=gui_receipt() if step else None))
    with pytest.raises(ObservationRequired, match="stage_verification_returned"):
        s.act(make_observation(tmp_path, 4, tree_xml=A11Y_XML, receipt=gui_receipt()))
    assert len(chosen) == 4  # The return cycle ends before offering another ineffective toggle.
    assert any(e.get("end_reason") == "execution_checkpoint_stalled" for e in s.events)
    assert s.goal.memory.execution_constraints
    assert planning_context(s.goal.memory)["execution_constraints"]
    assert s.goal.attempts[0]["state"] == s.goal.attempts[2]["state"]


def test_duplicate_popup_nodes_do_not_remove_their_own_close_button():
    popup = ('<alert name="Restore pages?" showing="true" visible="true" '
             'screencoord="(150,100)" size="(200,200)">'
             '<push_button name="Close" screencoord="(300,110)" size="(25,25)"/>'
             '</alert>')
    tree = ('<application active="true"><link name="PDF" screencoord="(200,200)" size="(50,20)"/>'
            '<link name="Not covered" screencoord="(20,200)" size="(50,20)"/>'
            + popup * 2 + '</application>')
    targets = A11yTargetBuilder().build(tree, "test", filter_occluded=True)
    assert [t.name for t in targets].count("Close") == 2
    assert "PDF" not in [t.name for t in targets]
    assert "Not covered" in [t.name for t in targets]


def test_explicit_popup_menu_excludes_the_covered_media_button():
    tree = ('<application active="true"><push_button name="More" screencoord="(290,290)" size="(20,20)"/>'
            '<menu ispopup="auto" showing="true" screencoord="(200,200)" size="(150,150)">'
            '<menu_item name="Exit picture-in-picture" screencoord="(210,280)" size="(130,40)"/>'
            '</menu></application>')
    ts, space = _space(tree)
    assert "More" not in [t.name for t in ts]
    assert any("Exit picture-in-picture" in c.description for c in space.target_heads["click"])


def test_browser_memory_and_playback_badges_do_not_create_new_loop_states():
    def state(name):
        tree = ('<application active="true"><page_tab name="' + name
                + '" screencoord="(10,10)" size="(200,40)"/></application>')
        return _loop_state(build_targets(tree), tree)

    assert state("Video - Memory usage - 47.1 MB") == state(
        "Video – Audio playing - Memory usage - 108 MB")
    assert state("Different page - Memory usage - 108 MB") != state("Video - Memory usage - 47.1 MB")


def test_four_state_cycle_is_detected_but_continuous_scroll_is_not():
    states = ["closed", "menu", "pip", "pip-menu"]
    attempts = [{"work": "w", "state": state, "key": ("click", "menu", "")}
                for state in states * 2]
    assert _cycle_actions(attempts, "closed") == {("click", "menu", "")}
    assert not _cycle_actions([{"state": "page", "key": ("scroll", "down", "")}] * 8, "page")


def test_overview_excludes_stale_app_and_keeps_unnamed_search_then_typing():
    tree = ('<desktop-frame foreground_title="old video" foreground_class=\'["totem"]\'>'
            '<application name="totem" active="true"><button name="Play" '
            'screencoord="(100,400)" size="(30,30)"/></application>'
            '<application name="gnome-shell"><panel name="Overview">'
            '<text name="" editable="true" showing="true" focused="false" '
            'screencoord="(865,61)" size="(290,17)"/>'
            '</panel></application></desktop-frame>')
    targets, space = _space(tree)
    assert "Play" not in [t.name for t in targets]
    assert any("Desktop search" in c.description for c in space.target_heads["click"])
    assert not space.target_heads["type_text"]  # Must focus the observed field first.
    _, space = _space(tree.replace('focused="false"', 'focused="true"'), [
        {"label": "Application", "target_hint": "Desktop search", "text": "Shotcut",
         "input_mode": "replace"}])
    assert gui_action(space.target_heads["type_text"][0], None).parameters["text"] == "Shotcut"


def test_hidden_overview_does_not_take_over_the_foreground():
    tree = ('<desktop-frame foreground_class=\'["app"]\'><application name="app" active="true"/>'
            '<application name="gnome-shell"><panel name="Overview">'
            '<text editable="true" showing="false"/></panel></application></desktop-frame>')
    assert 'name="app"' in foreground_tree(tree)


def test_slider_has_bounded_distinct_parameters_and_checks_the_intended_value():
    tree = ('<application active="true"><slider name="time" value="50" min="0" max="100" '
            'focused="true" screencoord="(100,100)" size="(1001,20)"/></application>')
    targets, space = _space(tree)
    choices = space.target_heads["set_slider"]
    assert len(choices) == 4 and not space.target_heads["click"]
    assert len({_action_key(c, targets) for c in choices}) == 4
    winner = next(c for c in choices if c.metadata["fraction"] == 0.75)
    assert gui_action(winner, targets[0]).parameters["x"] == 850
    pending = {"operation": "set_slider", "target_semantic": targets[0].semantic_key, "fraction": 0.75}
    assert _target_effect(pending, targets) == "not_met"
    updated, _ = _space(tree.replace('value="50"', 'value="75"'))
    assert _target_effect(pending, updated) == "met"


def test_new_source_fact_does_not_clear_a_failed_gui_action(tmp_path):
    s = session()
    s.begin("Edit", requirements())
    s.goal.memory.start({**plan(), "purpose": "prepare"}, 0)
    s.goal.memory.source_keys.add("quota")
    s.goal.failed_actions = {("R1:unit:1", "state", "candidate"): 2}
    s.goal.memory.execution_constraints = [{"work_item_id": "R1:unit:1"}]
    s.components.stage_verifier = Mock(return_value=result(
        unit_met=False, stage_outcome="not_met", delivery_state="none", evidence_refs=["S1:O2"],
        facts=[fact(evidence_refs=["S1:O2"])]))
    s.goal.end_reason = "returned_to_planner"
    with pytest.raises(ObservationRequired):
        s.goal._finish(s, make_observation(tmp_path, 2))
    assert s.goal.memory.facts["quota"]["value"] == "1400000 USD"
    assert s.goal.failed_actions and s.goal.memory.execution_constraints


def test_planner_application_handoff_supplies_actual_typable_input(tmp_path):
    s = session()
    s.begin("Open application", requirements())

    class Client:
        def json_request(self, messages, validate, **kwargs):
            value = {**plan(), "target_application": "Shotcut"}
            assert validate(value)
            return value

    shot = tmp_path / "screen.png"
    shot.write_bytes(b"synthetic")
    value = plan_goal(Client(), "Open application", shot, [], [], "", memory=s.goal.memory)
    assert value["inputs"][-1]["text"] == "Shotcut"


def test_contract_repair_names_the_actual_field_or_semantic_failure():
    s = session()
    s.begin("Edit", requirements())
    assert goal_error({**plan(), "work_item_id": "R9"}, s.goal.memory, True).startswith("work_item_id")
    assert goal_error({**plan(), "inputs": [{}]}, s.goal.memory, True).startswith("inputs[0]")
    assert goal_error({**plan(), "work_item_id": {}}, s.goal.memory, True).startswith("work_item_id")
    missing = plan()
    missing.pop("command")
    assert goal_error(missing, s.goal.memory, True).startswith("command")
    item = s.goal.memory.work_items["R1:unit:1"]
    item.update(stalled_stages=2, last_attempt={"route": "cli", "command": "same"})
    assert "repeats" in goal_error({**plan(), "route": "cli", "command": "same"}, s.goal.memory, True)
    assert result_error(result(facts=[{**fact(), "value": {}}])).startswith("facts[0].value")
    assert result_error(result(stage_outcome=[])).startswith("stage_outcome")
    assert result_error(result(materials=[{"source_ref": "x", "summary": "x", "evidence_refs": []}]
                               )).startswith("materials[0].covered_parts")


def test_planner_repair_preserves_punctuation_in_memory_keys(tmp_path):
    s = session()
    s.begin("Edit the supplied deck", requirements())
    exact = "deck.pptx_open_document"
    s.goal.memory.facts[exact] = fact(key=exact, scope="task")

    class Client:
        def json_request(self, messages, validate, **kwargs):
            assert not validate({**plan(), "input_fact_keys": ["deck_pptx_open_document"]})
            assert "copy keys EXACTLY" in messages[-1]["content"]
            assert exact in messages[-1]["content"]
            value = {**plan(), "input_fact_keys": [exact]}
            assert validate(value)
            return value

    observation = make_observation(tmp_path, 0)
    value = plan_goal(Client(), "Edit", Path(observation.screenshot.path), [], [], "",
                      memory=s.goal.memory)
    assert value["input_fact_keys"] == [exact]


def test_gui_plan_rejects_multiple_values_for_one_field_but_allows_distinct_fields():
    s = session()
    s.begin("Read the references", requirements())
    inputs = [{"label": str(i), "target_hint": "Address and search bar", "text": f"https://example/{i}",
               "input_mode": "replace"} for i in range(2)]
    assert "conflicting texts" in goal_error({**plan(), "inputs": inputs}, s.goal.memory, True)
    inputs[1]["target_hint"] = "Search"
    assert not goal_error({**plan(), "inputs": inputs}, s.goal.memory, True)


def test_different_fields_and_changed_pages_are_not_the_same_failed_action():
    first = build_targets()
    second = build_targets(A11Y_XML.replace("确定", "Next page"))
    one = build_space(first).target_heads["click"][0]
    two = build_space(second).target_heads["click"][0]
    assert _action_key(one, first) != _action_key(two, second)
