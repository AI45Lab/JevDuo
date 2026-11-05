"""Synthetic regressions for partial delivery, field binding and semantic navigation stalls."""

import json
from unittest.mock import Mock

import pytest
from PIL import Image

from jevduo.gui.compile import gui_action
from jevduo.gui.goal_candidates import goal_candidates, input_frontier
from jevduo.gui.grounding import ground_visual
from jevduo.memory.context import planning_context
from jevduo.memory.state import TaskMemory
from jevduo.models.ranking import Ranking
from jevduo.runtime.goal_stage import ObservationRequired, _document_mismatch, _navigation_key, _target_effect
from jevduo.verification.goal_verifier import verify_goal

from .conftest import gui_receipt, make_observation
from .test_goal_execution import _space
from .test_goal_memory import fact, prepare, session, source_memory
from .test_goal_stage import plan, requirements, result


def test_partial_sources_allow_delivery_without_claiming_source_or_unit_complete():
    memory = source_memory()
    prepare(memory, fact(value="Known first section", complete=False))
    item = planning_context(memory)["ready_work_items"][0]
    assert item["id"] == "R1:unit:1" and item["can_start_partial"]
    assert item["next_acquisition"]["missing_facts"][0]["key"] == "quota"
    memory.start({**plan(), "completion_criteria": "Known first section saved"}, 1)
    memory.apply(result(unit_met=False, delivery_state="saved", remaining=["Read remaining section"]),
                 {"A2": {}})
    assert memory.work_items[item["id"]]["delivery_state"] == "saved"
    assert memory.work_items[item["id"]]["status"] == "pending"
    assert memory.work_items["R1:prepare"]["status"] != "done"
    assert not memory.facts["quota"]["complete"]
    memory.start({**plan(), "purpose": "prepare"}, 2)
    assert memory.active_stage["fact_targets"][0]["key"] == "quota"


def test_explicit_dependencies_still_apply_and_preparation_stays_with_selected_unit():
    req = requirements()[0]
    req.update(units=["First section", "Second section"], unit_fact_keys=[["first"], ["second"]],
               facts_needed=[{"key": key, "description": key} for key in ("first", "second")])
    memory = TaskMemory("Edit", [req])
    second = memory.work_items["R1:unit:2"]
    second["depends_on"] = ["R1:unit:1"]
    assert second not in memory.ready_items()
    second["depends_on"] = []
    memory.start({**plan(), "work_item_id": second["id"], "purpose": "prepare"}, 0)
    assert memory.active_stage["prepares_unit"] == second["id"]
    assert memory.active_stage["fact_targets"][0]["key"] == "second"


FORM = '''<desktop-frame foreground_title="Research Portal - Google Chrome">
  <application active="true"><entry name="Address and search bar" editable="true"
    screencoord="(80,10)" size="(700,30)">file:///home/user/research-form.html?type=paper</entry>
    <section><label>Title *</label><entry editable="true" focused="false"
      screencoord="(200,100)" size="(200,30)"/>
      <label>Issue number</label><entry editable="true" focused="true"
      screencoord="(200,200)" size="(200,30)"/>
      <label>Title of series</label><entry editable="true"
      screencoord="(200,300)" size="(200,30)"/>
    </section></application></desktop-frame>'''


def test_text_binds_to_observed_label_instead_of_unrelated_focus():
    targets, space = _space(FORM, [{"label": "Article title", "target_hint": "Title",
                                   "text": "Observed source title", "input_mode": "replace"}])
    choices = space.target_heads["type_text"]
    assert len(choices) == 1
    target = next(t for t in targets if t.id == choices[0].target_id)
    assert target.name == "Title *" and not target.focused
    action = gui_action(choices[0], target)
    assert action.parameters["click"] == [300, 115]
    pending = {"operation": "type_text", "field_label": "Title *", "input_mode": "replace",
               "expected_text": "Observed source title"}
    wrong = FORM.replace('size="(200,30)"/>\n      <label>Title of series',
                         'size="(200,30)">Observed source title</entry>\n      <label>Title of series')
    assert _target_effect(pending, _space(wrong)[0]) == "not_met"


