"""targets：a11y XML → UITarget。与旧仓库 a11y_target_builder 行为对齐。"""

from __future__ import annotations

from jevduo.gui.semantics import enrich_targets
from jevduo.gui.targets import A11yTargetBuilder

from .conftest import A11Y_XML


def test_builds_named_targets_with_ids_and_semantic_keys():
    targets = A11yTargetBuilder().build(A11Y_XML, "snap", 128, infer_button_labels=True, include_sliders=True)
    by_name = {t.name: t for t in targets}
    assert by_name["确定"].role == "push_button"
    assert by_name["确定"].id == "a11y:snap:0"
    assert by_name["确定"].x == 100 and by_name["确定"].width == 80
    assert "push_button|确定" in by_name["确定"].semantic_key
    assert by_name["启用"].role == "check_box"
    assert by_name["启用"].checked is False


def test_button_label_inferred_from_single_label_child():
    targets = A11yTargetBuilder().build(A11Y_XML, "snap", 128, infer_button_labels=True)
    assert any(t.name == "取消" and t.role == "push_button" for t in targets)


def test_focused_unnamed_editable_gets_synthetic_name():
    targets = enrich_targets(
        A11yTargetBuilder().build(A11Y_XML, "snap", 128), A11Y_XML, (1920, 1080)
    )
    names = {t.name for t in targets}
    assert any(name.startswith("Focused textbox") for name in names)


def test_slider_included_only_when_requested():
    without = A11yTargetBuilder().build(A11Y_XML, "s", 128, include_sliders=False)
    with_sliders = A11yTargetBuilder().build(A11Y_XML, "s", 128, include_sliders=True)
    assert not any(t.role == "slider" for t in without)
    assert any(t.role == "slider" and t.name == "缩放" for t in with_sliders)


def test_max_targets_enforced():
    targets = A11yTargetBuilder().build(A11Y_XML, "s", 2)
    assert len(targets) == 2


def test_inactive_subtree_excluded():
    xml = A11Y_XML.replace('active="true"', 'active="false"')
    targets = A11yTargetBuilder().build(xml, "s", 128)
    assert targets == []


def test_compact_drops_empty_values_and_contains_geometry():
    targets = A11yTargetBuilder().build(A11Y_XML, "s", 128)
    first = next(t for t in targets if t.name == "chart.png")
    compact = first.compact()
    assert compact["x"] == 400 and compact["width"] == 32
    assert all(value not in (None, "", (), False) for value in compact.values())
