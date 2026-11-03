"""Synthetic, task-independent execution-control regressions; not benchmark results."""

from dataclasses import replace

from tandem.gui.candidates import ActionCandidate
from tandem.gui.compile import gui_action
from tandem.gui.goal_candidates import goal_candidates
from tandem.gui.interaction import GuiInteractionController
from tandem.memory.context import execution_context, planning_context
from tandem.memory.state import TaskMemory
from tandem.planning.goal_planner import goal_error

from .conftest import build_space
from .test_goal_execution import _space
from .test_goal_progress import FORM
from .test_goal_stage import plan, requirements, result


def _setup(inputs=()):
    memory = TaskMemory("Edit and save", requirements())
    memory.start({**plan(), "inputs": list(inputs)}, 0)
    targets, _ = _space(FORM)
    controller = GuiInteractionController()
    space = build_space(targets)
    controller.finalize(space, targets, memory.active_stage, memory, FORM)
    return controller, memory, targets


def _pending(controller, operation="keypress", sequence=None):
    return {"operation": operation, "action": {"parameters": {"sequence": sequence}},
            "control_scope": controller.control["scope_key"], "control_state": controller.content_state}


def test_pixels_and_stage_rewording_do_not_release_a_failed_execution_path():
    controller, memory, targets = _setup()
    for _ in range(4):
        controller.receive(_pending(controller, sequence=[["tab"]]), "unknown", True, 0, targets=targets)
    assert controller.checkpoint_due
    assert controller.local_effect == "unknown"
    update = result(unit_met=False, delivery_state="none", stage_outcome="not_met")
    controller.finish(memory, update, [{"evidence_ref": "A2"}])
    memory.apply(update, {"A2": {}})
    assert memory.work_items["R1:unit:1"]["recovery_required"]
    assert "execution recovery" in goal_error(plan(), memory, True)
    revised = {**plan(), "method": "Different words", "recovery_strategy": "alternate_interaction"}
    assert not goal_error(revised, memory, True)
    memory.start(revised, 4)
    space = build_space(targets)
    space.target_heads["keypress"] += [ActionCandidate(
        "tab", "keypress", "Tab", metadata={"key_sequence": [["tab"]]})]
    controller.finalize(space, targets, memory.active_stage, memory, FORM)
    assert not any(c.id in {"tab", "KEYPRESS_TAB"} for c in space.candidates())
    assert any(c.operation == "click" for c in space.candidates())
    assert planning_context(memory)["execution_constraints"][0]["blocked_families"] == ["focus_traversal"]


def test_new_known_focus_positions_are_allowed_until_the_traversal_cycles():
    controller, memory, targets = _setup()
    base = next(t for t in targets if t.name == "Title *")
    fields = [replace(base, name=f"Field {i}", semantic_key=f"field-{i}", focused=False)
              for i in range(8)]
    controller.finalize(build_space(fields), fields, memory.active_stage, memory, FORM)
    for index in range(8):
        observed = [replace(t, focused=i == index) for i, t in enumerate(fields)]
        controller.receive(_pending(controller, sequence=[["tab"]]), "unknown", True, 0, targets=observed)
        assert not controller.checkpoint_due
    observed = [replace(t, focused=i == 0) for i, t in enumerate(fields)]
    controller.receive(_pending(controller, sequence=[["tab"]]), "unknown", True, 0, targets=observed)
    assert controller.checkpoint_due


def test_new_observed_content_allows_continuous_navigation_without_semantic_verify():
    controller, memory, targets = _setup()
    base = next(t for t in targets if t.name == "Title *")
    for index in range(12):
        observed = [replace(base, name=f"Visible content section {index}")]
        controller.receive(_pending(controller, operation="scroll"), "unknown", True, 0, targets=observed)
        controller.finalize(build_space(observed), observed, memory.active_stage, memory, FORM)
        assert controller.local_effect == "controls_changed"
        assert not controller.checkpoint_due and not controller.control["blocked_families"]


def test_confirmed_field_effect_is_progress_but_does_not_erase_another_failed_path():
    controller, _, targets = _setup()
    controller.control.update(blocked_families=["scroll"], recovery_required=True)
    pending = _pending(controller, operation="type_text")
    controller.receive(pending, "unknown", True, 0, targets=targets)
    assert controller.control["blocked_families"] == ["scroll"]
    controller.receive(pending, "met", True, 0, targets=targets)
    assert controller.local_effect == "confirmed_input"
    assert controller.control["without_effect"] == 0
    assert controller.control["blocked_families"] == ["scroll"]
    assert controller.control["recovery_required"]