def test_unlabelled_or_ambiguous_fields_do_not_receive_all_supplied_text():
    inputs = [{"label": "Abstract", "target_hint": "Abstract", "text": "Known abstract",
               "input_mode": "replace"}]
    assert not _space(FORM, inputs)[1].target_heads["type_text"]
    duplicated = FORM.replace("Issue number", "Title *")
    inputs[0].update(target_hint="Title", label="Title")
    assert not _space(duplicated, inputs)[1].target_heads["type_text"]


def test_frontier_prioritizes_known_current_field_and_blocks_premature_next():
    tree = FORM.replace('</section>', '<push_button name="Next Step" screencoord="(200,400)" '
                        'size="(120,30)"/></section>')
    inputs = [{"label": "Article title", "target_hint": "Title", "text": "Known title",
               "input_mode": "replace"}]
    targets, space = _space(tree, inputs)
    frontier = input_frontier(space, targets, inputs)
    assert "Known title" in frontier["next_intent"]
    assert not frontier["missing_required_fields"]
    assert "done" not in space.controls
    assert not any('"Next Step"' in c.description for c in space.target_heads["click"])
    assert space.target_heads["type_text"] and space.target_heads["keypress"]


def test_frontier_reports_required_value_missing_without_inventing_text():
    inputs = [{"label": "Later attachment", "target_hint": "File chooser", "text": "/record.jpg",
               "input_mode": "replace"}]
    targets, space = _space(FORM, inputs)
    frontier = input_frontier(space, targets, inputs)
    assert frontier == {"next_intent": "", "missing_required_fields": ["Title *"],
                        "unbound_inputs": ["Later attachment"], "conflicting_fields": [],
                        "satisfied_inputs": []}
    assert not space.target_heads["type_text"]


def test_dom_declared_typing_operation_is_not_lost_for_an_unlisted_field_role():
    from dataclasses import replace

    targets, space = _space(FORM)
    target = replace(next(t for t in targets if t.name == "Title *"), role="date_editor",
                     operations=("click", "type_text"))
    space = goal_candidates(space, [target], FORM, [{"label": "Title", "target_hint": "Title",
                             "text": "Known title", "input_mode": "replace"}])
    assert space.target_heads["type_text"][0].target_id == target.id


def test_missing_current_page_input_returns_after_four_actions_without_an_api_verify(tmp_path):
    s = session()
    s.begin("Fill the form before uploading", requirements())
    s.goal.memory.start({**plan(), "inputs": [{"label": "Later attachment", "target_hint": "File chooser",
                           "text": "/record.jpg", "input_mode": "replace"}]}, 0)
    s.goal.memory.active_stage["actions"] = 4
    client = Mock()
    s.components.stage_verifier = lambda *a, **kw: verify_goal(client, *a, **kw)
    s.components.selector.rank = Mock()
    with pytest.raises(ObservationRequired, match="stage_verification_returned"):
        s.act(make_observation(tmp_path, 4, tree_xml=FORM))
    s.components.selector.rank.assert_not_called()
    client.json_request.assert_not_called()
    event = next(e for e in s.events if e.get("route") == "stage_verified")
    assert event["end_reason"] == "capability_missing: Later attachment"
    assert event["verification"]["stage_outcome"] == "uncertain"
    assert "Later attachment" in s.goal.memory.failures[-1]["error"]


