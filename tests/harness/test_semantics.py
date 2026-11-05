"""Shared semantic perception, scoped candidates, and compact JEV requests."""

import json

from tandem.gui.candidates import ActionCandidateBuilder
from tandem.gui.proposals import focused_candidates
from tandem.gui.semantics import enrich_targets
from tandem.gui.targets import A11yTargetBuilder
from tandem.models.ranking import build_choice_requests

CHROME_XML = """<application name="Google Chrome" active="true"
 xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"
 xmlns:attr="https://accessibility.ubuntu.example.org/ns/attributes"
 xmlns:st="https://accessibility.ubuntu.example.org/ns/state">
 <frame name="ExpenseFlow" st:active="true">
  <push_button name="Minimise" cp:screencoord="(1802,27)" cp:size="(30,35)"/>
  <push_button name="New Tab" cp:screencoord="(824,27)" cp:size="(28,41)"/>
  <entry name="Address and search bar" cp:screencoord="(286,78)" cp:size="(1506,24)"/>
  <label name="Receipt Date" cp:screencoord="(100,293)" cp:size="(140,18)"/>
  <entry cp:screencoord="(250,293)" cp:size="(218,18)" st:editable="true" st:focused="true"
   attr:description="Receipt date in day/month/year order">500.00</entry>
  <label name="Receipt Amount" cp:screencoord="(100,316)" cp:size="(140,18)"/>
  <entry cp:screencoord="(250,316)" cp:size="(218,18)" st:editable="true"></entry>
  <link name="Inbox 2" cp:screencoord="(70,274)" cp:size="(240,32)"/>
  <push_button name="Save" cp:screencoord="(700,400)" cp:size="(80,30)"/>
 </frame>
</application>"""


def _targets(snapshot=None):
    raw = A11yTargetBuilder().build(CHROME_XML, "t8", 128, infer_button_labels=True)
    return enrich_targets(raw, CHROME_XML, (1920, 1080), snapshot)


def _named(targets, name):
    return next(item for item in targets if item.name == name)


def _space(targets):
    return ActionCandidateBuilder().build(targets, 96, action_inputs=[], context="expense")


def test_a11y_recovers_nearby_labels_and_separates_browser_chrome():
    targets = _targets()
    date = _named(targets, "Receipt Date")
    amount = _named(targets, "Receipt Amount")
    assert date.label_source == "a11y_nearby"
    assert date.value == "500.00" and "500.00" not in date.name
    assert date.scope == amount.scope == "web_content"
    assert _named(targets, "Minimise").scope == "browser_chrome"


def test_dom_enriches_a11y_semantics_without_admitting_uncalibrated_dom_only_controls():
    date = _named(_targets(), "Receipt Date")
    snapshot = {
        "viewport": {"screen_y": 112},
        "elements": [
            {
                "index": 1, "role": "entry", "name": "Receipt Date",
                "description": "Receipt date", "value": "11/12/2025",
                "x": date.x, "y": date.y, "width": date.width, "height": date.height,
                "operations": ["click", "type_text"], "label_source": "dom",
            },
            {
                "index": 2, "role": "push_button", "name": "Upload receipt",
                "x": 900, "y": 400, "width": 120, "height": 30,
                "operations": ["click"], "label_source": "dom",
            },
        ],
    }
    targets = _targets(snapshot)
    merged = _named(targets, "Receipt Date")
    assert merged.id == date.id and merged.source == "a11y+dom"
    assert merged.value == "11/12/2025" and merged.operations == ("click", "type_text")
    assert not any(t.name == "Upload receipt" for t in targets)
    click = next(c for c in _space(targets).target_heads["click"] if c.target_id == merged.id)
    assert click.source == "a11y+dom"


def test_web_proposal_does_not_reintroduce_browser_shell_candidates():
    targets = _targets()
    inbox = _named(targets, "Inbox 2")
    proposal = {"operation": "click", "target_id": inbox.id,
                "target_name": inbox.name, "target_role": inbox.role}
    space = focused_candidates(_space(targets), targets, CHROME_XML, [proposal], None)
    names = {target.name for target in targets if target.id in {c.target_id for c in space.candidates()}}
    assert "Inbox 2" in names and "Save" in names
    assert "Minimise" not in names and "New Tab" not in names


def test_shell_proposal_does_not_reintroduce_web_candidates():
    targets = _targets()
    tab = _named(targets, "New Tab")
    proposal = {"operation": "click", "target_id": tab.id,
                "target_name": tab.name, "target_role": tab.role}
    space = focused_candidates(_space(targets), targets, CHROME_XML, [proposal], None)
    names = {target.name for target in targets if target.id in {c.target_id for c in space.candidates()}}
    assert "New Tab" in names and "Minimise" in names
    assert "Inbox 2" not in names and "Save" not in names


def test_recovery_candidates_stay_inside_requested_scope():
    targets = _targets()
    space = focused_candidates(
        _space(targets), targets, CHROME_XML, [], None,
        recovery=True, recovery_scopes={"web_content"},
    )
    names = {target.name for target in targets if target.id in {c.target_id for c in space.candidates()}}
    assert "Inbox 2" in names and "Save" in names
    assert "Minimise" not in names and "New Tab" not in names
    assert any(candidate.operation == "return_to_planner" for candidate in space.candidates())


def test_focused_typing_rejects_a_different_named_field():
    targets = _targets()
    proposal = {"operation": "type_text", "target_name": "Receipt Amount",
                "target_role": "entry", "text": "500.00", "input_mode": "replace"}
    space = focused_candidates(_space(targets), targets, CHROME_XML, [proposal], None)
    assert not [candidate for candidate in space.candidates() if candidate.operation == "type_text"]
    proposal["target_name"] = "Receipt Date"
    space = focused_candidates(_space(targets), targets, CHROME_XML, [proposal], None)
    assert [candidate for candidate in space.candidates() if candidate.operation == "type_text"]


def test_jev_request_has_semantics_but_not_full_verification_history():
    targets = _targets()
    inbox = _named(targets, "Inbox 2")
    proposal = {"operation": "click", "target_id": inbox.id,
                "target_name": inbox.name, "target_role": inbox.role}
    space = focused_candidates(_space(targets), targets, CHROME_XML, [proposal], None)
    recent = [{"operation": "click", "target": "Save", "outcome": "achieved",
               "verification": {"evidence": "large"}}
             ]
    operation, actions = build_choice_requests(
        stage_goal="Expense", intent="Open inbox", active_window="", targets=targets,
        action_space=space, recent_actions=recent,
    )
    state = json.loads(operation["state"])
    assert state["recent_semantic_actions"] == [
        {"operation": "click", "target": "Save", "outcome": "achieved"}
    ]
    assert "verification" not in operation["state"]
    assert "Scope: web_content" in actions["click"]["options"][0]
