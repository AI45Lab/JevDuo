"""Compare native failures across contracts without treating new IDs as progress."""

from __future__ import annotations

import json


def unreconciled_actions(history: list[dict]) -> tuple[dict, ...]:
    blocked = []
    for cycle in history:
        result = cycle.get("subagent_result") or {}
        try:
            summary = json.loads(result.get("summary", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(summary, dict):
            continue
        if summary.get("unresolved_delivery"):
            blocked.append({"operation": "*", "target": "*", "window_id": summary.get("screen", {}).get("window_id")})
        for action in summary.get("unsafe_to_replay", []):
            blocked.append({**action, "window_id": summary.get("screen", {}).get("window_id")})
    return tuple(blocked)


def repeated_blocker(history: list[dict], agent_input: dict, threshold: int = 3) -> dict | None:
    matches = []
    for cycle in reversed(history):
        decision = cycle.get("decision") or {}
        contract = decision.get("contract") or {}
        if contract.get("agent_id") != "gui_jev":
            break
        previous_input = contract.get("agent_input") or {}
        scope = ("app", "window_id", "root", "snapshot_timeout_ms", "input_mode", "allowed_operations", "keys")
        if any(agent_input.get(key) != previous_input.get(key) for key in scope):
            break
        result = cycle.get("subagent_result") or {}
        if result.get("outcome") == "achieved":
            break
        try:
            summary = json.loads(result.get("summary", ""))
        except (ValueError, TypeError):
            break
        if not isinstance(summary, dict):
            break
        turns = summary.get("turns", [])
        if any(turn.get("ok") and turn.get("changed") for turn in turns):
            break
        screen = summary.get("screen", {})
        last = turns[-1] if turns else {}
        key = summary.get("blocker") or {
            "blocker": summary.get("blocked_reason") or summary.get("stop"),
            "operation": last.get("operation"),
            "target": last.get("target"),
            "app": screen.get("app"),
            "window": screen.get("window"),
            "context": screen.get("visible_context"),
        }
        if matches and key != matches[-1]:
            break
        matches.append(key)
        if len(matches) >= threshold:
            return key
    return None