def test_visible_input_hint_does_not_erase_the_delegated_goal(tmp_path):
    s = session()
    s.begin("Fill the title, then upload", requirements())
    s.goal.memory.start({**plan(), "goal": "Upload the attachment on the next page",
                         "inputs": [{"label": "Article title", "target_hint": "Title",
                                     "text": "Known title", "input_mode": "replace"}]}, 0)

    def rank(**kwargs):
        assert "Known title" in json.loads(kwargs["intent"])["next_action_hint"]
        assert kwargs["stage_goal"] == "Upload the attachment on the next page"
        winner = kwargs["action_space"].target_heads["type_text"][0]
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    action = s.act(make_observation(tmp_path, 0, tree_xml=FORM))
    assert action.parameters["text"] == "Known title"
    event = next(e for e in s.events if e.get("route") == "gui")
    assert event["plan"]["stage_goal"] == "Upload the attachment on the next page"
    assert event["plan"]["current_goal"] == event["plan"]["stage_goal"]
    assert "Known title" in event["input_frontier"]["next_intent"]


def test_launcher_name_cannot_be_typed_into_a_website_search_field():
    inputs = [{"label": "Target application", "target_hint": "Desktop search", "text": "Google Chrome",
               "scope": "desktop_launcher", "input_mode": "replace"}]
    tree = ('<application active="true" name="Google Chrome"><entry name="Search" '
            'editable="true" screencoord="(100,100)" size="(200,30)"/></application>')
    targets, space = _space(tree, inputs)
    assert not space.target_heads["type_text"]
    assert not input_frontier(space, targets, inputs)["unbound_inputs"]  # Not a current website input.
    assert _space(tree.replace('name="Google Chrome"', 'name="gnome-shell"'), inputs)[1].target_heads[
        "type_text"]


def test_conflicting_urls_are_not_offered_as_alternating_replacements():
    inputs = [{"label": f"Image {i}", "target_hint": "Address and search bar",
               "text": f"file:///image-{i}.png", "input_mode": "replace"} for i in range(2)]
    targets, space = _space(FORM, inputs)
    assert not space.target_heads["type_text"]
    assert input_frontier(space, targets, inputs)["conflicting_fields"] == ["Address and search bar"]


def test_typed_focused_url_requests_enter_instead_of_another_address_bar_click():
    tree = FORM.replace('name="Address and search bar"', 'name="Address and search bar" focused="true"')
    value = "file:///home/user/research-form.html?type=paper"
    targets, space = _space(tree, [{"label": "Page", "target_hint": "Address and search bar",
                                  "text": value, "input_mode": "replace"}])
    frontier = input_frontier(space, targets, [{"label": "Page", "target_hint": "Address and search bar",
                                              "text": value, "input_mode": "replace"}])
    assert "ENTER" in frontier["next_intent"]
    assert not any("Address and search bar" in c.description for c in space.target_heads["click"])
    assert any(c.id == "KEYPRESS_ENTER" for c in space.target_heads["keypress"])


def test_loaded_url_with_hidden_scheme_is_not_retyped_as_an_unfilled_field():
    value = "http://localhost:3000/questionnaire"
    tree = FORM.replace('file:///home/user/research-form.html?type=paper', 'localhost:3000/questionnaire')
    inputs = [{"label": "Page", "target_hint": "Address and search bar",
               "text": value, "input_mode": "replace"}]
    targets, space = _space(tree, inputs)
    assert not space.target_heads["type_text"]
    assert input_frontier(space, targets, inputs)["next_intent"] == ""
    assert "done" in space.controls


def test_blank_tab_navigation_does_not_offer_opening_another_blank_tab():
    from dataclasses import replace

    tree = FORM.replace('foreground_title="Research Portal - Google Chrome"',
                        'foreground_title="New Tab - Google Chrome"').replace(
        '</section>', '<push_button name="New Tab" screencoord="(300,20)" size="(30,30)"/></section>')
    targets, space = _space(tree)
    targets = [replace(t, scope="browser_chrome") for t in targets]
    space = goal_candidates(space, targets, tree, [{"label": "Page", "target_hint": "Address and search bar",
                                                  "text": "https://example.test", "input_mode": "replace"}])
    assert not any('"New Tab"' in c.description for c in space.target_heads["click"])
    assert space.target_heads["type_text"]


