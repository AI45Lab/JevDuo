"""Synthetic task-memory and local recovery regressions, not benchmark results."""

from unittest.mock import Mock, patch

import pytest
from PIL import Image

from jevduo.config import SessionConfig
from jevduo.gui.candidates import ActionCandidate, ActionCandidateBuilder
from jevduo.gui.goal_candidates import foreground_tree, goal_candidates, scroll_point
from jevduo.gui.targets import A11yTargetBuilder
from jevduo.memory.context import planning_context
from jevduo.memory.state import TaskMemory
from jevduo.models.ranking import Ranking
from jevduo.protocol.models import GuiAction
from jevduo.runtime.goal_stage import ObservationRequired
from jevduo.runtime.session import JevDuoSession
from jevduo_eval.osworld import gui_code

from .conftest import FixedSelector, gui_receipt, make_components, make_observation
from .test_goal_stage import plan, requirements, result


def fact(key="quota", value="1400000 USD", **fields):
    return {"key": key, "value": value, "status": "confirmed", "scope": "task",
            "source_ref": "source.pdf page 1", "evidence_refs": ["A2"], **fields}


def source_memory():
    req = requirements()[0]
    req.update(facts_needed=[{"key": "quota", "description": "Annual quota"}],
               unit_fact_keys=[["quota"]])
    return TaskMemory("Edit", [req])


def prepare(memory, update):
    memory.start({**plan(), "work_item_id": "R1:prepare", "purpose": "prepare"}, 0)
    memory.apply(result(stage_outcome="not_met", unit_met=False, delivery_state="none",
                        facts=[update]), {"A2": {}})


def test_registered_source_is_reusable_when_marked_as_source_content():
    memory = source_memory()
    prepare(memory, fact())
    assert memory.facts["quota"]["scope"] == "task"
    assert memory.work_items["R1:prepare"]["status"] == "done"
    assert memory.ready_items()[0]["id"] == "R1:unit:1"


def test_uncertain_observation_and_unexplained_change_preserve_source():
    memory = source_memory()
    prepare(memory, fact())
    for update in [fact(value="Not currently visible", status="uncertain"), fact(value="Guess")]:
        memory.start({**plan(), "purpose": "prepare"}, 1)
        memory.apply(result(unit_met=False, delivery_state="none", facts=[update]), {"A2": {}})
        assert memory.facts["quota"]["value"] == "1400000 USD"
        assert memory.last_rejections[0]["reason"] == "confirmed_source_preserved"
    memory.start({**plan(), "purpose": "prepare"}, 2)
    memory.apply(result(unit_met=False, delivery_state="none",
                        facts=[fact(value="1500000 USD", correction_reason="Source correction observed")]),
                 {"A2": {}})
    assert memory.facts["quota"]["value"] == "1500000 USD"


def test_independent_unit_unlocks_before_all_source_reading_finishes():
    req = requirements()[0]
    req.update(units=["Save quota page", "Save sales page"], unit_fact_keys=[["quota"], ["sales"]],
               facts_needed=[{"key": "quota", "description": "Quota"},
                             {"key": "sales", "description": "Sales"}])
    memory = TaskMemory("Edit", [req])
    prepare(memory, fact())
    ready = {w["id"] for w in memory.ready_items()}
    assert "R1:unit:1" in ready and "R1:unit:2" in ready
    assert not memory._facts_ready(memory.work_items["R1:unit:2"])
    assert memory.work_items["R1:prepare"]["status"] != "done"


def test_material_only_update_does_not_reset_delivery_stall():
    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    memory.apply(result(stage_outcome="not_met", unit_met=False, delivery_state="none",
                        materials=[{"source_ref": "frame.png", "covered_parts": ["another screenshot"],
                                    "summary": "No new required facts", "evidence_refs": ["A2"]}]),
                 {"A2": {}})
    assert memory.stats["source_parts"] == 1
    assert memory.stats["stages_without_progress"] == 1


def test_observed_missing_module_is_not_lost_when_recent_failure_window_rotates():
    memory = TaskMemory("Edit", requirements())
    memory.failures = [{"error": "ModuleNotFoundError: No module named 'pptx'"}]
    memory.failures.extend({"error": "A later GUI failure"} for _ in range(15))
    context = planning_context(memory)
    assert context["observed_missing_modules"] == ["pptx"]
    assert all("pptx" not in f["error"] for f in context["failures"])


