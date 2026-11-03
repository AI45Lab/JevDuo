"""Synthetic mechanism regressions, not real task acceptance or model quality measurements."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from tandem.gui.candidates import ActionCandidate
from tandem.gui.compile import gui_action
from tandem.gui.goal_candidates import application_mismatch, document_frontier, input_frontier, modal_frontier
from tandem.gui.grounding import ground_visual
from tandem.gui.semantics import enrich_targets
from tandem.memory.context import planning_context
from tandem.memory.state import TaskMemory
from tandem.models.ranking import Ranking
from tandem.planning.goal_planner import goal_error, goal_requirements, plan_goal
from tandem.runtime.goal_stage import _action_key, _target_effect, _visual_changed
from tandem.verification.goal_verifier import result_error, verify_goal

from .conftest import build_space, make_observation
from .test_goal_execution import _space
from .test_goal_memory import session
from .test_goal_progress import FORM
from .test_goal_stage import plan, requirements, result


def test_field_alias_is_unique_and_already_filled_value_is_recognized():
    tree = FORM.replace("Title *", "Brief diagnosis summary (required)").replace(
        'size="(200,30)"/>', 'size="(200,30)">Known diagnosis</entry>', 1)
    inputs = [{"label": "Diagnosis", "target_hint": "Diagnosis Summary", "text": "Known diagnosis",
               "input_mode": "replace"}]
    targets, space = _space(tree, inputs)
    frontier = input_frontier(space, targets, inputs)
    assert frontier["satisfied_inputs"] == ["Diagnosis"]
    assert not frontier["unbound_inputs"] and not space.target_heads["type_text"]
    targets, space = _space(tree.replace("Issue number", "Diagnosis Summary"), inputs)
    assert not space.target_heads["type_text"]  # A broad label never resolves ambiguous fields.


@pytest.mark.parametrize("order", [("month", "day", "year"), ("day", "month", "year")])
@pytest.mark.parametrize("suffix", ["", " Departure date"])
def test_native_date_uses_observed_segments_and_checks_iso_value(order, suffix):
    targets, space = _space(FORM)
    original = next(t for t in targets if t.name == "Title *")
    snapshot = {"elements": [{"index": 1, "role": "entry", "name": "Departure date",
                 "input_type": "date", "date_order": order, "value": "", "label_source": "dom",
                 "x": original.x, "y": original.y, "width": original.width, "height": original.height,
                 "operations": ["click", "type_text"]}]}
    targets = [replace(t, application="Google Chrome") for t in targets]
    target = next(t for t in enrich_targets(targets, FORM, (1920, 1080), snapshot)
                  if t.name == "Departure date")
    from tandem.gui.goal_candidates import goal_candidates

    inputs = [{"label": "Travel date", "target_hint": "Departure date", "text": "2026-03-25",
               "input_mode": "replace"}]
    parts = [replace(target, id=f"part-{name}", role="spin_button", name=name.title() + suffix, input_type="",
                     x=target.x + index * 40, width=30, value="0") for index, name in enumerate(order)]
    for name, expected in [("Year", "2026"), ("Month", "03"), ("Day", "25")]:
        space = goal_candidates(build_space([target, *parts]), [target, *parts], FORM, inputs)
        choice = space.target_heads["type_text"][0]
        segment = next(p for p in parts if p.id == choice.target_id)
        action = gui_action(choice, segment)
        assert segment.name == name + suffix and action.parameters["text"] == expected
        assert action.parameters["click"][0] == segment.x + segment.width // 2
        parts = [replace(p, value=expected) if p.id == segment.id else p for p in parts]
    assert not goal_candidates(space, [replace(target, value="2026-03-25")], FORM,
                               inputs).target_heads["type_text"]
    inputs[0]["text"] = "03252026"
    assert "ISO" in input_frontier(space, [target], inputs)["conflicting_fields"][0]


def test_portal_dialog_survives_stale_foreground_metadata():
    tree = '''<desktop-frame foreground_title="Home" foreground_class='["nautilus"]'>
      <application name="gnome-shell"/>
      <application name="nautilus" active="true"><push_button name="Trash"
        screencoord="(50,500)" size="(100,30)"/></application>
      <application name="xdg-desktop-portal-gnome"><dialog name="Open File" modal="true" visible="true"
        showing="true">
        <push_button name="Select" screencoord="(300,100)" size="(80,30)"/>
      </dialog></application></desktop-frame>'''
    targets, space = _space(tree)
    assert "Select" in [t.name for t in targets] and "Trash" not in [t.name for t in targets]
    context = modal_frontier(space, targets, tree)
    assert context["blocking_overlay"] == "Open File"
    assert any(c.target_id for c in space.target_heads["click"])
    assert not space.target_heads["scroll"] and "done" not in space.controls


def test_visual_modal_keeps_close_but_removes_background_and_false_progress(tmp_path):
    targets, space = _space(FORM)
    close = ActionCandidate(id="close-overlay", operation="click", description="Close the modal",
                            source="visual_grounder", metadata={"visual_point": [350, 250],
                            "blocking_overlay": True, "overlay_name": "System Check",
                            "overlay_bounds": [200, 200, 200, 100]})
    space.target_heads["visual"] = [close]
    context = modal_frontier(space, targets, FORM)
    assert space.target_heads["visual"] == [close] and not space.target_heads["scroll"]
    assert "System Check" in context["next_intent"]
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    Image.new("RGB", (600, 600), "black").save(before)
    frame = Image.new("RGB", (600, 600), "white")
    frame.paste("black", (200, 200, 400, 300))
    frame.save(after)
    assert _visual_changed(before, after)
    assert not _visual_changed(before, after, context["overlay_bounds"])


def test_grounder_records_explicit_overlay_without_inventing_one(tmp_path):
    obs = make_observation(tmp_path, 0)
    client = Mock()
    client.json_request.return_value = {"blocking_overlay": {"name": "Warning", "bounds": [1, 1, 40, 40]},
        "candidates": [{"operation": "click", "target": "Close", "bounds": [5, 5, 20, 20]}]}
    choices = ground_visual(client, "Edit", {"action_proposals": []}, Path(obs.screenshot.path))
    assert choices[0].metadata["overlay_name"] == "Warning"


def test_wrong_document_can_navigate_but_cannot_receive_article_text():
    inputs = [{"label": "Page", "target_hint": "Address and search bar",
               "text": "file:///home/user/right.html", "input_mode": "replace"},
              {"label": "Title", "target_hint": "Title", "text": "Article title", "input_mode": "replace"}]
    targets, space = _space(FORM, inputs)
    context = document_frontier(space, targets, "right.html")
    assert context["document_recovery"] == "right.html"
    assert [c.metadata["text"] for c in space.target_heads["type_text"]] == [inputs[0]["text"]]
    assert "done" not in space.controls


def test_runtime_gives_jev_a_navigation_chance_on_wrong_document(tmp_path):
    s = session()
    s.begin("Fill right.html", requirements())
    s.goal.memory.start({**plan(), "target_document": "right.html", "inputs": [
        {"label": "Page", "target_hint": "Address and search bar",
         "text": "file:///home/user/right.html", "input_mode": "replace"}]}, 0)

    def rank(**kwargs):
        assert "right.html" in kwargs["stage_goal"] and "Navigate" in kwargs["stage_goal"]
        winner = kwargs["action_space"].target_heads["type_text"][0]
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    action = s.act(make_observation(tmp_path, 0, tree_xml=FORM))
    assert action.parameters["text"] == "file:///home/user/right.html"


def test_date_effect_checks_intended_field_even_after_tab_moves_focus():
    targets, _ = _space(FORM)
    target = next(t for t in targets if t.name == "Title *")
    pending = {"operation": "type_text", "target_semantic": target.semantic_key,
               "field_label": target.name, "expected_text": "2026-03-25", "input_mode": "replace"}
    targets = [replace(t, value="2026-03-25") if t.id == target.id else t for t in targets]
    assert _target_effect(pending, targets) == "met"
    assert _target_effect(pending, [replace(t, value="") for t in targets]) == "not_met"


def test_partial_milestones_advance_once_without_verifying_entire_unit():
    memory = TaskMemory("Fill application", requirements())
    for key in ["application/section-1", "application/section-2", "application/section-2"]:
        memory.start(plan(), 0)
        advanced = memory.apply(result(unit_met=False, milestones=[{
            "key": key, "summary": "Section saved and checked", "evidence_refs": ["A2"]}]), {"A2": {}})
    item = memory.work_items["R1:unit:1"]
    assert not advanced and item["stalled_stages"] == 1
    assert len(item["milestones"]) == 2 and memory.stats["milestone_advances"] == 2
    assert item["delivery_state"] != "verified" and item["status"] != "done"
    assert planning_context(memory)["ready_work_items"][0]["milestones"] == item["milestones"]
    memory.start(plan(), 0)
    memory.apply(result(stage_outcome="uncertain", unit_met=False, delivery_state="none", milestones=[{
        "key": "unsupported", "summary": "Not observed", "evidence_refs": ["A2"]}]), {"A2": {}})
    assert "unsupported" not in item["milestones"]


def test_failed_cli_writes_require_inspection_before_another_write():
    memory = TaskMemory("Edit deck", requirements())
    for _ in range(2):
        memory.start({**plan(), "route": "cli", "command": "different failed command"}, 0)
        memory.apply(result(stage_outcome="uncertain", unit_met=False, delivery_state="none",
                            failure_kind="execution_error"), {"A2": {}})
    candidate = {**plan(), "route": "cli", "command": "another guessed edit"}
    assert "two CLI" in goal_error(candidate, memory, True)
    assert not goal_error({**candidate, "purpose": "prepare"}, memory, True)


def test_task_gui_restriction_overrides_cli_enabled_configuration(tmp_path):
    s = session()
    s.begin("Book tickets", requirements())

    class Client:
        def json_request(self, messages, validate, **kwargs):
            context = json.loads(messages[1]["content"][0]["text"])
            assert context["allow_cli"] is False
            assert not validate({**plan(), "route": "cli", "command": "cat source"})
            assert validate(plan())
            return plan()

    obs = make_observation(tmp_path, 0)
    plan_goal(Client(), "Do not open or use Terminal, VSCode or backend APIs.",
              Path(obs.screenshot.path), [], [], "", memory=s.goal.memory)


@pytest.mark.parametrize("violation", ["", "Edited website source to bypass field validation"])
def test_cli_integrity_is_not_gui_acceptance_and_policy_violations_are_rejected(tmp_path, violation):
    s = session()
    s.begin("Edit presentation", requirements())
    s.goal.memory.start({**plan(), "route": "cli", "purpose": "save"}, 0)
    obs = make_observation(tmp_path, 1)
    client = Mock()
    client.config.stage_verify_max_chars = 12000
    client.json_request.return_value = result(policy_violation=violation,
        policy_constraint="website_source_tampering", policy_evidence_refs=["A2"])
    checked = verify_goal(client, "Edit", Path(obs.screenshot.path), memory=s.goal.memory,
                          evidence={"A2": {}},
                          history=[{"kind": "cli", "evidence_ref": "A2", "screenshot": obs.screenshot.path}],
                          end_reason="cli_returned")
    assert checked["unit_met"] is False
    assert checked["delivery_state"] == ("none" if violation else "saved")
    if violation:
        assert checked["failure_kind"] == "policy_violation" and checked["milestones"] == []
    assert result_error(result(milestones=[{"key": "x", "summary": "saved", "evidence_refs": [{}]}]))


def test_policy_violation_ends_attempt_instead_of_replanning_around_the_rule(tmp_path):
    s = session()
    s.begin("Fill website", requirements())
    s.goal.memory.start(plan(), 0)
    s.goal.end_reason = "returned_to_planner"
    s.components.stage_verifier = Mock(return_value=result(
        stage_outcome="not_met", unit_met=False, delivery_state="none", evidence_refs=["S1:O0"],
        failure_kind="policy_violation", policy_violation="Changed application validation",
        remaining=["Changed application validation"]))
    action = s.act(make_observation(tmp_path, 0))
    assert action.kind == "fail" and "policy_violation" in action.reason


def test_duplicate_requirements_are_repaired_without_discarding_either_delivery():
    first = {**requirements()[0], "requirement": "Create report", "units": ["Report saved"]}
    second = {**first, "requirement": "Create slides", "units": ["Slides saved"]}

    class Client:
        def json_request(self, messages, validate, **kwargs):
            assert not validate({"requirements": [first, second]})
            assert "Duplicate" in messages[-1]["content"]
            repaired = {"requirements": [first, {**second, "id": "R2"}]}
            assert validate(repaired)
            return repaired

    with pytest.raises(ValueError, match="unique"):
        TaskMemory("Create report and slides", [first, second])
    memory = TaskMemory("Create report and slides", goal_requirements(Client(), "Create report and slides"))
    assert [item["goal"] for item in memory.work_items.values()] == ["Report saved", "Slides saved"]


def test_closed_portal_cannot_take_over_chrome_or_remove_its_controls():
    tree = '''<desktop-frame foreground_title="Research Portal - Google Chrome"
      foreground_class='["google-chrome"]'><application name="gnome-shell"/>
      <application name="Google Chrome" active="true"><push_button name="Save"
        screencoord="(300,100)" size="(80,30)"/></application>
      <application name="xdg-desktop-portal-gnome"><dialog name="Open File"
        modal="true" visible="true"/></application></desktop-frame>'''
    targets, space = _space(tree)
    assert [t.name for t in targets] == ["Save"]
    assert modal_frontier(space, targets, tree) == {}
    assert any(c.target_id for c in space.target_heads["click"])
    assert "KEYPRESS_ESCAPE" in [c.id for c in space.target_heads["keypress"]]


def test_anonymous_shell_strip_is_not_renamed_to_activities():
    tree = '''<desktop-frame><application name="gnome-shell" active="true">
      <label name="Activities" screencoord="(18,5)" size="(63,17)"/>
      <push_button screencoord="(62,33)" size="(8,883)"/>
      </application></desktop-frame>'''
    from tandem.gui.targets import A11yTargetBuilder

    raw = A11yTargetBuilder().build(tree, "t", 128, infer_button_labels=True)
    targets = enrich_targets(raw, tree, (1920, 1080))
    assert not any(t.name == "Activities" and t.role == "push_button" for t in targets)


def test_wrong_application_preserves_actual_navigation_shortcuts_not_editing():
    assert application_mismatch("WPS Presentation",
                                '<desktop-frame foreground_title="Untitled - LibreOffice"/>')
    assert not application_mismatch("WPS Presentation", '<desktop-frame foreground_title="deck.pptx - WPS"/>')
    targets, space = _space(FORM)
    document_frontier(space, targets, application="WPS Presentation")
    keys = {c.id for c in space.target_heads["keypress"]}
    assert {"GOAL_KEY:alt+tab", "GOAL_KEY:ctrl+o", "KEYPRESS_ESCAPE"} <= keys
    assert "GOAL_KEY:ctrl+s" not in keys and not space.target_heads["type_text"]
    launcher_tree = '''<desktop-frame><application name="gnome-shell" active="true">
      <push_button name="Activities" screencoord="(0,0)" size="(80,28)"/>
      </application></desktop-frame>'''
    targets, space = _space(launcher_tree)
    document_frontier(space, targets, application="WPS Presentation")
    assert any("Activities" in c.description for c in space.target_heads["click"])


def test_visual_date_cannot_bypass_observed_native_date_adapter(tmp_path):
    client = Mock()
    proposal = {"operation": "type_text", "target_hint": "Incident Time", "text": "2024-04-25",
                "input_mode": "replace"}
    client.json_request.return_value = {"candidates": [{**proposal, "target": "Incident Time",
                                                       "bounds": [1, 1, 20, 20]}]}
    obs = make_observation(tmp_path, 0)
    assert not ground_visual(client, "Fill date", {"action_proposals": [proposal]},
                             Path(obs.screenshot.path))


@pytest.mark.parametrize("constraint,refs", [("", ["A2"]), ("Use keyboard navigation", ["A2"]),
                                             ("website_source_tampering", ["old"])])
def test_planner_method_or_unproven_policy_error_is_recoverable(tmp_path, constraint, refs):
    s = session()
    s.begin("Fill form", requirements())
    s.goal.memory.start(plan(), 0)
    obs = make_observation(tmp_path, 1)
    client = Mock()
    client.config.stage_verify_max_chars = 12000
    client.json_request.return_value = result(policy_violation="Used a wrong shortcut",
                                             policy_constraint=constraint, policy_evidence_refs=refs)
    checked = verify_goal(client, "Fill form", Path(obs.screenshot.path), memory=s.goal.memory,
                          evidence={"A2": {}}, history=[{"kind": "gui", "evidence_ref": "A2",
                          "observed_change": True, "screenshot": obs.screenshot.path}],
                          end_reason="stage_done_requested")
    assert checked["failure_kind"] == "recoverable_stage_error" and not checked["unit_met"]


def test_duplicate_buttons_keep_row_context_and_identity_after_layout_shift():
    from tandem.gui.candidates import ActionCandidateBuilder

    snapshot = {"elements": [{"index": index, "role": "push_button", "name": "Select",
        "nearby_text": row, "x": 100, "y": 100 * index, "width": 80, "height": 30,
        "operations": ["click"]} for index, row in enumerate(["G8 09:00 North", "G14 11:00 South"], 1)]}
    targets = enrich_targets([], None, (1920, 1080), snapshot)
    space = ActionCandidateBuilder().build(targets, context="G14")
    choices = space.target_heads["click"]
    assert "G14" in choices[0].description and "G8" in choices[1].description
    assert _action_key(choices[0], targets) != _action_key(choices[1], targets)
    moved = [replace(t, y=t.y + 100) for t in targets]
    assert _action_key(choices[0], moved) == _action_key(choices[0], targets)


def test_working_artifact_cannot_be_silently_replaced_by_a_fresh_copy():
    memory = TaskMemory("Edit presentation", requirements())
    artifact = {"target_path": "/output/deck.pptx", "working_path": "/work/current.pptx",
                "save_state": "saved", "evidence_refs": ["A2"]}
    for change in [artifact, {**artifact, "working_path": "/work/new.pptx"}]:
        memory.start(plan(), 0)
        memory.apply(result(unit_met=False, delivery_state="saved", artifacts=[change]), {"A2": {}})
    assert memory.artifacts[artifact["target_path"]]["working_path"] == artifact["working_path"]
    assert memory.last_rejections[-1]["reason"] == "working_copy_change_requires_explicit_migration"
    memory.start(plan(), 0)
    memory.apply(result(unit_met=False, delivery_state="saved", artifacts=[{
        **artifact, "working_path": "/work/new.pptx", "replaces_working_path": artifact["working_path"]}]),
        {"A2": {}})
    assert memory.artifacts[artifact["target_path"]]["working_path"] == "/work/new.pptx"
