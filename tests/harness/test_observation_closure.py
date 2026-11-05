"""Synthetic observation-to-action regressions; no benchmark IDs or answers."""

from dataclasses import replace

import pytest

from tandem.gui.candidates import ActionCandidateBuilder
from tandem.gui.field_binding import _input_target, matches_field
from tandem.gui.goal_candidates import foreground_tree, goal_candidates, modal_frontier
from tandem.gui.interaction import target_effect
from tandem.gui.semantics import enrich_targets
from tandem.gui.targets import A11yTargetBuilder

from .test_semantics import _named, _targets


@pytest.mark.parametrize("role", ["static", "text"])
@pytest.mark.parametrize("action", ["clickAncestor_desc", "click_desc", "press_desc"])
def test_native_click_capability_preserves_navigation_not_plain_text(role, action):
    tree = f'''<application name="Google Chrome" active="true" xmlns:a="urn:actions">
      <frame name="Workspace" active="true">
        <{role} name="Inventory" a:{action}="" screencoord="(20,100)" size="(80,20)"/>
        <{role} name="/inventory" screencoord="(400,200)" size="(80,20)"/>
      </frame></application>'''
    targets = A11yTargetBuilder().build(tree, "test", filter_occluded=True)
    assert [t.name for t in targets] == ["Inventory"]
    assert targets[0].operations == ("click",)
    space = ActionCandidateBuilder().build(targets, action_inputs=[], context="Use sidebar Inventory")
    assert [c.target_id for c in space.target_heads["click"]] == [targets[0].id]


def test_candidate_limit_is_explicit_not_a_hidden_twenty_four():
    base = _named(_targets(), "Save")
    targets = [replace(base, id=str(i), name=f"Control {i}", x=i * 10) for i in range(40)]
    space = ActionCandidateBuilder().build(targets, max_candidates=40)
    assert len(space.target_heads["click"]) == 40
    assert len(ActionCandidateBuilder().build(targets, max_candidates=7).target_heads["click"]) == 7


def test_exact_control_label_beats_repeated_sidebar_context():
    base = _named(_targets(), "Save")
    targets = [replace(base, id=str(i), name=f"Section {i}",
                       nearby_text="Permissions project user policy roles settings") for i in range(30)]
    targets.append(replace(base, id="wanted", name="Permissions"))
    space = ActionCandidateBuilder().build(targets, max_candidates=4, context="Open Permissions")
    assert space.target_heads["click"][0].target_id == "wanted"


def test_page_container_cannot_rename_a_nested_button():
    base = replace(_named(_targets(), "Save"), name="Publish", x=500, y=400)
    tree = '<application name="Google Chrome" active="true"/>'
    snapshot = {"elements": [{"index": 1, "role": "push_button",
        "name": "Workspace all documents Publish and other actions", "x": 0, "y": 0,
        "width": 1600, "height": 900, "operations": ["click"]}]}
    targets = enrich_targets([base], tree, (1920, 1080), snapshot)
    assert next(t for t in targets if t.id == base.id).name == "Publish"
    assert next(t for t in targets if t.id == base.id).source == "a11y"


@pytest.mark.parametrize("dx,dy", [(80, 0), (0, 25), (40, 0)])
def test_same_named_controls_without_verified_alignment_remain_distinct(dx, dy):
    base = replace(_named(_targets(), "Save"), name="Publish", x=500, y=400)
    snapshot = {"elements": [{"index": 1, "role": "push_button", "name": "Publish",
        "x": base.x + dx, "y": base.y + dy, "width": base.width, "height": base.height,
        "operations": ["click"]}]}
    targets = enrich_targets([base], None, (1920, 1080), snapshot)
    assert [(t.x, t.y, t.source) for t in targets] == [
        (500, 400, "a11y"), (500 + dx, 400 + dy, "dom"),
    ]
    space = ActionCandidateBuilder().build(targets, action_inputs=[])
    assert {c.target_id for c in space.target_heads["click"]} == {t.id for t in targets}


@pytest.mark.parametrize("anchor_count,expected_y", [(2, 425), (3, 400)])
def test_only_three_unique_anchors_correct_dom_only_control_coordinates(anchor_count, expected_y):
    base = _named(_targets(), "Save")
    anchors = [replace(base, id=str(i), name=f"Anchor {i}", x=200, y=150 + 60 * i)
               for i in range(anchor_count)]
    elements = [{"index": i, "role": t.role, "name": t.name, "x": t.x, "y": t.y + 25,
                 "width": t.width, "height": t.height} for i, t in enumerate(anchors)]
    elements.append({"index": 4, "role": "push_button", "name": "New action", "x": 600,
                     "y": 425, "width": 100, "height": 30})
    targets = enrich_targets(anchors, None, (1920, 1080), {"elements": elements})
    assert _named(targets, "New action").y == expected_y
    if anchor_count == 3:
        assert len(targets) == 4
        assert [(t.id, t.y, t.source) for t in targets[:3]] == [
            (t.id, t.y, "a11y+dom") for t in anchors
        ]


