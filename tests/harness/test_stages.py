"""Stage target rebinding and bounded recovery after unbound proposals."""

from pathlib import Path
from types import SimpleNamespace

from jevduo.config import SessionConfig
from jevduo.gui.candidates import ActionCandidateBuilder
from jevduo.gui.proposals import focused_candidates
from jevduo.gui.semantics import enrich_targets
from jevduo.gui.targets import A11yTargetBuilder
from jevduo.protocol.models import GuiAction
from jevduo.runtime.session import JevDuoSession
from jevduo.runtime.stages import StageExecution

from .conftest import FixedSelector, ScriptedPlanner, ScriptedVerifier, make_components, make_observation

FILES_XML = """<application name="org.gnome.Nautilus" active="true"
 xmlns:cp="https://accessibility.ubuntu.example.org/ns/component">
 <frame name="Home" active="true">
  <menu name="File" cp:screencoord="(130,100)" cp:size="(60,25)"/>
  <icon name="Desktop" cp:screencoord="(330,150)" cp:size="(80,70)"/>
  <push_button name="Open" cp:screencoord="(700,500)" cp:size="(80,30)"/>
 </frame>
</application>"""


def _targets(xml=FILES_XML):
    raw = A11yTargetBuilder().build(xml, "s", 128)
    return enrich_targets(raw, xml, (1920, 1080))


def _plan_with(proposals):
    return {
        "done": False,
        "stage_goal": "Open Desktop",
        "milestones": [{
            "route": "gui", "current_goal": "Open Desktop", "expected_observation": "Desktop opens",
            "action_proposals": proposals, "checks": [],
        }],
    }


def _stage_plan(tmp_path, targets, proposals, xml=FILES_XML):
    plan = _plan_with(proposals)
    session = SimpleNamespace(
        instruction="Open Desktop",
        requirements=[],
        recent=[],
        verified_focus=None,
        components=SimpleNamespace(planner=lambda *args: plan),
        events=[],
    )
    observation = make_observation(tmp_path, 0)
    stage = StageExecution(10)
    result = stage.plan(session, observation, targets, xml, Path(observation.screenshot.path))
    return result, session


def test_compatible_role_is_bound_only_when_unique(tmp_path):
    targets = _targets()
    proposals = [{"operation": "click", "target_name": "File", "target_role": "menu_item"}]
    plan, _ = _stage_plan(tmp_path, targets, proposals)
    assert not plan.get("recovery")
    assert plan["action_proposals"][0]["target_id"] == targets[0].id


def test_unbound_proposal_returns_recovery_and_diagnostic_context(tmp_path):
    targets = _targets()
    proposals = [{"operation": "click", "target_name": "Missing", "target_role": "link"}]
    plan, session = _stage_plan(tmp_path, targets, proposals)
    assert plan["recovery"] is True and plan["recovery_scopes"] == ["menu", "native"]
    replan = next(event for event in session.events if event.get("route") == "stage_replan")
    assert replan["reason"] == "target_unbound_or_ambiguous"
    assert replan["unbound_proposals"] == proposals
    assert replan["available_targets"]

    space = ActionCandidateBuilder().build(targets, 96, action_inputs=[], context="Desktop")
    space = focused_candidates(
        space, targets, FILES_XML, [], None, recovery=True,
        recovery_scopes=plan["recovery_scopes"],
    )
    candidates = space.candidates()
    assert any(candidate.operation == "click" for candidate in candidates)
    assert any(candidate.operation == "double_click" for candidate in candidates)
    assert any(candidate.operation == "return_to_planner" for candidate in candidates)
    assert not any(candidate.operation == "visual_fallback" for candidate in candidates)


def test_duplicate_compatible_targets_are_not_guessed(tmp_path):
    xml = FILES_XML.replace(
        '<icon name="Desktop" cp:screencoord="(330,150)" cp:size="(80,70)"/>',
        '<icon name="Desktop" cp:screencoord="(330,150)" cp:size="(80,70)"/>'
        '<icon name="Desktop" cp:screencoord="(450,150)" cp:size="(80,70)"/>',
    )
    targets = _targets(xml)
    proposals = [{"operation": "double_click", "target_name": "Desktop", "target_role": "icon"}]
    plan, _ = _stage_plan(tmp_path, targets, proposals, xml)
    assert plan["recovery"] is True


def test_unbound_stage_reaches_selector_instead_of_failing_before_jev(tmp_path):
    proposals = [{"operation": "click", "target_name": "Missing", "target_role": "link"}]
    planner = ScriptedPlanner([_plan_with(proposals)])
    selector = FixedSelector()
    session = JevDuoSession(
        make_components(planner, ScriptedVerifier([]), selector),
        config=SessionConfig(execution_mode="stage"),
    )
    session.begin("Open Desktop", [])
    action = session.act(make_observation(tmp_path, 0, tree_xml=FILES_XML))
    assert isinstance(action, GuiAction)
    assert selector.calls
    assert "return_to_planner" in selector.calls[0]["operations"]


def test_bound_but_unexecutable_scroll_gets_keyboard_recovery(tmp_path):
    proposals = [{"operation": "scroll_down", "target_name": "File", "target_role": "menu"}]
    planner = ScriptedPlanner([_plan_with(proposals)])
    selector = FixedSelector()
    session = JevDuoSession(
        make_components(planner, ScriptedVerifier([]), selector),
        config=SessionConfig(execution_mode="stage"),
    )
    session.begin("Open Desktop", [])
    action = session.act(make_observation(tmp_path, 0, tree_xml=FILES_XML))
    assert isinstance(action, GuiAction)
    gui = next(event for event in session.events if event.get("route") == "gui")
    assert gui["plan"]["recovery_reason"] == "no_executable_candidate"
    candidate_ids = {candidate["id"] for candidate in gui["candidates"]}
    assert "KEYPRESS_PAGE_DOWN" in candidate_ids
    assert "KEY_SEQUENCE:down" in candidate_ids
    assert "RETURN_TO_PLANNER" in candidate_ids
