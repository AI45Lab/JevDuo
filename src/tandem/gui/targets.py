"""a11y tree → UITarget 列表；从旧仓库 gui/ui_target.py + a11y_target_builder.py 按契约移植。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass


@dataclass(slots=True)
class UITarget:
    id: str
    snapshot_id: str
    role: str
    name: str
    value: str
    enabled: bool | None
    visible: bool | None
    showing: bool | None
    selected: bool | None
    checked: bool | None
    focused: bool | None
    x: int | None
    y: int | None
    width: int | None
    height: int | None
    semantic_key: str
    parent_context: tuple[str, ...]
    source: str = "a11y"
    application: str = ""
    description: str = ""
    nearby_text: str = ""
    scope: str = "native"
    label_source: str = "a11y"
    operations: tuple[str, ...] = ()
    range_min: float | None = None
    range_max: float | None = None
    input_type: str = ""
    date_order: tuple[str, ...] = ()

    def compact(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value not in (None, "", (), False)}

    def contains(self, x: int, y: int) -> bool:
        return (
            self.x is not None
            and self.y is not None
            and self.width is not None
            and self.height is not None
            and self.x <= x < self.x + self.width
            and self.y <= y < self.y + self.height
        )


SUPPORTED_ROLES = {
    "button",
    "push_button",
    "checkbox",
    "check_box",
    "radio_button",
    "textbox",
    "text",
    "entry",
    "searchbox",
    "password_text",
    "combo_box",
    "menu",
    "menu_item",
    "tab",
    "page_tab",
    "link",
    "toggle_button",
    "switch",
    "spin_button",
    "icon",
    "list_item",
    "tree_item",
    "table_cell",
    "grid_cell",
    "option",
}


def _local(value: str) -> str:
    return value.rsplit("}", 1)[-1].replace("-", "_").lower()


def _state(element: ET.Element, name: str) -> bool | None:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return str(value).lower() == "true"
    return None


def _pair(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", value or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _attribute(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return str(value)
    return ""


def _number(element, name):
    value = _attribute(element, name)
    return float(value) if re.fullmatch(r"-?\d+(?:\.\d+)?", value) else None


def _is_overlay(element):
    return _local(element.tag) in {"dialog", "alert", "popup_menu"} or (
        _local(element.tag) == "menu" and _attribute(element, "ispopup") in {"true", "auto", "menu"})


def _field_labels(root):
    labels = {}
    for parent in root.iter():
        preceding = ""
        for child in parent:
            if _local(child.tag) in {"label", "static"}:
                preceding = str(child.get("name") or child.text or "").strip()
                continue
            fields = [e for e in child.iter() if _state(e, "editable") is True
                      or _local(e.tag) in {"entry", "textbox", "searchbox", "combo_box", "spin_button"}]
            if preceding and len(fields) == 1:
                labels[fields[0]] = preceding
            preceding = ""
    return labels


class A11yTargetBuilder:
    def build(
        self,
        raw_tree: str,
        snapshot_id: str,
        max_targets: int = 64,
        infer_button_labels: bool = False,
        include_sliders: bool = False,
        context: str = "",
        filter_occluded: bool = False,
    ) -> list[UITarget]:
        root = ET.fromstring(raw_tree)
        targets: list[UITarget] = []
        field_labels = _field_labels(root) if filter_occluded else {}
        target_nodes, overlays = {}, {}
        if filter_occluded:
            for node in root.iter():
                if (_is_overlay(node) and _state(node, "showing") is True
                        and _state(node, "visible") is not False):
                    position, size = _pair(_attribute(node, "screencoord")), _pair(_attribute(node, "size"))
                    if position and size and min(size) > 0:
                        overlays.setdefault((*position, *size), set()).update(node.iter())

        def visit(element, active, parents, app_name, modal=False, roles=(), file_manager=False):
            active = active or _state(element, "active") is True
            role = _local(element.tag)
            modal = modal or _is_overlay(element)
            raw_name = str(element.attrib.get("name") or "").strip()
            name = raw_name
            if role == "application":
                app_name = name
            label_source = "a11y" if name else "unresolved"
            if not name and element in field_labels:
                name, label_source = field_labels[element], "a11y_adjacent"
            if (not name and filter_occluded and "gnome-shell" in parents
                    and "Overview" in parents and _state(element, "editable") is True):
                name, label_source = "Desktop search", "a11y_context"
            file_manager = file_manager or "nautilus" in app_name.lower() or "file chooser" in name.lower()
            value = (
                str(element.text or "").strip()
                if role in {"text", "textbox", "entry", "searchbox", "spin_button"}
                else _attribute(element, "value") if role == "slider" else ""
            )
            if role == "spin_button" and not value and (number := _number(element, "value")) is not None:
                value = format(number, "g")
            position = _pair(_attribute(element, "screencoord"))
            size = _pair(_attribute(element, "size"))
            if infer_button_labels and not name and role in {"button", "push_button"} and position and size:
                labels = {
                    str(child.attrib.get("name") or "").strip()
                    for child in element.iter()
                    if _local(child.tag) in {"label", "static"} and child.attrib.get("name")
                }
                if len(labels) == 1:
                    name = labels.pop()
                    label_source = "a11y_descendant"
            enabled = _state(element, "enabled")
            visible = _state(element, "visible")
            showing = _state(element, "showing")
            native_click = any(_local(key) in {"clickancestor_desc", "click_desc", "press_desc"}
                               for key in element.attrib)
            context = parents + ((name,) if name else ())
            interactive_text = role != "text" or (
                _state(element, "editable") is True or _state(element, "focusable") is True
            )
            system_target = "gnome-shell" in {item.lower() for item in parents}
            safe_label = (
                role == "label"
                and active
                and (
                    any("file chooser" in item.lower() or item.lower() == "places" for item in parents)
                    or filter_occluded and file_manager and any(r in {
                        "list_item", "tree_item", "button", "push_button", "menu_item", "option", "link"}
                        for r in roles)
                    or bool(
                        re.search(
                            r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", name, re.I
                        )
                    )
                )
            )
            if (
                (active or system_target)
                and (role in SUPPORTED_ROLES or safe_label or (include_sliders and role == "slider")
                     or native_click
                     or filter_occluded and (role == "image" or file_manager and role == "canvas"))
                and (name or role in SUPPORTED_ROLES)
                and interactive_text
                and enabled is not False
                and visible is not False
                and showing is not False
                and position
                and size
                and size[0] > 0
                and size[1] > 0
            ):
                normalized_name = " ".join(name.lower().split())
                parent_context = tuple(item for item in parents[-3:] if item)
                targets.append(
                    UITarget(
                        id=f"a11y:{snapshot_id}:{len(targets)}",
                        snapshot_id=snapshot_id,
                        role=role,
                        name=name,
                        value=value,
                        enabled=enabled,
                        visible=visible,
                        showing=showing,
                        selected=_state(element, "selected"),
                        checked=_state(element, "checked"),
                        focused=_state(element, "focused"),
                        x=position[0],
                        y=position[1],
                        width=size[0],
                        height=size[1],
                        semantic_key="|".join((role, normalized_name, *[p.lower() for p in parent_context])),
                        parent_context=parent_context,
                        application=app_name,
                        description=(
                            _attribute(element, "description")
                            or _attribute(element, "placeholder")
                            or _attribute(element, "placeholder-text")
                        ),
                        label_source=label_source,
                        source="a11y_dialog" if filter_occluded and modal else "a11y",
                        operations=("click",) if native_click else (),
                        range_min=_number(element, "min") if role == "slider" else None,
                        range_max=_number(element, "max") if role == "slider" else None,
                    )
                )
                target_nodes[targets[-1].id] = element
            for child in element:
                visit(child, active, context[-4:], app_name, modal, (*roles, role), file_manager)

        visit(root, False, (), "")
        if filter_occluded:
            targets = [t for t in targets if not any(
                target_nodes[t.id] not in nodes and x <= t.x + t.width // 2 < x + width
                and y <= t.y + t.height // 2 < y + height
                for (x, y, width, height), nodes in overlays.items())]
        if context:
            words = set(re.findall(r"\w+", context.lower()))
            targets.sort(key=lambda t: (t.source == "a11y_dialog",
                len(words & set(re.findall(r"\w+", t.name.lower()))), t.focused is True,
                t.role in {"button", "push_button", "combo_box", "entry", "textbox"}), reverse=True)
        return targets[:max_targets]