@pytest.mark.parametrize("alias_kind", ["dom_duplicate", "dom_nested", "native_duplicate"])
def test_aliases_of_one_physical_control_cannot_calibrate_the_page(alias_kind):
    base = _named(_targets(), "Save")
    anchors = ([replace(base, id=str(i), name=f"Alias {i}") for i in range(3)]
               if alias_kind == "native_duplicate" else [base])
    elements = [{"index": i, "role": base.role, "name": anchors[i].name if len(anchors) == 3 else base.name,
                 "x": base.x, "y": base.y + 25,
                 "width": base.width + (i if alias_kind == "dom_nested" else 0), "height": base.height}
                for i in range(3)]
    elements.append({"index": 4, "role": "push_button", "name": "New action", "x": 600,
                     "y": 425, "width": 100, "height": 30})
    targets = enrich_targets(anchors, None, (1920, 1080), {"elements": elements})
    assert _named(targets, "New action").y == 425


def test_ambiguous_anchor_does_not_contribute_to_frame_alignment():
    base = _named(_targets(), "Save")
    anchors = [replace(base, id=str(i), name=f"Anchor {i}", x=200, y=150 + 60 * i)
               for i in range(3)]
    elements = [{"index": i, "role": t.role, "name": t.name, "x": t.x, "y": t.y + 25,
                 "width": t.width, "height": t.height} for i, t in enumerate(anchors)]
    elements.extend([
        {**elements[0], "index": 3, "y": anchors[0].y + 27},
        {"index": 4, "role": "push_button", "name": "New action", "x": 600,
         "y": 425, "width": 100, "height": 30},
    ])
    targets = enrich_targets(anchors, None, (1920, 1080), {"elements": elements})
    assert _named(targets, "New action").y == 425


def test_ambiguous_same_title_windows_do_not_activate_background_frame():
    tree = '''<desktop-frame foreground_title="Workspace" foreground_class='["google-chrome"]'>
      <application name="Google Chrome"><frame name="Workspace" active="true">
        <push_button name="Current" screencoord="(10,20)" size="(30,20)"/>
      </frame><frame name="Workspace"><push_button name="Background"
        screencoord="(10,50)" size="(30,20)"/></frame></application>
      <application name="gnome-shell"/></desktop-frame>'''
    targets = A11yTargetBuilder().build(foreground_tree(tree), "test")
    assert [t.name for t in targets] == ["Current"]


def test_generic_short_label_does_not_erase_field_qualifier_or_owner():
    assert not matches_field("File name", "Name")
    assert not matches_field("Messaging Search mail", "Search", "Cloud console")
    assert matches_field("Messaging Search mail", "Search mail", "Messaging")
    assert matches_field("Name", "Name")


def test_password_has_an_input_candidate_and_is_not_claimed_verified():
    tree = '''<application name="Google Chrome" active="true"><frame name="Sign in">
      <password-text name="Password" editable="true" focused="true" screencoord="(100,200)"
          size="(200,30)"/>
    </frame></application>'''
    targets = A11yTargetBuilder().build(tree, "test")
    value = {"label": "Login password", "target_hint": "Password", "text": "synthetic-only",
             "input_mode": "replace"}
    assert _input_target(value, targets) is targets[0]
    space = goal_candidates(ActionCandidateBuilder().build(targets), targets, tree, [value])
    assert space.target_heads["type_text"][0].metadata["text"] == "synthetic-only"
    assert target_effect({"operation": "type_text", "target_semantic": targets[0].semantic_key,
                          "field_label": "Password"}, targets) == "unknown"


def test_coordinate_free_native_chooser_still_owns_input():
    tree = '''<desktop-frame foreground_title="Choose Files"
        foreground_class='["xdg-desktop-portal-gnome"]'>
      <application name="xdg-desktop-portal-gnome"><dialog name="Choose Files"
          modal="true" visible="true"><text name="GtkFileChooserEntry" editable="true"/>
      </dialog></application></desktop-frame>'''
    space = goal_candidates(ActionCandidateBuilder().build([]), [], tree, [])
    frontier = modal_frontier(space, [], tree)
    assert frontier["blocking_overlay"] == "Choose Files"
    assert frontier["overlay_kind"] == "task_dialog"
    assert "active dialog" in frontier["next_intent"]