def test_unit_can_reuse_source_definition_from_another_requirement():
    req = requirements()[0]
    req.update(facts_needed=[{"key": "quota", "description": "Quota"}], unit_fact_keys=[["quota"]])
    other = {**req, "id": "R2", "facts_needed": []}
    memory = TaskMemory("Edit", [req, other])
    assert memory.work_items["R2:unit:1"]["facts_needed"] == req["facts_needed"]
    assert not memory._facts_ready(memory.work_items["R2:unit:1"])
    prepare(memory, fact())
    assert "R2:unit:1" in {w["id"] for w in memory.ready_items()}


def test_constraint_defined_source_gets_an_executable_preparation_item():
    delivery = requirements()[0]
    delivery["unit_fact_keys"] = [["quota"]]
    constraint = {**delivery, "id": "R2", "kind": "constraint", "units": [], "unit_fact_keys": [],
                  "facts_needed": [{"key": "quota", "description": "Quota from the source"}]}
    memory = TaskMemory("Edit", [delivery, constraint])
    assert {w["id"] for w in memory.ready_items()} == {"R1:prepare", "R1:unit:1"}
    assert not memory._facts_ready(memory.work_items["R1:unit:1"])
    assert memory.work_items["R1:prepare"]["facts_needed"] == constraint["facts_needed"]
    prepare(memory, fact())
    assert [w["id"] for w in memory.ready_items()] == ["R1:unit:1"]