def test_navigation_checkpoint_survives_geometry_and_window_title_changes():
    from dataclasses import replace

    targets, space = _space(FORM)
    target = replace(targets[0], name="New Tab", scope="browser_chrome", x=100)
    candidate = next(c for c in space.candidates() if c.operation == "click")
    first = _navigation_key(candidate, target, "R1", ("PDF - Chrome", '["chrome"]'))
    second = _navigation_key(candidate, replace(target, x=500), "R1", ("New Tab - Chrome", '["chrome"]'))
    assert first == second


def test_website_identity_uses_observed_url_not_business_title():
    targets, _ = _space(FORM)
    assert not _document_mismatch("research-form.html", FORM, targets)
    assert _document_mismatch("other-form.html", FORM, targets)


@pytest.mark.parametrize("title,blocked", [
    ("Untitled - WPS Presentation", True), ("different.pptx - WPS Office", True),
    ("report.pptx - WPS Office", False), ("WPS Office", False),
])
def test_document_guard_distinguishes_positive_mismatch_from_unknown(title, blocked):
    tree = f'<desktop-frame foreground_title="{title}"/>'
    assert _document_mismatch("report.pptx", tree, []) is blocked


def test_visual_typing_cannot_bypass_field_binding(tmp_path):
    obs = make_observation(tmp_path, 0)
    proposed = [{"operation": "type_text", "target": name, "target_hint": "Title", "bounds": [1, 1, 20, 20],
                 "text": "Known title", "input_mode": "replace"} for name in ("Issue number", "Title")]
    client = Mock()
    client.json_request.return_value = {"candidates": proposed}
    candidates = ground_visual(client, "Edit", {"action_proposals": [
        {"operation": "type_text", "label": "Title", "target_hint": "Title", "text": "Known title",
         "input_mode": "replace"}]}, tmp_path / "step_00.png")
    assert len(candidates) == 1 and candidates[0].metadata["field_label"] == "Title"
    assert obs.step_idx == 0


@pytest.mark.parametrize("acquire", [False, True])
def test_unresolved_navigation_is_not_released_by_unrelated_materials(tmp_path, acquire):
    s = session()
    s.begin("Read source then edit", requirements())
    s.components.stage_verifier = Mock(return_value=result(
        stage_outcome="not_met", unit_met=False, delivery_state="none", evidence_refs=["A4"],
        materials=[{"source_ref": "source.pdf", "covered_parts": ["page 2"], "summary": "New content",
                    "evidence_refs": ["A4"]}] if acquire else []))
    chosen = []

    def rank(**kwargs):
        candidates = kwargs["action_space"].candidates()
        scrolling = [c for c in candidates if c.operation == "scroll" and "DOWN" in c.id]
        winner = scrolling[0] if scrolling else kwargs["action_space"].controls["return_to_planner"]
        chosen.append(winner.operation)
        return Ranking(winner, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    s.goal.memory.start(plan(), 0)
    s.act(make_observation(tmp_path, 0))
    for offset in range(1, 5):
        observation = make_observation(tmp_path, offset, receipt=gui_receipt())
        Image.new("RGB", (64, 64), "white" if offset % 2 else "black").save(observation.screenshot.path)
        if offset == 4:
            with pytest.raises(ObservationRequired):
                s.act(observation)
        else:
            s.act(observation)
    assert s.events[-2]["end_reason"] == "execution_checkpoint_stalled"
    assert s.components.stage_verifier.call_count == 1
    assert s.goal.memory.work_items["R1:unit:1"]["recovery_required"]
    s.goal.memory.start({**plan(), "method": "A reworded method",
                         "recovery_strategy": "alternate_interaction"}, 4)
    with pytest.raises(ObservationRequired):
        s.act(make_observation(tmp_path, 4))
    assert chosen[-1] == "return_to_planner"
    control = next(c for c in s.goal.memory.execution_constraints if c.get("kind") == "execution_checkpoint")
    assert control["blocked_families"] == ["scroll"]
