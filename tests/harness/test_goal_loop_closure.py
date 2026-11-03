"""Task-independent regressions distilled from real GUI failures; not benchmark acceptance."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tandem.gui.goal_candidates import _input_target, goal_candidates
from tandem.gui.interaction import GuiInteractionController
from tandem.gui.semantics import enrich_targets
from tandem.memory.state import TaskMemory

from .conftest import build_space
from .test_goal_execution import _space
from .test_goal_progress import FORM
from .test_goal_stage import plan, requirements, result


def _field():
    targets, _ = _space(FORM)
    return next(t for t in targets if t.name == "Title *")


def test_specific_hint_does_not_bind_a_context_word_to_an_unrelated_select():
    field = replace(_field(), name="Semester", role="combo_box", input_type="select-one")
    value = {"label": "Semester 2 subclass with afternoon meeting", "target_hint": "Subclass B",
             "text": "Subclass B", "input_mode": "replace", "kind": "choice"}
    assert _input_target(value, [field]) is None


def test_full_iso_date_never_binds_to_one_date_segment():
    field = replace(_field(), name="Day", role="spin_button", description="dd")
    value = {"label": "Visit date", "target_hint": "Visit date, focused Day segment",
             "text": "2026-03-25", "input_mode": "replace"}
    assert _input_target(value, [field]) is None


def test_named_clickable_choice_is_not_translated_into_dropdown_typing():
    field = replace(_field(), id="card", name="Subclass B (Class 123)", role="push_button")
    value = {"label": "Afternoon subclass", "target_hint": "Subclass B", "text": "Subclass B",
             "kind": "choice", "input_mode": "replace"}
    space = goal_candidates(build_space([field]), [field], FORM, [value])
    assert any(c.operation == "click" and c.target_id == "card" and c.source == "goal_input"
               for c in space.candidates())
    assert not space.target_heads["type_text"]


@pytest.mark.parametrize("suffix", ["", " Visit date"])
def test_a11y_date_segments_form_one_typed_input_without_cdp(suffix):
    base = _field()
    fields = [replace(base, id=f"part-{i}", name=name + suffix, role="spin_button", value=value,
                      x=100 + i * 40, y=200, width=30, height=20, nearby_text="Visit date")
              for i, (name, value) in enumerate([("Day", "25"), ("Month", "3"), ("Year", "2026")])]
    targets = enrich_targets(fields, None, (800, 600))
    date = next(t for t in targets if t.input_type == "date")
    assert date.name == "Visit date" and date.value == "2026-03-25"
    assert date.date_order == ("day", "month", "year")


def _controller():
    memory = TaskMemory("Edit the current form", requirements())
    memory.start(plan(), 0)
    targets, _ = _space(FORM)
    controller = GuiInteractionController()
    controller.finalize(build_space(targets), targets, memory.active_stage, memory, FORM)
    return controller, memory, targets


def test_ui_change_does_not_erase_a_failed_path():
    controller, _, targets = _controller()
    controller.control.update(blocked_families=["activation_keys"], recovery_required=True)
    pending = {"operation": "scroll", "action": {"parameters": {}},
               "control_scope": controller.control["scope_key"], "control_state": controller.content_state}
    controller.receive(pending, "unknown", True, 0,
                       targets=[replace(targets[0], name="Another visible section")])
    assert controller.control["blocked_families"] == ["activation_keys"]
    assert controller.control["recovery_required"]


def test_changed_but_incorrect_input_is_not_treated_as_new_content_progress():
    controller, _, targets = _controller()
    controller.control.update(ground_calls=2)
    pending = {"operation": "type_text", "action": {"parameters": {}},
               "control_scope": controller.control["scope_key"], "control_state": controller.content_state}
    controller.receive(pending, "not_met", True, 0,
                       targets=[replace(targets[0], value="Another wrong value")])
    assert controller.local_effect != "controls_changed"
    assert controller.control["without_effect"] == 1 and controller.control["ground_calls"] == 2


def test_returning_to_seen_controls_is_not_endless_new_content():
    controller, memory, targets = _controller()
    base = targets[0]
    for index in range(8):
        pending = {"operation": "scroll", "action": {"parameters": {}},
                   "control_scope": controller.control["scope_key"],
                   "control_state": controller.content_state}
        observed = [replace(base, name="Menu open" if index % 2 else "Menu closed")]
        controller.receive(pending, "unknown", True, 0, targets=observed)
        controller.finalize(build_space(observed), observed, memory.active_stage, memory, FORM)
    assert controller.checkpoint_due


def test_visual_dialog_name_variants_share_failure_and_grounding_budget():
    from tandem.gui.candidates import ActionCandidate

    controller, memory, _ = _controller()
    tree = '<desktop-frame foreground_class=\'["editorapp"]\' foreground_title="Document"/>'
    controls = []
    for name in ["EditorApp Replace dialog", "Replace", "Replace dialog"]:
        space = goal_candidates(build_space([]), [], tree, [])
        space.target_heads["visual"] = [ActionCandidate("close", "click", "Close", source="visual_grounder",
            metadata={"visual_point": [30, 40], "blocking_overlay": True,
                      "overlay_name": name, "overlay_bounds": [10, 20, 100, 100]})]
        controller.finalize(space, [], memory.active_stage, memory, tree)
        controls.append(controller.control)
        controller.control.update(blocked_families=["activation_keys"], ground_calls=2)
    assert controls[0] is controls[1] is controls[2]
    assert controls[-1]["ground_calls"] == 2


def test_registered_source_key_does_not_promote_temporary_ui_state():
    req = requirements()[0]
    req.update(facts_needed=[{"key": "profile", "description": "Source profile"}],
               unit_fact_keys=[["profile"]])
    memory = TaskMemory("Fill a form from the profile", [req])
    memory.start(plan(), 0)
    update = result(stage_outcome="not_met", unit_met=False, delivery_state="none", facts=[{
        "key": "profile", "value": "The current input is still empty", "status": "confirmed",
        "scope": "stage", "source_ref": "Current form", "evidence_refs": ["A2"]}])
    assert not memory.apply(update, {"A2": {}})
    assert "profile" not in memory.facts
    assert memory.last_rejections[0]["reason"] == "source_key_requires_source_fact"


def test_task_scope_alone_does_not_make_a_form_snapshot_source_acquisition():
    req = requirements()[0]
    req.update(facts_needed=[{"key": "profile", "description": "Source profile"}],
               unit_fact_keys=[["profile"]])
    memory = TaskMemory("Fill a form", [req])
    memory.start(plan(), 0)
    update = result(stage_outcome="not_met", unit_met=False, delivery_state="none", facts=[{
        "key": "profile", "value": "Current form is still wrong", "status": "confirmed",
        "scope": "task", "source_ref": "Form", "evidence_refs": ["A2"]}])
    assert not memory.apply(update, {"A2": {}})
    assert "profile" not in memory.facts
    assert memory.last_rejections[0]["reason"] == "source_update_requires_acquisition_stage"


def test_business_dialog_preserves_its_local_objective():
    controller, memory, targets = _controller()
    memory.active_stage["goal"] = "Select the requested afternoon class"
    memory.active_stage["completion_criteria"] = "Select the requested afternoon class"
    modal = ('<desktop-frame foreground_class=\'["browser"]\' foreground_title="Courses">'
             '<application><dialog name="Course details" modal="true" active="true" showing="true" '
             'screencoord="(0,0)" size="(800,600)"/></application></desktop-frame>')
    space = goal_candidates(build_space(targets), targets, modal, [])
    context = controller.finalize(space, targets, memory.active_stage, memory, modal)
    assert "dismiss" not in context["next_intent"].lower()
    assert "afternoon class" in context["next_intent"]


def test_selector_recovery_context_does_not_keep_the_original_navigation_method():
    controller, memory, _ = _controller()
    context = {"goal": "Edit a chart", "completion_criteria": "Chart saved", "inputs": [],
               "method": "Switch applications and edit the chart", "guidance": "Read another page",
               "next_intent": "Close the blocking confirmation", "blocking_overlay": "Confirmation",
               "scope_recovery": True, "constraints": [], "unbound_inputs": [], "execution_control": {}}
    compact = controller.choice_context(context)
    assert compact["objective"] == "Close the blocking confirmation"
    assert not compact["method"] and not compact["guidance"]
    assert compact["delegated_goal"] == context["goal"]
    assert "Switch applications" not in json.dumps(compact)


def test_unresolved_scope_recovery_omits_future_edit_values_and_exit_conditions():
    controller, _, _ = _controller()
    context = {"goal": "Edit chart", "completion_criteria": "Chart has future metrics and is saved",
               "inputs": [{"label": "Later title", "text": "Next quarter", "input_mode": "replace"}],
               "next_intent": "Restore input access by resolving the current dialog",
               "blocking_overlay": "Confirmation", "scope_recovery": True, "unbound_inputs": ["Later title"]}
    compact = controller.choice_context(context)
    assert compact["inputs"] == [] and compact["unbound_inputs"] == []
    assert "future metrics" not in json.dumps(compact) and "Next quarter" not in json.dumps(compact)


def test_verify_navigation_cannot_reclaim_a_historical_save(tmp_path):
    from PIL import Image

    from tandem.verification.goal_verifier import verify_goal

    _, memory, _ = _controller()
    path = tmp_path / "screen.png"
    Image.new("RGB", (16, 16)).save(path)
    response = result(stage_outcome="not_met", unit_met=False, delivery_state="saved",
                      progress_kind="navigation", observed_errors=[], milestones=[])
    response["unit_assessment"] = {"work_item_id": "R1:unit:1", "status": "not_met",
                                   "remaining": [], "evidence_refs": ["A2"]}
    client = Mock()
    client.config.stage_verify_max_chars = 48000

    def request(messages, validate, **kw):
        assert validate(response)
        return response

    client.json_request.side_effect = request
    output = verify_goal(client, "Edit", path, memory=memory, history=[],
                         end_reason="stage_done_requested", evidence={"A2": {"screenshot": str(path)}})
    assert output["delivery_state"] == "none" and not output["unit_met"]
    assert output["historical_delivery_claim"] == "saved"


def test_invalid_verify_output_is_uncertain_not_task_success_or_api_failure(tmp_path):
    from PIL import Image

    from tandem.models.slow import ModelResponseError
    from tandem.verification.goal_verifier import verify_goal

    _, memory, _ = _controller()
    path = tmp_path / "screen.png"
    Image.new("RGB", (16, 16)).save(path)
    client = Mock()
    client.config.stage_verify_max_chars = 48000
    client.json_request.side_effect = ModelResponseError("invalid structured response")
    output = verify_goal(client, "Edit", path, memory=memory, history=[],
                         end_reason="stage_done_requested", evidence={"A2": {"screenshot": str(path)}})
    assert output["stage_outcome"] == "uncertain" and not output["unit_met"]
    assert output["failure_kind"] == "verification_output_invalid"
    assert not memory.apply(output, {"A2": {}})


def test_missing_current_target_triggers_grounding_despite_other_controls(tmp_path):
    import pytest

    from tandem.config import SessionConfig
    from tandem.gui.candidates import ActionCandidate
    from tandem.models.ranking import Ranking
    from tandem.runtime.goal_stage import ObservationRequired
    from tandem.runtime.session import TandemSession

    from .conftest import make_components, make_observation

    components = make_components(lambda *a, **kw: plan(), lambda *a, **kw: result(), Mock())
    components.memory_factory = TaskMemory
    components.grounder = Mock(return_value=[ActionCandidate(
        "located", "click", "Visible requested option", source="visual_grounder",
        metadata={"visual_point": [20, 40]})])
    components.selector.rank = Mock(return_value=Ranking(
        components.grounder.return_value[0], [], [], 0, 0, 1, True))
    session = TandemSession(components, config=SessionConfig(execution_mode="goal"))
    session.begin("Choose a visible option", requirements())
    session.goal.memory.start({**plan(), "inputs": [{"label": "Requested option", "text": "Option B",
         "target_hint": "Requested option", "kind": "choice", "input_mode": "replace"}]}, 0)
    observation = make_observation(tmp_path, 0)
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        session.act(observation)
    assert session.act(observation).parameters == {"x": 20, "y": 40}
    components.grounder.assert_called_once()


def test_invalid_planner_output_is_an_agent_stop_not_an_external_api_failure(tmp_path):
    from tandem.config import SessionConfig
    from tandem.models.slow import ModelResponseError
    from tandem.runtime.session import TandemSession

    from .conftest import make_components, make_observation

    planner = Mock(side_effect=ModelResponseError("Unrepairable structured output"))
    components = make_components(planner, Mock(), Mock())
    components.memory_factory = TaskMemory
    session = TandemSession(components, config=SessionConfig(execution_mode="goal"))
    session.begin("Edit", requirements())
    action = session.act(make_observation(tmp_path, 0))
    assert action.kind == "fail" and action.reason == "planner_output_invalid"
    assert session.events[-1]["route"] == "planner_output_invalid"
