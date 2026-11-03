"""focus：a11y 上下文提取 / toggle 检测 / 候选修剪 / action_key。"""

from __future__ import annotations

from tandem.gui.candidates import ActionCandidate, ActionCandidateBuilder
from tandem.gui.focus import action_key, input_and_menu_context, prune_stalled, toggle_transition

from .conftest import A11Y_XML, build_targets


def test_menu_extraction_with_alt_mnemonic():
    inputs, menus = input_and_menu_context(A11Y_XML)
    assert menus and menus[0]["name"] == "File"
    assert menus[0]["key_sequence"] == [["esc"], ["alt", "f"], ["i"]]


def test_menu_item_mnemonic_does_not_confirm_the_dialog_it_opens():
    tree = '''<application active="true"><menu name="File">
      <menu_item name="Open..." click_kb=";&lt;Alt&gt;f:o;"/>
    </menu></application>'''
    _, menus = input_and_menu_context(tree)
    assert menus[0]["key_sequence"] == [["esc"], ["alt", "f"], ["o"]]


def test_inputs_include_focused_editable_with_bounds():
    inputs, _ = input_and_menu_context(A11Y_XML)
    focused = [item for item in inputs if item["focused"]]
    assert focused and focused[0]["bounds"] == [300, 120, 200, 28]


def test_toggle_transition_detects_change():
    targets = build_targets()
    toggle = next(t for t in targets if t.name == "启用")
    after = A11Y_XML.replace('name="启用" cp:screencoord="(300,160)" cp:size="(120,24)" checked="false"',
                             'name="启用" cp:screencoord="(300,160)" cp:size="(120,24)" checked="true"')
    result = toggle_transition(A11Y_XML, after, toggle)
    assert result == {"target": "启用", "before": "false", "after": "true", "changed": True}
    assert toggle_transition(A11Y_XML, A11Y_XML, toggle)["changed"] is False


def test_toggle_transition_returns_none_for_ambiguous_or_non_toggle():
    targets = build_targets()
    button = next(t for t in targets if t.name == "确定")
    assert toggle_transition(A11Y_XML, A11Y_XML, button) is None
    ambiguous = A11Y_XML.replace(
        "</frame>",
        '<check_box name="启用" cp:screencoord="(500,160)" cp:size="(120,24)" checked="false"/></frame>',
    )
    toggle = next(t for t in targets if t.name == "启用")
    assert toggle_transition(A11Y_XML, ambiguous, toggle) is None


def test_prune_stalled_removes_repeated_blocked_candidate():
    targets = build_targets()
    space = ActionCandidateBuilder().build(targets, 96, [], context="打开")
    candidate = space.target_heads["click"][0]
    key = action_key(candidate)
    recent = [
        {"action_key": key, "state_changed": False},
        {"action_key": key, "state_changed": False},
    ]
    prune_stalled(space, recent)
    assert all(action_key(c) != key for c in space.candidates())


def test_action_key_covers_semantic_metadata():
    click = ActionCandidate(
        id="click:x", operation="click", description="d", metadata={"semantic_key": "s|k"}
    )
    typed = ActionCandidate(
        id="t:x", operation="type_text", description="d",
        metadata={"text": "hi", "input_mode": "replace"},
    )
    assert action_key(click) != action_key(typed)
    assert action_key(click) == action_key(
        ActionCandidate(id="click:y", operation="click", description="d",
                        metadata={"semantic_key": "s|k"})
    )
