"""Generic binding/blocker regressions derived from recorded failures; synthetic fixtures."""

from dataclasses import replace

from tandem.gui.candidates import ActionCandidate
from tandem.gui.goal_candidates import _input_target, goal_candidates
from tandem.gui.interaction import GuiInteractionController
from tandem.memory.context import execution_context, planning_context
from tandem.memory.state import TaskMemory

from .conftest import build_space
from .test_goal_execution import _space
from .test_goal_progress import FORM
from .test_goal_stage import plan, requirements, result


def test_whole_form_button_cannot_steal_native_choice_fields():
    targets, _ = _space(FORM)
    base = targets[0]
    fields = [replace(base, id="country", role="combo_box", name="Country of Birth",
                      input_type="select-one", operations=("click",)),
              replace(base, id="level", role="combo_box", name="Education Level",
                      input_type="select-one", operations=("click",)),
              replace(base, id="form", role="push_button", operations=("click",), input_type="",
                      name="Questionnaire Country of Birth China Education Level Bachelor's Save Submit")]
    for label, text, expected in [("Country of Birth", "China", "country"),
                                  ("Education Level", "Bachelor's", "level")]:
        value = {"label": label, "target_hint": label, "text": text,
                 "kind": "choice", "input_mode": "replace"}
        assert _input_target(value, fields).id == expected


def setup_controller():
    req = requirements()[0]
    req.update(units=["First unit", "Second unit"], unit_fact_keys=[[], []])
    memory = TaskMemory("Complete two units", [req])
    memory.start(plan(), 0)
    controller = GuiInteractionController()
    targets, _ = _space(FORM)
    return controller, memory, targets


def test_rejected_zero_action_completion_survives_switching_work_units():
    controller, memory, targets = setup_controller()
    space = goal_candidates(build_space(targets), targets, FORM, [])
    controller.finalize(space, targets, memory.active_stage, memory, FORM, "S1:O0")
    assert "done" in space.controls
    rejected = result(stage_outcome="not_met", unit_met=False, delivery_state="none",
                      summary="The window was not maximized", evidence_refs=["S1:O0"])
    controller.finish(memory, rejected, [])
    memory.apply(rejected, {"S1:O0": {}})
    memory.start({**plan(), "work_item_id": "R1:unit:2"}, 0)
    space = goal_candidates(build_space(targets), targets, FORM, [])
    controller.finalize(space, targets, memory.active_stage, memory, FORM, "S2:O0")
    assert "done" not in space.controls
    assert memory.active_blocker["kind"] == "rejected_completion"
    assert execution_context(memory)["active_blocker"] == planning_context(memory)["active_blocker"]
    assert controller.control["failed_stages"] == 1


def test_new_related_control_value_releases_rejected_completion_with_evidence():
    controller, memory, targets = setup_controller()
    controller.finalize(goal_candidates(build_space(targets), targets, FORM, []),
                        targets, memory.active_stage, memory, FORM, "S1:O0")
    controller.finish(memory, result(stage_outcome="not_met", unit_met=False, delivery_state="none"), [])
    observed = [replace(t, value="New observed field value") for t in targets]
    space = goal_candidates(build_space(observed), observed, FORM, [])
    controller.finalize(space, observed, memory.active_stage, memory, FORM, "S1:O1")
    assert "done" in space.controls
    assert memory.active_blocker["status"] == "resolved"
    assert memory.active_blocker["resolution_evidence_refs"] == ["S1:O1"]


def test_short_goal_is_not_replaced_by_acceptance_criterion():
    controller, memory, targets = setup_controller()
    memory.active_stage.update(goal="Maximize the current window", completion_criteria="Window is maximized")
    context = controller.finalize(goal_candidates(build_space(targets), targets, FORM, []),
                                 targets, memory.active_stage, memory, FORM)
    view = controller.choice_context({**execution_context(memory), **context})
    assert view["objective"] == "Maximize the current window"
    assert view["exit_condition"] == "Window is maximized"


def test_unrelated_modal_names_share_one_owner_and_suspend_future_goal():
    controller, memory, _ = setup_controller()
    tree = '<desktop-frame foreground_class=\'["editor"]\' foreground_title="Working document"/>'
    controls = []
    for name in ["Default office software prompt", "Default software notice", "Editor prompt"]:
        space = goal_candidates(build_space([]), [], tree, [])
        space.target_heads["visual"] = [ActionCandidate("close", "click", "Close", source="visual_grounder",
            metadata={"visual_point": [30, 40], "blocking_overlay": True, "overlay_name": name,
                      "overlay_kind": "obstruction", "overlay_bounds": [10, 20, 100, 100]})]
        context = controller.finalize(space, [], memory.active_stage, memory, tree, "S1:O0")
        controls.append(controller.control)
        controller.control["ground_calls"] = 2
        view = controller.choice_context({**execution_context(memory), **context})
        assert "Restore input access" in view["objective"]
        assert view["inputs"] == [] and "Form completed" not in view["exit_condition"]
    assert controls[0] is controls[1] is controls[2]
    assert controls[-1]["ground_calls"] == 2
    controller.finalize(goal_candidates(build_space([]), [], tree, []), [],
                        memory.active_stage, memory, tree, "S1:O2")
    assert memory.active_blocker["status"] == "resolved"
    assert memory.active_blocker["resolution_evidence_refs"] == ["S1:O2"]


def test_required_task_dialog_is_not_dismissed_after_failure():
    controller, memory, _ = setup_controller()
    tree = '<desktop-frame foreground_class=\'["editor"]\' foreground_title="Save"/>'
    space = goal_candidates(build_space([]), [], tree, [])
    space.target_heads["visual"] = [ActionCandidate("save", "click", "Save", source="visual_grounder",
        metadata={"visual_point": [30, 40], "blocking_overlay": True, "overlay_name": "Save",
                  "overlay_kind": "task_dialog", "overlay_bounds": [10, 20, 100, 100]})]
    controller.finalize(space, [], memory.active_stage, memory, tree)
    controller.control["recovery_required"] = True
    context = controller.finalize(space, [], memory.active_stage, memory, tree)
    assert not context["scope_recovery"]
