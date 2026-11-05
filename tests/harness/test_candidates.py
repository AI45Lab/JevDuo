"""candidates：UITarget → ActionSpace。与旧仓库 action_candidate_builder 行为对齐。"""

from __future__ import annotations

from jevduo.gui.candidates import ActionCandidateBuilder

from .conftest import A11Y_XML, build_targets


def _space(context="打开文件", xml=A11Y_XML):
    return ActionCandidateBuilder().build(build_targets(xml), 96, action_inputs=[], context=context)


def test_click_head_present_with_center_candidates():
    space = _space()
    assert space.target_heads["click"]
    click = space.target_heads["click"][0]
    assert click.operation == "click" and click.target_id and click.metadata["semantic_key"]


def test_type_head_limited_to_six_and_excluded_from_click():
    space = _space()
    assert len(space.target_heads["type_text"]) <= 6
    typed_ids = {c.target_id for c in space.target_heads["type_text"]}
    assert all(c.target_id not in typed_ids for c in space.target_heads["click"])


def test_context_keywords_add_keypress_options():
    space = _space(context="open terminal window and close the dialog")
    ids = [c.id for c in space.target_heads["keypress"]]
    assert "KEYPRESS_CTRL_ALT_T" in ids
    assert "KEYPRESS_ALT_F4" in ids
    plain = _space(context="普通目标")
    assert "KEYPRESS_CTRL_ALT_T" not in [c.id for c in plain.target_heads["keypress"]]


def test_controls_exclude_operations_already_in_heads():
    space = _space()
    assert "click" not in space.controls
    assert "wait" in space.controls and space.controls["wait"].operation == "wait"


def test_chrome_close_button_filtered_unless_close_requested():
    xml = A11Y_XML.replace(
        '<push_button name="确定"',
        '<push_button name="Close" cp:screencoord="(10,10)" cp:size="(60,24)"/>'
        '<push_button name="确定"',
        1,
    ).replace('name="main-window"', 'name="Google Chrome"')
    space_plain = ActionCandidateBuilder().build(build_targets(xml), 96, [], context="点击其它按钮")
    close = [t for t in space_plain.targets if t.name == "Close"][0]
    assert not any(c.target_id == close.id for c in space_plain.target_heads["click"])
    space_close = ActionCandidateBuilder().build(build_targets(xml), 96, [], context="close the browser")
    assert any(c.target_id == close.id for c in space_close.target_heads["click"])


def test_candidates_flatten_heads_and_controls():
    space = _space()
    assert len(space.candidates()) == sum(
        len(items) for items in space.target_heads.values()
    ) + len(space.controls)
