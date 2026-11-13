"""Bounded native evidence returned to Slow, including interrupted child progress."""

from __future__ import annotations

import json


def feedback(events: list[dict], *, execution_diagnostics: list[dict] | None = None) -> dict:
    diagnostics = (execution_diagnostics or [])[-8:]
    terminal = events[-1] if events else {}
    turns = [event["turn"] for event in events if "turn" in event]
    screen = terminal.get("screen") or next((event["screen"] for event in reversed(events) if event.get("screen")), {})
    error = terminal.get("error")
    last = turns[-1] if turns else {}
    snapshot = next((entry for entry in reversed(diagnostics) if "snapshot_timeout_ms" in entry), {})
    unsafe = [
        {key: turn.get(key) for key in ("operation", "target", "ref", "delivery")}
        for turn in turns
        if turn.get("delivery") in ("delivered_unverified", "delivery_uncertain")
        and (
            not turn.get("ok")
            or turn.get("changed") is None
            or (
                turn.get("operation") in {"TYPE_TEXT", "INSERT_TEXT", "CLEAR", "SELECT", "LAUNCH"}
                and turn.get("changed") is not True
            )
        )
    ]
    return {
        "stop": terminal.get("stop", "no_terminal_event"),
        "error": error,
        "execution_diagnostics": diagnostics,
        "application_selections": [
            event["application_selection"] for event in events if "application_selection" in event
        ],
        "turns": turns[-40:],
        "turns_truncated": len(turns) > 40,
        "screen": screen,
        "decision_probabilities": terminal.get("why"),
        "confidence": terminal.get("confidence"),
        "available_operations": terminal.get("available_operations", []),
        "blocked_reason": (error or {}).get("code") or terminal.get("stop"),
        "blocker": terminal.get("blocker"),
        "last_operation": last.get("operation"),
        "unsafe_to_replay": unsafe,
        "unresolved_delivery": any("[redacted]" in str(action.get("target")) for action in unsafe),
        "recovery": {
            "must_reobserve": bool(unsafe or error),
            "snapshot_timeout_ms": snapshot.get("snapshot_timeout_ms", screen.get("snapshot_timeout_ms")),
            "unaddressable_targets": screen.get("unaddressable_targets", []),
        },
    }


def bounded_json(value: dict, limit: int = 32000) -> str:
    """Keep valid JSON while bounding untrusted screen text and provider error details."""

    def bounded(item):
        if isinstance(item, str):
            return item[:2000]
        if isinstance(item, list):
            return [bounded(entry) for entry in item[:200]]
        if isinstance(item, dict):
            return {key: bounded(entry) for key, entry in item.items()}
        return item

    unsafe = value.get("unsafe_to_replay", [])
    if len(unsafe) > 200 or any(len(str(action.get("target", ""))) > 2000 for action in unsafe):
        value = {**value, "unresolved_delivery": True}
    value = bounded(value)
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > limit:
        value["screen"] = {key: item for key, item in value.get("screen", {}).items() if key != "visible_context"}
        value["context_truncated"] = True
        value["turns"] = value.get("turns", [])[-8:]
        text = json.dumps(value, ensure_ascii=False)
    if len(text) > limit:
        screen_id = value.get("screen", {}).get("window_id")
        value = {
            key: value.get(key)
            for key in (
                "stop",
                "blocked_reason",
                "last_operation",
                "unsafe_to_replay",
                "unresolved_delivery",
                "execution_diagnostics",
            )
        }
        value["screen"] = {"window_id": screen_id} if screen_id else {}
        value["evidence_truncated"] = True
        text = json.dumps(value, ensure_ascii=False)
    if len(text) > limit:
        value["unsafe_to_replay"] = []
        value["unresolved_delivery"] = True
        text = json.dumps(value, ensure_ascii=False)
    return text
