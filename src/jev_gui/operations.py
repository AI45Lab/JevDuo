"""Compile planner-owned GUI operations into bounded, single-intent requests."""

from __future__ import annotations

from datetime import date


def operation_request(card, operation, index, remaining):
    """Keep values and control identity attached while the local executor advances."""
    kind = operation["kind"]
    request = {
        "goal": operation["intent"],
        "success_condition": operation["success_condition"],
        "operation_kind": kind,
        "control_hint": operation.get("target_hint", ""),
        "control_id": operation.get("target_id", ""),
        "operation_index": index,
        "parent_goal": card["goal"],
        "max_actions": min(remaining, operation.get("max_actions", 12)),
        "mode": "reactive" if kind == "reactive" else "semantic",
        "visual_targets": operation.get("visual_targets", []),
        "allowed_keys": operation.get("allowed_keys", ["tab", "shift+tab", "enter", "esc"]),
        "commit_targets": operation.get("commit_targets", []),
        "command_keys": operation.get("keys", []),
        "file_paths": operation.get("paths", []),
        "drag_path": operation.get("drag_path", []),
    }
    if kind in {"fill", "select"}:
        request["inputs"] = [
            {
                "field": operation["target_hint"],
                "text": operation["text"],
                "target_hint": operation["target_hint"],
                "target_id": operation.get("target_id", ""),
                "target_kind": operation.get("target_kind", "editable"),
                "kind": kind,
            }
        ]
    return request


def date_keys(target, text):
    """Use observed native segment order; never guess from a locale or a task ID."""
    try:
        expected = date.fromisoformat(text)
    except ValueError:
        return None
    if len(target.date_order) != 3 or set(target.date_order) != {"year", "month", "day"}:
        return None
    parts = {"year": f"{expected.year:04}", "month": f"{expected.month:02}", "day": f"{expected.day:02}"}
    sequence = []
    for index, part in enumerate(target.date_order):
        if index:
            sequence.append("right")
        sequence.extend(parts[part])
    return sequence + ["tab"]


def matching_value(expected, observed, order=()):
    """Native date controls can display a localized date while DOM reports ISO."""
    if expected == observed:
        return True
    try:
        parsed = date.fromisoformat(expected)
    except ValueError:
        return False
    parts = {"year": f"{parsed.year:04}", "month": f"{parsed.month:02}", "day": f"{parsed.day:02}"}
    return bool(order) and observed == "/".join(parts[part] for part in order)
