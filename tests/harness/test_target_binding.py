"""Generic synthetic target-binding regressions; not benchmark or real-desktop acceptance."""

from dataclasses import replace

import pytest

from tandem.gui.candidates import ActionCandidateBuilder
from tandem.gui.semantics import enrich_targets
from tandem.gui.targets import A11yTargetBuilder

from .test_semantics import CHROME_XML, _named, _targets


@pytest.mark.parametrize("text", ["/analytics", "/inventory", "42,000"])
def test_sidebar_goal_does_not_promote_body_text_or_table_cells_to_navigation(text):
    base = _named(_targets(), "Save")
    targets = [replace(base, id=role, role=role, name=text, operations=())
               for role in ("text", "label", "table_cell")]
    targets.append(replace(base, id="nav", name="Inventory", source="dom", operations=("click",),
                           parent_context=("navigation", "Workspace")))
    space = ActionCandidateBuilder().build(targets, context="Open Inventory using the sidebar")
    assert [c.target_id for c in space.target_heads["click"]] == ["nav"]
    assert not space.target_heads["double_click"]


def test_explicitly_actionable_grid_cell_and_native_table_editing_are_preserved():
    base = _named(_targets(), "Save")
    targets = [replace(base, id="interactive", role="grid_cell", operations=("click",)),
               replace(base, id="native", role="table_cell", scope="native", operations=())]
    space = ActionCandidateBuilder().build(targets, context="Use the sidebar and edit the selected cell")
    assert {c.target_id for c in space.target_heads["click"]} == {"interactive", "native"}


def test_structural_navigation_binding_survives_candidate_budget_and_dom_merge():
    base = _named(_targets(), "Save")
    body = [replace(base, id=str(i), name="Open Inventory using sidebar", x=300 + i)
            for i in range(30)]
    body.append(replace(base, id="nav", name="Inventory", x=50, y=220, width=100, height=30))
    snapshot = {"elements": [{"index": 1, "role": "push_button", "name": "Inventory", "x": 50,
        "y": 220, "width": 100, "height": 30, "parent_context": ["navigation", "Workspace"],
        "operations": ["click"]}]}
    targets = enrich_targets(body, CHROME_XML, (1920, 1080), snapshot)
    space = ActionCandidateBuilder().build(targets, max_candidates=4,
                                           context="Open Inventory using the sidebar")
    assert space.target_heads["click"][0].target_id == "nav"
    assert _named(targets, "Inventory").source == "a11y+dom"


@pytest.mark.parametrize("tag", ["dialog", "frame"])
def test_native_modal_owns_input_and_browser_dom_cannot_reintroduce_background_controls(tag):
    xml = f'''<desktop-frame foreground_title="Select a document">
      <application name="Google Chrome" active="true">
        <frame name="Editor" active="true">
          <push_button name="Upload" screencoord="(50,120)" size="(80,30)"/>
        </frame>
        <{tag} name="Select a document" modal="true" active="true" visible="true" showing="true"
            screencoord="(200,200)" size="(600,400)">
          <entry name="Location" screencoord="(240,250)" size="(400,30)"/>
          <push_button name="Open" screencoord="(650,550)" size="(100,30)"/>
        </{tag}>
      </application>
    </desktop-frame>'''
    snapshot = {"elements": [{"index": 1, "role": "push_button", "name": "Background delete",
                              "x": 900, "y": 400, "width": 100, "height": 30}]}
    raw = A11yTargetBuilder().build(xml, "t0", filter_occluded=True)
    targets = enrich_targets(raw, xml, (1920, 1080), snapshot)
    assert {t.name for t in targets} == {"Location", "Open"}
    assert all(t.scope == "dialog" for t in targets)


def test_native_popup_menu_suspends_even_uncovered_web_dom_until_menu_closes():
    def tree(showing):
        return f'''<application name="Google Chrome" active="true">
          <frame name="Editor" active="true">
            <push_button name="Delete row" screencoord="(700,500)" size="(100,30)"/>
            <menu name="Context" ispopup="true" active="true" showing="{showing}"
                screencoord="(100,100)" size="(250,300)">
              <menu_item name="Back" showing="{showing}" screencoord="(110,110)" size="(200,30)"/>
            </menu>
          </frame>
        </application>'''
    snapshot = {"elements": [{"index": 1, "role": "push_button", "name": "Delete row",
                              "x": 700, "y": 500, "width": 100, "height": 30}]}
    for showing, expected in [("true", {"Context", "Back"}), ("false", {"Delete row"})]:
        xml = tree(showing)
        raw = A11yTargetBuilder().build(xml, "t0", filter_occluded=True)
        targets = enrich_targets(raw, xml, (1920, 1080), snapshot)
        assert {t.name for t in targets} == expected
        if showing == "true":
            assert all(t.scope == "menu" for t in targets)


def test_retained_inactive_modal_does_not_suspend_the_current_page():
    xml = '''<desktop-frame foreground_title="Editor">
      <application name="Google Chrome" active="true"><frame name="Editor" active="true">
        <push_button name="Save" screencoord="(700,500)" size="(100,30)"/></frame>
        <dialog name="Old picker" modal="true" active="false" showing="true"/>
      </application></desktop-frame>'''
    snapshot = {"elements": [{"index": 1, "role": "push_button", "name": "Save",
                              "x": 700, "y": 500, "width": 100, "height": 30}]}
    raw = A11yTargetBuilder().build(xml, "t0", filter_occluded=True)
    targets = enrich_targets(raw, xml, (1920, 1080), snapshot)
    assert [t.name for t in targets] == ["Save"]
    assert targets[0].source == "a11y+dom"


def test_web_dialog_does_not_disable_its_browser_dom_controls():
    xml = '''<application name="Google Chrome" active="true">
      <frame name="Editor" active="true"><document_web name="Web editor">
        <dialog name="Confirm replacement" modal="true" active="true">
          <push_button name="Replace" screencoord="(700,500)" size="(100,30)"/>
        </dialog>
      </document_web></frame></application>'''
    snapshot = {"elements": [{"index": 1, "role": "push_button", "name": "Replace",
                              "x": 700, "y": 500, "width": 100, "height": 30}]}
    raw = A11yTargetBuilder().build(xml, "t0", filter_occluded=True)
    targets = enrich_targets(raw, xml, (1920, 1080), snapshot)
    assert [t.name for t in targets] == ["Replace"]
    assert targets[0].source == "a11y+dom"
