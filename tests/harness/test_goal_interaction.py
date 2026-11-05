"""Synthetic control/acceptance regressions; not real GUI or benchmark acceptance."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw

from jevduo.components import build_default
from jevduo.config import SessionConfig
from jevduo.gui.candidates import ActionCandidate
from jevduo.gui.compile import gui_action
from jevduo.gui.goal_candidates import _input_target, application_mismatch, goal_candidates, input_frontier
from jevduo.gui.interaction import GuiInteractionController, visual_changed
from jevduo.memory.context import planning_context
from jevduo.memory.state import TaskMemory
from jevduo.models.ranking import Ranking
from jevduo.planning.goal_planner import goal_error
from jevduo.runtime.goal_stage import ObservationRequired, _action_key
from jevduo.verification.goal_verifier import result_error, verify_goal

from .conftest import build_space, gui_receipt, make_observation
from .test_goal_execution import _space
from .test_goal_integration import _shell_observation
from .test_goal_memory import session
from .test_goal_progress import FORM
from .test_goal_stage import plan, requirements, result


def _input(text="laptop.pptx", hint="File name", **kw):
    return {"label": hint, "target_hint": hint, "text": text, "input_mode": "replace", **kw}


def _visual(value, point=(20, 45)):
    return ActionCandidate("visual-input", "type_text", "Type into the observed field",
        source="visual_grounder", metadata={"visual_point": list(point), "visual_bounds": [10, 40, 30, 10],
        "target_hint": value["target_hint"], "target_label": value["target_hint"],
        "field_label": value["target_hint"], "text": value["text"], "input_mode": "replace"})


def _checked_result(**kw):
    v = result(progress_kind="content", observed_errors=[], **kw)
    v["unit_assessment"] = {"work_item_id": "R1:unit:1", "status": "met" if v["unit_met"] else "not_met",
                            "evidence_refs": ["A2"], "remaining": []}
    return v


def test_interaction_factory_is_injectable():
    built = build_default(client=Mock(), endpoint="http://unused",
                          config=SessionConfig(execution_mode="goal"))
    assert built.interaction_factory is GuiInteractionController
    s = session()
    controller = GuiInteractionController()
    s.components.interaction_factory = Mock(return_value=controller)
    s.begin("Edit", requirements())
    assert s.goal.interaction is controller


def test_tab_misselection_preserves_visual_input_and_specific_intent(tmp_path):
    s = session()
    s.begin("Save laptop.pptx", requirements())
    value = _input()
    s.goal.memory.start({**plan(), "inputs": [value]}, 0)
    choice = _visual(value)
    choice.metadata.update(blocking_overlay=True, overlay_name="Save As", overlay_bounds=[5, 30, 50, 30])
    s.components.grounder = Mock(return_value=[choice])
    selected = []

    def rank(**kw):
        assert any(c.operation == "type_text" for c in kw["action_space"].candidates())
        assert "laptop.pptx" in json.loads(kw["intent"])["next_action_hint"]
        winner = (next(c for c in kw["action_space"].candidates() if c.id == "KEYPRESS_TAB")
                  if not selected else choice)
        selected.append(winner)
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    first = _shell_observation(tmp_path / "first")
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(first)
    assert s.act(first).action_type == "hotkey"
    second = replace(_shell_observation(tmp_path / "second", step=1), last_receipt=gui_receipt())
    assert s.act(second).parameters["text"] == "laptop.pptx"
    s.components.grounder.assert_called_once()


def test_scroll_invalidates_cached_coordinates_even_if_pixels_happen_to_match(tmp_path):
    controller = GuiInteractionController()
    obs = _shell_observation(tmp_path / "scene")
    frame = {"state": ("same",), "screenshot": obs.screenshot.path}
    assert controller.reusable_visual(frame, ("same",), Path(obs.screenshot.path))
    controller.receive({"operation": "scroll"}, "unknown", False, 0)
    assert not controller.reusable_visual(frame, ("same",), Path(obs.screenshot.path))


@pytest.mark.parametrize("invalidating_id", ["SCROLL_DOWN", "GOAL_KEY:alt+tab", "GOAL_KEY:ctrl+o",
                                            "GOAL_KEY:ctrl+l"])
def test_new_grounding_after_invalidating_action_reaches_selector(tmp_path, invalidating_id):
    s = session()
    s.begin("Open the visible tool", requirements())
    s.goal.memory.start(plan(), 0)
    visual = ActionCandidate("visible-tool", "click", "Open the visible tool", source="visual_grounder",
                             metadata={"visual_point": [20, 40]})
    s.components.grounder = Mock(return_value=[visual])
    selected = []

    def rank(**kw):
        choices = kw["action_space"].candidates()
        assert visual in choices
        winner = next(c for c in choices if c.id == invalidating_id) if not selected else visual
        selected.append(winner)
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    first = _shell_observation(tmp_path / "first")
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(first)
    assert s.act(first).kind == "gui"
    second = replace(_shell_observation(tmp_path / "second", step=1), last_receipt=gui_receipt())
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(second)
    assert not s.goal.interaction.invalidate_visual
    # Real workers refresh again without replaying the preceding scroll/hotkey.
    refreshed = _shell_observation(tmp_path / "refreshed", step=1)
    assert s.act(refreshed).parameters == {"x": 20, "y": 40}
    assert selected == [selected[0], visual]
    assert s.components.grounder.call_count == 2
    assert not any(e.get("route") == "recovery_exhausted" for e in s.events)


def test_field_identity_beats_spatial_hint_and_unrelated_nearby_text():
    targets, _ = _space(FORM)
    base = next(t for t in targets if t.name == "Title *")
    fields = [replace(base, id="from", name="From", nearby_text="From", input_type="select-one"),
              replace(base, id="to", name="To", nearby_text="Return date", input_type="select-one"),
              replace(base, id="date", name="Departure date", nearby_text="To")]
    values = [_input("Shanghai", "From city selector", label="Departure city"),
              _input("Beijing", "To city selector", label="Arrival city"),
              _input("March 25", "Departure date calendar immediately right of the To selector",
                     label="Departure date")]
    assert [_input_target(v, fields).id for v in values] == ["from", "to", "date"]
    space = goal_candidates(build_space(fields), fields, FORM, values)
    assert {c.target_id: c.metadata["text"] for c in space.target_heads["type_text"]} == {
        "from": "Shanghai", "to": "Beijing", "date": "March 25"}
    frontier = input_frontier(space, fields, values)
    assert not frontier["conflicting_fields"] and not frontier["unbound_inputs"]


def test_duplicate_observed_labels_remain_unbound_instead_of_conflicting():
    targets, _ = _space(FORM)
    base = next(t for t in targets if t.name == "Title *")
    fields = [replace(base, id="first"), replace(base, id="second")]
    values = [_input("Known title", "Title")]
    assert _input_target(values[0], fields) is None
    space = goal_candidates(build_space(fields), fields, FORM, values)
    frontier = input_frontier(space, fields, values)
    assert not space.target_heads["type_text"] and not frontier["conflicting_fields"]
    assert frontier["unbound_inputs"] == ["Title"]


def test_small_input_change_is_visible_in_target_roi_but_not_global_average(tmp_path):
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    image = Image.new("RGB", (1920, 1080), "white")
    image.save(before)
    ImageDraw.Draw(image).text((150, 1052), "Known command", fill="black")
    image.save(after)
    assert not visual_changed(before, after)
    assert visual_changed(before, after, [130, 1048, 1600, 30])


def test_visual_field_identity_does_not_change_with_detector_box_jitter():
    a, b = _visual(_input()), _visual(_input(), (24, 46))
    a.description, b.description = "Field at [10,40,30,10]", "Field at [14,41,30,10]"
    assert _action_key(a) == _action_key(b)
    b.metadata["text"] = "different.pptx"
    assert _action_key(a) != _action_key(b)


def test_command_has_separate_commit_and_no_premature_done():
    controller = GuiInteractionController()
    memory = TaskMemory("Compute", requirements())
    value = _input('Example("known")', "Command input", kind="command", commit="enter")
    stage = {**plan(), "id": "S1", "inputs": [value]}
    targets, space = _space(FORM)
    space.target_heads["visual"] = [_visual(value)]
    controller.constrain(space, targets, stage, memory)
    choice = space.target_heads["visual"][0]
    key = choice.metadata["interaction_binding"]
    controller.receive({"operation": "type_text", "interaction_binding": key}, "unknown", True, 0)
    context = controller.constrain(space, targets, stage, memory)
    commit = next(c for c in space.candidates() if c.metadata.get("commit_action"))
    assert "ENTER" in context["next_intent"] and "done" not in space.controls
    assert not any(c.operation == "type_text" for c in space.candidates())
    action = gui_action(commit, None)
    assert action.parameters == {"text": "\n", "input_mode": "insert", "click": [20, 45]}
    controller.receive({"operation": "keypress", "interaction_binding": key,
                        "commit_action": True}, "unknown", True, 0)
    assert controller.snapshot()["fields"][0]["phase"] == "submitted_unconfirmed"


def test_native_select_uses_prefix_and_enter_instead_of_enumerating_countries():
    raw, _ = _space(FORM)
    field = replace(next(t for t in raw if t.name == "Title *"), name="Country of Citizenship",
                    role="combo_box", input_type="select-one", value="Afghanistan")
    value = _input("China", "Country of Citizenship", kind="choice")
    space = goal_candidates(build_space([field]), [field], FORM, [value])
    choice = space.target_heads["type_text"][0]
    action = gui_action(choice, field)
    assert action.parameters["text"] == "China\n" and action.parameters["input_mode"] == "insert"
    filled = goal_candidates(space, [replace(field, value="China")], FORM, [value])
    assert not filled.target_heads["type_text"]


def test_target_application_excludes_known_other_dock_apps_and_generalizes_to_geogebra():
    assert application_mismatch("GeoGebra Classic 5",
        '<desktop-frame foreground_class=\'["soffice"]\' foreground_title="Untitled - LibreOffice"/>')
    assert not application_mismatch("GeoGebra Classic 5",
        '<desktop-frame foreground_class=\'["org-geogebra-desktop-GeoGebra3D"]\'/>')
    tree = '''<desktop-frame><application name="gnome-shell" active="true">
      <push_button name="Activities" screencoord="(0,0)" size="(80,25)"/>
      <push_button name="LibreOffice Impress" screencoord="(0,100)" size="(60,60)"/>
      <push_button name="WPS 2019" screencoord="(0,200)" size="(60,60)"/>
      </application></desktop-frame>'''
    targets, space = _space(tree)
    GuiInteractionController().constrain(space, targets, {**plan(), "id": "S1", "inputs": [],
        "target_application": "WPS Presentation"}, TaskMemory("Edit", requirements()))
    offered = [c.description for c in space.candidates()]
    assert any("Activities" in s for s in offered) and any("WPS 2019" in s for s in offered)
    assert not any("LibreOffice" in s for s in offered)


def test_original_error_survives_advice_and_blocks_same_method_across_work_items():
    memory = TaskMemory("Import image", requirements())
    memory.start(plan(), 0)
    memory.apply(result(stage_outcome="not_met", unit_met=False, delivery_state="none",
        summary="Image was not imported", remaining=["Try adding more arguments"], observed_errors=[{
            "message": "Unknown command : Example", "command_name": "Example",
            "application": "GeoGebra Classic 5", "evidence_refs": ["A2"]}]), {"A2": {}})
    entry = next(f for f in memory.failures if f.get("observed_method"))
    assert entry["error"] == "Unknown command : Example"
    assert entry["suggested_actions"] == ["Try adding more arguments"]
    value = {**plan(), "target_application": "GeoGebra Classic 5", "inputs": [_input('Example("other", 2)')]}
    assert "unsupported command" in goal_error(value, memory, True)
    memory.failures.extend({"error": "Another failure"} for _ in range(15))
    assert planning_context(memory)["unsupported_methods"][0]["command_name"] == "Example"
    assert not memory.blocked_command("OtherCommand(1)", "GeoGebra Classic 5")
    assert not memory.blocked_command("Example(1)", "Another application")


def test_navigation_never_becomes_content_delivery_or_resets_delivery_stall():
    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    memory.apply(result(unit_met=False, progress_kind="navigation", delivery_state="implemented",
        milestones=[{"key": "window-maximized", "summary": "Window maximized", "evidence_refs": ["A2"]}]),
        {"A2": {}})
    item = memory.work_items["R1:unit:1"]
    assert item["delivery_state"] == "none" and not item["milestones"] and item["stalled_stages"] == 1


def test_save_unit_can_finish_without_accepting_sibling_animation(tmp_path):
    req = requirements()[0]
    req.update(units=["File saved and open", "Animation matches tutorial"], unit_fact_keys=[[], []])
    memory = TaskMemory("Save the file and build the animation", [req])
    memory.start({**plan(), "purpose": "verify", "completion_criteria": "File saved and open"}, 0)
    obs = make_observation(tmp_path, 2)

    class Client:
        config = SessionConfig()

        def json_request(self, messages, validate, **kwargs):
            context = json.loads(messages[1]["content"][0]["text"])
            assert context["unit_acceptance"]["criterion"] == "File saved and open"
            assert context["sibling_units_not_current_acceptance"][0]["goal"] == "Animation matches tutorial"
            bad = _checked_result(unit_met=False)
            bad["unit_assessment"]["status"] = "met"
            assert not validate(bad)
            good = _checked_result()
            assert validate(good)
            return good

    checked = verify_goal(Client(), memory.instruction, Path(obs.screenshot.path), memory=memory,
                          history=[], end_reason="stage_done_requested", evidence={"A2": {}})
    memory.apply(checked, {"A2": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "done"
    assert memory.work_items["R1:unit:2"]["status"] == "pending"


def test_input_uncertainty_does_not_skip_semantic_verification(tmp_path):
    memory = TaskMemory("Submit a command", requirements())
    memory.start(plan(), 0)
    obs = make_observation(tmp_path, 2)
    client = Mock(config=SessionConfig(), json_request=Mock(return_value=_checked_result(unit_met=False)))
    verify_goal(client, "Submit a command", Path(obs.screenshot.path), memory=memory, evidence={"A2": {}},
                history=[{"kind": "gui", "operation": "type_text", "relevant_change": False,
                          "screenshot": obs.screenshot.path}], end_reason="stage_done_requested")
    client.json_request.assert_called_once()


def test_last_interaction_is_preserved_as_historical_state_not_source_fact():
    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    checkpoint = {"stage_id": "S1", "fields": [{"target": "Title", "phase": "typed_unconfirmed"}]}
    memory.active_stage["interaction"] = checkpoint
    memory.apply(result(unit_met=False, delivery_state="none"), {"A2": {}})
    assert memory.snapshot()["last_interaction"] == checkpoint
    assert planning_context(memory)["last_interaction_historical"] == checkpoint
    assert not memory.facts


def test_null_unit_assessment_is_a_contract_error_not_a_runtime_exception():
    value = _checked_result()
    value["unit_assessment"] = None
    assert "unit_assessment" in result_error(value)


def test_prepare_verifies_sources_without_model_delivery_assessment(tmp_path):
    memory = TaskMemory("Read source material", requirements())
    memory.start({**plan(), "purpose": "prepare"}, 0)
    obs = make_observation(tmp_path, 2)

    class Client:
        config = SessionConfig()

        def json_request(self, messages, validate, **kwargs):
            context = json.loads(messages[1]["content"][0]["text"])
            assert "unit_assessment" not in context["response_template"]
            assert context["unit_acceptance"]["scope"] == "not_assessed_during_prepare"
            source = result(unit_met=False, delivery_state="none", progress_kind="source", observed_errors=[])
            assert validate(source)
            # A model's redundant not_met delivery assessment must not cause another API call.
            source["unit_assessment"] = {"work_item_id": "R1:unit:1", "status": "not_met",
                                         "evidence_refs": [], "remaining": []}
            assert validate(source)
            assert not validate({**source, "unit_met": True, "delivery_state": "verified"})
            assert not validate({**source, "facts": [{"key": "number", "value": 25}]})
            return source

    checked = verify_goal(Client(), memory.instruction, Path(obs.screenshot.path), memory=memory,
                          history=[], end_reason="stage_done_requested", evidence={"A2": {}})
    assert checked["unit_assessment"] == {"work_item_id": "R1:unit:1", "status": "not_assessed",
                                          "evidence_refs": [], "remaining": []}
    assert checked["unit_met"] is False and checked["delivery_state"] == "none"
