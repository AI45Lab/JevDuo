"""a11y 上下文提取与候选修剪；从旧仓库 gui/actions.py 按契约移植（visual grounding 部分除外）。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from .candidates import ActionCandidate

KEYS = {
    "KEYPRESS_ENTER": ("enter",),
    "KEYPRESS_ESCAPE": ("esc",),
    "KEYPRESS_TAB": ("tab",),
    "KEYPRESS_CTRL_L": ("ctrl", "l"),
    "KEYPRESS_CTRL_F": ("ctrl", "f"),
    "KEYPRESS_CTRL_O": ("ctrl", "o"),
    "KEYPRESS_CTRL_ALT_T": ("ctrl", "alt", "t"),
    "KEYPRESS_ALT_F4": ("alt", "f4"),
    "KEYPRESS_PAGE_DOWN": ("pagedown",),
    "KEYPRESS_PAGE_UP": ("pageup",),
}
KEY_ALIASES = {
    "escape": "esc",
    "return": "enter",
    "super": "winleft",
    "del": "delete",
    "equal": "=",
    "equals": "=",
}
MODIFIERS = {"ctrl", "alt", "shift", "winleft"}
SINGLE_KEYS = (
    set("abcdefghijklmnopqrstuvwxyz0123456789")
    | {
        "esc",
        "enter",
        "tab",
        "space",
        "delete",
        "backspace",
        "pagedown",
        "pageup",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "winleft",
        ",",
        ".",
        "=",
        "-",
    }
    | {f"f{index}" for index in range(1, 13)}
)


def input_and_menu_context(accessibility_tree: str) -> tuple[list[dict], list[dict]]:
    inputs, menus = [], []

    def walk(element, active=False, menu_path=()):
        attrs = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
        role = element.tag.rsplit("}", 1)[-1].replace("-", "_")
        active = active or attrs.get("active") == "true"
        name = attrs.get("name", "")
        if role in {"menu", "menu_item"} and name:
            menu_path = (*menu_path, name)
            keyboard = attrs.get("click_kb", "").split(";")
            path = keyboard[1].split(":") if len(keyboard) > 1 else []
            if (
                active
                and path
                and re.fullmatch(r"<Alt>[a-z]", path[0], re.I)
                and all(re.fullmatch(r"[a-z]", part, re.I) for part in path[1:])
            ):
                menus.append(
                    {
                        "name": name,
                        "path": " > ".join(menu_path),
                        "role": role,
                        "key_sequence": [
                            ["esc"],
                            ["alt", path[0][-1].lower()],
                            *[[part.lower()] for part in path[1:]],
                        ],
                    }
                )
        if (
            active
            and (attrs.get("editable") == "true" or role == "terminal")
            and (attrs.get("showing") != "false" and attrs.get("enabled") != "false")
        ):
            numbers = re.findall(r"-?\d+", attrs.get("screencoord", "") + attrs.get("size", ""))
            inputs.append(
                {
                    "name": name or f"Focused {role}",
                    "role": role,
                    "focused": attrs.get("focused") == "true",
                    "bounds": [int(value) for value in numbers] if len(numbers) == 4 else None,
                }
            )
        for child in element:
            walk(child, active, menu_path)

    walk(ET.fromstring(accessibility_tree))
    return inputs, menus


def toggle_transition(before: str, after: str, target) -> dict | None:
    if target is None or target.role not in {"toggle_button", "check_box", "checkbox", "switch"}:
        return None

    def state(tree):
        matches = []
        for element in ET.fromstring(tree).iter():
            role = element.tag.rsplit("}", 1)[-1].replace("-", "_")
            if role == target.role and element.attrib.get("name") == target.name:
                attrs = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
                if attrs.get("showing") != "false":
                    matches.append(attrs)
        if len(matches) != 1:
            return None
        return next((matches[0][key] for key in ("checked", "pressed", "value") if key in matches[0]), None)

    first, second = state(before), state(after)
    if first is None or second is None:
        return None
    return {"target": target.name, "before": first, "after": second, "changed": first != second}


def action_key(candidate: ActionCandidate) -> str:
    metadata = candidate.metadata
    sequence = metadata.get("key_sequence")
    if candidate.operation == "keypress" and candidate.id in KEYS:
        sequence = [list(KEYS[candidate.id])]
    return json.dumps(
        [
            candidate.operation,
            *[
                metadata.get(key)
                for key in ("semantic_key", "visual_point", "visual_end", "text", "input_mode", "fraction")
            ],
            sequence,
            candidate.id if not metadata and sequence is None else None,
        ],
        sort_keys=True,
    )


def prune_stalled(space, recent: list[dict]) -> None:
    if len(recent) < 2 or any(item.get("state_changed") is not False for item in recent[-2:]):
        return
    blocked = recent[-1].get("action_key")
    if not blocked or blocked != recent[-2].get("action_key"):
        return
    space.target_heads = {
        head: [item for item in items if action_key(item) != blocked]
        for head, items in space.target_heads.items()
    }
    space.controls = {key: item for key, item in space.controls.items() if action_key(item) != blocked}
