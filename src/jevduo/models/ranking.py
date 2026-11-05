"""JEV/Kev 共用的 choice 请求、排序结果及事件序列化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..gui.candidates import OPERATION_DESCRIPTIONS, ActionCandidate
from ..gui.targets import UITarget


@dataclass(slots=True)
class Ranking:
    winner: ActionCandidate | None
    action_ranked: list[dict[str, Any]]
    operation_ranked: list[dict[str, Any]]
    operation_margin: float
    target_margin: float
    latency_ms: float
    used_image: bool
    confident: bool = True
    operation_request: dict[str, Any] | None = None
    action_request: dict[str, Any] | None = None
    score_type: str = "fast_model_choice_softmax"


def build_choice_requests(
    *,
    stage_goal: str,
    intent: str,
    active_window: str,
    targets: list[UITarget],
    action_space,
    recent_actions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    candidates = action_space.candidates()
    offered_target_ids = {candidate.target_id for candidate in candidates}
    control_fields = {
        "id", "role", "name", "description", "value", "focused", "checked", "selected", "application",
        "scope", "nearby_text", "parent_context", "x", "y", "width", "height", "source", "label_source",
    }
    controls = {
        target.id: {
            key: value
            for key, value in target.compact().items()
            if key in control_fields and not (key == "value" and target.role == "password_text")
        }
        for target in targets
        if target.id in offered_target_ids
    }
    recent = [
        {
            key: item[key]
            for key in ("operation", "target", "goal", "outcome", "state_changed")
            if key in item
        }
        for item in recent_actions[-10:]
    ]
    state = json.dumps(
        {
            "local_goal": intent,
            "active_window": active_window,
            "current_ui_controls": list(controls.values()),
            "recent_semantic_actions": recent,
            "stage_goal": stage_goal,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    operations = list(dict.fromkeys(candidate.operation for candidate in candidates))
    operation_request = {
        "state": state,
        "question": "Which operation should be performed next?",
        "option_ids": operations,
        "options": [
            f"Choose operation {operation}: {OPERATION_DESCRIPTIONS.get(operation, operation)}"
            for operation in operations
        ],
    }
    action_requests = {}
    for operation in operations:
        selected = [candidate for candidate in candidates if candidate.operation == operation]
        options = []
        for candidate in selected:
            description = candidate.description
            target = controls.get(candidate.target_id)
            if target:
                context = " > ".join(target.get("parent_context", [])[-2:])
                details = []
                if target.get("description"):
                    details.append(f"Details: {target['description'][:160]}")
                nearby = target.get("nearby_text", "")
                if nearby and nearby.casefold() != target.get("name", "").casefold():
                    details.append(f"Nearby: {nearby[:240]}")
                if target.get("value"):
                    details.append(f"Current value: {str(target['value'])[:160]}")
                if target.get("scope"):
                    details.append(f"Scope: {target['scope']}")
                description += (
                    (" " + "; ".join(details) + ";" if details else "")
                    + f" Parent context: {context};"
                    f" screen box ({target.get('x')}, {target.get('y')},"
                    f" {target.get('width')}, {target.get('height')})."
                )
            options.append(description)
        action_requests[operation] = {
            "state": state,
            "question": "Which concrete action should be performed for the selected operation?",
            "option_ids": [candidate.id for candidate in selected],
            "options": options,
        }
    return operation_request, action_requests


def ranking_dict(value: Ranking) -> dict[str, Any]:
    return {
        "action_ranked": value.action_ranked,
        "operation_ranked": value.operation_ranked,
        "winner": value.winner.compact() if value.winner else None,
        "operation_margin": value.operation_margin,
        "target_margin": value.target_margin,
        "latency_ms": value.latency_ms,
        "used_image": value.used_image,
        "score_type": value.score_type,
        "score_is_calibrated_correctness_probability": False,
        "operation_request": value.operation_request,
        "action_request": value.action_request,
    }
