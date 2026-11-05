"""Synthetic regressions for the execution/observation/delivery handoffs; not benchmark scores."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from jevduo.gui.goal_candidates import (
    foreground_tree,
    goal_candidates,
    modal_frontier,
    portal_needs_grounding,
)
from jevduo.gui.interaction import GuiInteractionController, target_effect
from jevduo.gui.semantics import enrich_targets
from jevduo.memory.context import execution_context, planning_context, verification_context
from jevduo.memory.state import TaskMemory
from jevduo.runtime.goal_stage import ObservationRequired
from jevduo.verification.verifier import verify_observation

from .conftest import build_space, build_targets, cli_receipt, make_observation
from .test_goal_memory import session
from .test_goal_stage import plan, requirements, result

PORTAL = '''<desktop-frame foreground_class='["google-chrome"]' foreground_title="Form - Chrome">
<application name="gnome-shell"/><application name="google-chrome" active="true">
<frame name="Form - Chrome"><push_button name="Submit" screencoord="(900,500)" size="(80,30)"/>
</frame></application><application name="xdg-desktop-portal-gnome"><dialog name="Open Files"
modal="true" visible="true"><push_button name="_Open Open" sensitive="true" visible="true"/>
</dialog></application></desktop-frame>'''


def test_coordinate_free_portal_is_retained_for_visual_confirmation():
    tree = foreground_tree(PORTAL)
    assert "_Open Open" in tree and "Submit" in tree
    assert portal_needs_grounding(tree) == "Open Files"
    # Coordinate-free nodes are not turned into invented click coordinates.
    assert not [t for t in build_targets(tree) if t.name == "_Open Open"]


def test_coordinate_free_portal_requests_grounder_despite_rich_background(tmp_path):
    s = session()
    s.begin("Upload the document", requirements())
    s.goal.memory.start(plan(), 0)
    s.components.grounder = Mock(return_value=[])
    s.act(make_observation(tmp_path, 0, tree_xml=PORTAL))
    s.components.grounder.assert_called_once()


def test_native_dialog_keeps_focus_and_selection_keys():
    tree = '<desktop-frame><dialog name="Open Files" active="true" modal="true" '
    tree += 'screencoord="(100,100)" size="(800,600)"/></desktop-frame>'
    space = goal_candidates(build_space([]), [], tree, [])
    modal_frontier(space, [], tree)
    offered = [c.metadata.get("key_sequence") for c in space.candidates() if c.operation == "keypress"]
    assert [["f6"]] in offered and [["space"]] in offered
    assert [["ctrl", "t"]] not in offered  # Do not navigate behind an active file dialog.


def test_foreground_window_missing_from_a11y_cannot_offer_background_controls(tmp_path):
    tree = '''<desktop-frame foreground_title="Open File" foreground_class='["google-chrome"]'>
    <application name="Google Chrome"><frame name="Form - Google Chrome"><push_button name="Submit"
    screencoord="(900,500)" size="(80,30)"/></frame></application></desktop-frame>'''
    assert not enrich_targets(build_targets(tree), tree, (1920, 1080))
    s = session()
    s.begin("Open the reference", requirements())
    s.goal.memory.start(plan(), 0)
    s.components.grounder = Mock(return_value=[])
    s.act(make_observation(tmp_path, 0, tree_xml=tree))
    s.components.grounder.assert_called_once()


def test_field_hint_does_not_replace_preservation_goal():
    controller = GuiInteractionController()
    context = {"goal": "Read the reference in a NEW tab and preserve the filled form",
               "completion_criteria": "Reference read and form retained", "inputs": [],
               "next_intent": "Type the reference URL into the address bar", "constraints": [],
               "method": "Open a new tab first", "guidance": "Never replace the existing form tab"}
    view = controller.choice_context(context)
    assert view["objective"] == context["goal"]
    assert view["next_action_hint"] == context["next_intent"]
    assert view["guidance"] == context["guidance"]


def test_opened_document_consumes_its_now_absent_path_entry():
    memory = TaskMemory("Read reference", requirements())
    value = {"label": "Reference path", "target_hint": "Enter location or URL", "text": "/tmp/reference.png",
             "input_mode": "replace", "commit": "enter"}
    memory.start({**plan(), "purpose": "prepare", "inputs": [value]}, 0)
    controller = GuiInteractionController()
    controller.stage_id = memory.active_stage["id"]
    key = json.dumps([value["target_hint"].casefold(), value["text"]], ensure_ascii=False)
    controller.fields[key] = {"target": value["target_hint"], "expected_value": value["text"],
                              "phase": "submitted_unconfirmed", "commit": "enter"}
    tree = '<desktop-frame foreground_class=\'["eog"]\' foreground_title="reference.png - Image Viewer"/>'
    space = goal_candidates(build_space([]), [], tree, [value])
    context = controller.finalize(space, [], memory.active_stage, memory, tree)
    assert context["unbound_inputs"] == [] and controller.fields[key]["phase"] == "consumed"
    assert controller.choice_context({**execution_context(memory), **context})["inputs"] == []


def test_unrelated_focused_field_cannot_disprove_a_visual_input():
    target = replace(build_targets()[0], name="Address and search bar", value="old", focused=True)
    pending = {"operation": "type_text", "field_label": "Enter location or URL",
               "expected_text": "/tmp/reference.png", "input_mode": "replace"}
    assert target_effect(pending, [target]) == "unknown"


def test_unpublished_working_copy_is_not_saved_delivery_then_can_be_published():
    memory = TaskMemory("Update the board deck", requirements())
    memory.start(plan(), 0)
    first = result(artifacts=[{"target_path": "/tmp/working.pptx", "working_path": "/tmp/working.pptx",
                               "save_state": "saved", "evidence_refs": ["A2"]}])
    memory.apply(first, {"A2": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "pending"
    assert first["delivery_state"] == "implemented" and not first["unit_met"]
    assert planning_context(memory)["pending_publications"]
    memory.start({**plan(), "purpose": "save"}, 2)
    merely_open = result(evidence_refs=["A3"])
    memory.apply(merely_open, {"A3": {}})
    assert not merely_open["unit_met"]  # Opening the temporary copy does not publish it.
    memory.start({**plan(), "purpose": "save"}, 3)
    final = result(evidence_refs=["A3"], artifacts=[{
        "target_path": "/home/user/Desktop/board.pptx", "working_path": "/home/user/Desktop/board.pptx",
        "save_state": "saved", "replaces_working_path": "/tmp/working.pptx", "evidence_refs": ["A3"]}])
    memory.apply(final, {"A3": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "done"
    assert not planning_context(memory)["pending_publications"]


def test_rollback_revokes_requirement_status_not_confirmed_source_facts():
    reqs = requirements()
    reqs[0]["units"].append("Another section")
    reqs[0]["unit_fact_keys"].append([])
    memory = TaskMemory("Complete form", reqs)
    memory.start(plan(), 0)
    fact = {"key": "source", "value": "Stable source", "status": "confirmed", "scope": "task",
            "source_ref": "input.pdf", "evidence_refs": ["A2"]}
    memory.apply(result(facts=[fact], requirement_checks=[{
        "id": "R1", "status": "met", "evidence_refs": ["A2"]}]), {"A2": {}})
    memory.start({**plan(), "work_item_id": "R1:unit:2", "purpose": "prepare"}, 2)
    memory.apply(result(unit_met=False, delivery_state="none", evidence_refs=["A3"],
                        invalidated_work_item_ids=["R1:unit:1"]), {"A3": {}})
    assert memory.requirements[0]["status"] == "uncertain"
    assert memory.work_items["R1:unit:1"]["status"] == "pending"
    assert memory.facts["source"]["value"] == "Stable source"


def test_real_dispatch_resets_terminal_rejection_counter(tmp_path):
    s = session()
    s.begin("Edit", requirements())
    s.goal.memory.start({**plan(), "route": "cli", "command": "true"}, 0)
    s.consecutive_no_action = 2
    assert s.act(make_observation(tmp_path, 0)).kind == "cli"
    with pytest.raises(ObservationRequired, match="stage_verification_returned"):
        s.act(make_observation(tmp_path, 1, receipt=cli_receipt()))
    assert s.consecutive_no_action == 0


def test_terminal_verifier_receives_independent_stage_ledger(tmp_path):
    memory = TaskMemory("Finish", requirements())
    memory.start(plan(), 0)
    memory.apply(result(), {"A2": {}})
    client = Mock()

    def request(messages, validate, **kwargs):
        context = json.loads(messages[1]["content"][0]["text"])
        assert context["verified_task_memory"]["work_items"][0]["status"] == "done"
        assert "simultaneously" in messages[0]["content"]
        return {"outcome": "achieved", "task_status": "completed", "evidence": "stage A2",
                "path_input_focused": False, "state_changed": False, "requirement_checks": []}

    client.json_request.side_effect = request
    make_observation(tmp_path, 0)
    verify_observation(client, "Finish", tmp_path / "step_00.png", terminal_claim={"done": True},
                       task_memory=verification_context(memory))
    client.json_request.assert_called_once()


def test_planner_gets_actual_wall_budget(tmp_path):
    s = session()
    s.begin("Edit", requirements())
    s.components.planner = Mock(return_value=plan())
    with pytest.raises(ObservationRequired, match="planner_returned"):
        s.act(make_observation(tmp_path, 0, wall=321))
    assert s.components.planner.call_args.kwargs["remaining_seconds"] == 321