def test_final_scope_rejects_a_commit_added_for_a_background_field():
    value = {"label": "Command", "target_hint": "Title", "text": "Example(1)",
             "input_mode": "replace", "kind": "command", "commit": "enter"}
    controller, memory, targets = _setup([value])
    # Establish a real command-input dispatch before the new foreground modal appears.
    space = build_space(targets)
    space.target_heads["type_text"] = [ActionCandidate(
        "command", "type_text", "Type command", target_id=next(t.id for t in targets if t.name == "Title *"),
        metadata={"text": value["text"], "target_hint": "Title", "input_mode": "replace"})]
    controller.constrain(space, targets, memory.active_stage, memory)
    typed = space.target_heads["type_text"][0]
    controller.receive({"operation": "type_text",
                        "interaction_binding": typed.metadata["interaction_binding"]},
                       "unknown", True, 0)
    modal = ('<desktop-frame foreground_title="Editor"><application><dialog name="Confirmation" '
             'modal="true" showing="true" active="true" screencoord="(400,400)" size="(300,200)"/>'
             '</application></desktop-frame>')
    memory.active_stage.update(target_document="expected-document.pptx", document_recovery=True)
    space = goal_candidates(build_space(targets), targets, modal, [])
    space.target_heads["visual"] = [ActionCandidate(
        "close", "click", "Close the visible confirmation", source="visual_grounder",
        metadata={"visual_point": [650, 420], "blocking_overlay": True,
                  "overlay_name": "Confirmation", "overlay_bounds": [400, 400, 300, 200]})]
    context = controller.finalize(space, targets, memory.active_stage, memory, modal)
    assert not any(c.metadata.get("commit_action") for c in space.candidates())
    assert "Confirmation" in context["next_intent"]
    assert any(c.id == "close" for c in space.candidates())
    assert gui_action(next(c for c in space.candidates() if c.id == "close"), None).action_type == "click"


def test_non_delivery_source_facts_do_not_clear_an_active_modal_failure():
    controller, memory, targets = _setup()
    controller.control.update(global_scope=True, attempts={"focus_traversal": 5})
    update = result(stage_outcome="met", unit_met=False, delivery_state="none", progress_kind="source",
                    facts=[{"key": "source.note", "value": "An unrelated observed note",
                            "status": "confirmed",
                            "scope": "task", "source_ref": "source.txt", "evidence_refs": ["A2"]}])
    controller.finish(memory, update, [{"evidence_ref": "A2"}])
    memory.apply(update, {"A2": {}})
    assert controller.control["blocked_families"] == ["focus_traversal"]
    assert controller.control["recovery_required"]


def test_a_physical_modal_failure_survives_switching_work_units():
    req = requirements()[0]
    req.update(units=["First section", "Second section"], unit_fact_keys=[[], []])
    memory = TaskMemory("Edit two sections", [req])
    controller = GuiInteractionController()
    modal = '<desktop-frame foreground_title="Editor" foreground_class=\'["editor"]\'/>'

    def space():
        value = goal_candidates(build_space([]), [], modal, [])
        value.target_heads["visual"] = [ActionCandidate(
            "visible-button", "click", "Visible button", source="visual_grounder",
            metadata={"visual_point": [30, 40], "blocking_overlay": True,
                      "overlay_name": "Confirmation", "overlay_bounds": [10, 20, 100, 100]})]
        return value

    memory.start(plan(), 0)
    controller.finalize(space(), [], memory.active_stage, memory, modal)
    for _ in range(4):
        controller.receive(_pending(controller, sequence=[["tab"]]), "unknown", True, 0)
    update = result(stage_outcome="not_met", unit_met=False, delivery_state="none")
    controller.finish(memory, update, [{"evidence_ref": "A2"}])
    memory.apply(update, {"A2": {}})
    memory.start({**plan(), "work_item_id": "R1:unit:2"}, 4)
    choices = space()
    controller.finalize(choices, [], memory.active_stage, memory, modal)
    assert controller.control["global_scope"]
    assert not any(c.id == "KEYPRESS_TAB" for c in choices.candidates())
    assert execution_context(memory)["execution_constraints"][0]["blocked_families"] == ["focus_traversal"]


def test_confirmed_required_source_content_can_release_nonmodal_browsing():
    req = requirements()[0]
    req.update(facts_needed=[{"key": "source.required", "description": "Required source section"}],
               unit_fact_keys=[["source.required"]])
    memory = TaskMemory("Read required source", [req])
    memory.start({**plan(), "purpose": "prepare"}, 0)
    controller = GuiInteractionController()
    targets, _ = _space(FORM)
    controller.finalize(build_space(targets), targets, memory.active_stage, memory, FORM)
    controller.control.update(blocked_families=["scroll"], attempts={"scroll": 4}, recovery_required=True)
    update = result(unit_met=False, delivery_state="none", progress_kind="source", facts=[{
        "key": "source.required", "value": "Confirmed requested section", "status": "confirmed",
        "scope": "task", "source_ref": "source.txt", "evidence_refs": ["A2"]}])
    controller.finish(memory, update, [{"evidence_ref": "A2"}])
    assert not controller.control["blocked_families"]
    assert not controller.control["recovery_required"]