def test_source_can_cite_registered_history_but_completion_needs_current_evidence():
    memory = source_memory()
    prepare(memory, fact())
    memory.start(plan(), 1)
    memory.apply(result(evidence_refs=["A2"]), {"A3": {}})
    assert memory.work_items["R1:unit:1"]["status"] != "done"
    memory.start(plan(), 2)
    memory.apply(result(evidence_refs=["A2", "A3"]), {"A3": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "done"


def test_named_images_and_native_file_targets_survive_bounded_selection():
    chrome = '<application active="true" name="Chrome"><frame name="Browser">'
    chrome += ''.join(f'<push_button name="Toolbar {i}" screencoord="(1,40)" size="(20,20)"/>'
                      for i in range(150))
    chrome += '<image name="Fractal tutorial" screencoord="(500,500)" size="(200,120)"/>'
    chrome += '</frame></application>'
    targets = A11yTargetBuilder().build(chrome, "s", 8, context="Open Fractal tutorial", filter_occluded=True)
    space = ActionCandidateBuilder().build(targets, context="Open Fractal tutorial")
    assert any("Fractal tutorial" in c.description for c in space.target_heads["click"])
    files = '''<application name="org.gnome.Nautilus"><frame active="true" name="Home">
      <list-item focusable="true"><label name="Desktop" screencoord="(100,200)" size="(100,30)"/></list-item>
      <canvas name="documents" screencoord="(300,200)" size="(80,80)"/>
    </frame></application>'''
    targets = A11yTargetBuilder().build(files, "s", filter_occluded=True)
    assert {t.name for t in targets} == {"Desktop", "documents"}
    space = goal_candidates(ActionCandidateBuilder().build(targets), targets, files, [])
    assert space.target_heads["double_click"]


def test_scroll_point_is_inside_the_small_foreground_window():
    tree = '<frame active="true" screencoord="(94,54)" size="(652,652)"/>'
    x, y = scroll_point(tree, (1920, 1080))
    assert 94 < x < 746 and 54 < y < 706


def test_x11_foreground_identity_excludes_stale_background_active_app():
    tree = '''<desktop-frame foreground_class='["wpp", "Wpp"]' foreground_title="System Check">
      <application name="Google Chrome"><frame name="Mail" active="true"/></application>
      <application name="wpp"/>
      <application name="gnome-shell"><menu name="WPS Presentation"/></application>
    </desktop-frame>'''
    filtered = foreground_tree(tree)
    assert 'name="Google Chrome"' not in filtered
    assert 'name="wpp"' in filtered and 'name="gnome-shell"' in filtered


def session():
    components = make_components(lambda *a, **kw: plan(), lambda *a, **kw: None, FixedSelector())
    components.memory_factory = TaskMemory
    components.stage_verifier = lambda *a, **kw: result(unit_met=False, delivery_state="none")
    return JevDuoSession(components, config=SessionConfig(execution_mode="goal"))


def test_no_effect_forces_grounding_and_masks_the_failed_candidate(tmp_path):
    s = session()
    s.begin("Edit", requirements())
    grounded = ActionCandidate("visual", "click", "Observed alternate", source="visual_grounder",
                               metadata={"visual_point": [500, 500]})
    s.components.grounder = Mock(return_value=[grounded])
    obs = make_observation(tmp_path, 0)
    with pytest.raises(ObservationRequired):
        s.act(obs)
    s.act(obs)
    s.act(make_observation(tmp_path, 1, receipt=gui_receipt()))
    failed = s.goal.pending["candidate_key"]
    obs2 = make_observation(tmp_path, 2, receipt=gui_receipt())
    with pytest.raises(ObservationRequired, match="local_visual_recovery_returned"):
        s.act(obs2)
    s.components.grounder.assert_called_once()

    def rank(**kwargs):
        from jevduo.runtime.goal_stage import _action_key

        candidates = kwargs["action_space"].candidates()
        assert grounded in candidates
        assert not any(_action_key(c, kwargs["targets"]) == failed for c in candidates)
        return Ranking(grounded, [], [], 0, 0, 1, True)

    s.components.selector.rank = rank
    assert s.act(obs2).action_type == "click"


def test_unrelated_animation_does_not_reset_semantic_no_effect(tmp_path):
    s = session()
    s.begin("Edit", requirements())
    obs = make_observation(tmp_path, 0)
    with pytest.raises(ObservationRequired):
        s.act(obs)
    s.act(obs)
    for step, color in [(1, "black"), (2, "white")]:
        obs = make_observation(tmp_path, step, receipt=gui_receipt())
        Image.new("RGB", (64, 64), color).save(obs.screenshot.path)
        assert s.act(obs).kind == "gui"
    assert s.goal.no_effect == 2
    assert all(h["observed_change"] and not h["relevant_change"] for h in s.goal.history)


def test_unicode_codegen_uses_utf8_clipboard_not_ascii_typing():
    text = "Organization Change — 组织调整"
    action = GuiAction("type", {"text": text, "input_mode": "replace"})
    gui = Mock()
    with patch("subprocess.run") as run, patch("time.sleep"):
        exec(gui_code(action), {"pyautogui": gui})
    assert run.call_args.kwargs["input"] == text.encode("utf-8")
    assert "UTF8_STRING" in run.call_args.args[0]
    gui.write.assert_not_called()
    gui.hotkey.assert_any_call("ctrl", "v")


def test_partial_collection_extends_without_overwriting_or_claiming_complete():
    memory = source_memory()
    prepare(memory, fact(value="Partial options", entries={"1": "Humanities"}, complete=False))
    assert not memory._facts_ready(memory.work_items["R1:unit:1"])
    prepare(memory, fact(value="Full options", entries={"2": "Social Sciences"}, complete=True))
    assert memory.facts["quota"]["entries"] == {"1": "Humanities", "2": "Social Sciences"}
    assert memory.work_items["R1:prepare"]["status"] == "done"
    memory.start(plan(), 2)
    memory.apply(result(unit_met=False, facts=[fact(value="Changed", entries={"1": "Wrong"})]), {"A2": {}})
    assert memory.facts["quota"]["entries"]["1"] == "Humanities"
    assert memory.last_rejections[0]["reason"] == "source_update_requires_acquisition_stage"


def test_preparation_targets_one_unit_not_all_materials():
    req = requirements()[0]
    req.update(units=["Save quota page", "Save sales page"], unit_fact_keys=[["quota"], ["sales"]],
               facts_needed=[{"key": "quota", "description": "Quota"},
                             {"key": "sales", "description": "Sales"}])
    memory = TaskMemory("Edit", [req])
    memory.start({**plan(), "work_item_id": "R1:prepare", "purpose": "prepare"}, 0)
    assert memory.active_stage["prepares_unit"] == "R1:unit:1"
    assert memory.active_stage["fact_targets"] == [{"key": "quota", "description": "Quota"}]


def test_snapshot_ids_and_ancestry_are_not_a_new_ui_state():
    from dataclasses import replace

    from jevduo.runtime.goal_stage import _state_key

    target = A11yTargetBuilder().build('''<frame active="true"><push-button name="Open"
        screencoord="(100,100)" size="(50,30)"/></frame>''', "one")[0]
    changed_path = replace(target, snapshot_id="two", semantic_key="changed tab title|Open")
    assert _state_key([target]) == _state_key([changed_path])


def test_action_buttons_are_not_displaced_by_file_metadata_cells():
    tree = '<application name="viewer"><frame active="true" name="Open Image">'
    tree += ''.join(f'<table-cell name="Image {n}" screencoord="(300,{n * 10})" size="(100,10)"/>'
                    for n in range(45))
    tree += '<push-button name="Open" screencoord="(900,100)" size="(80,30)"/></frame></application>'
    targets = A11yTargetBuilder().build(tree, "s", 128, context="Open Image")
    space = ActionCandidateBuilder().build(targets, context="Open Image")
    button = next(t for t in targets if t.name == "Open")
    assert any(c.target_id == button.id for c in space.target_heads["click"])
