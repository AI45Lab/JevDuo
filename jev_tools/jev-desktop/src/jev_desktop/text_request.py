"""Context passed to an independently supplied text generator."""

import copy
from dataclasses import dataclass
from typing import Any

from .policy import HISTORY_FIELDS
from .screen import describe, fingerprint


@dataclass(frozen=True)
class TextRequest:
    goal: str
    field: dict[str, Any]
    screen: dict[str, Any]
    history: list[dict[str, Any]]
    operation: str = "TYPE_TEXT"


def build_text_request(goal, screen, node, history, *, values=True):
    context = [
        {key: value for key, value in entry.items() if values or key != "value"}
        for entry in screen.get("context", [])
    ]
    return TextRequest(
        goal=goal,
        field=copy.deepcopy(describe(node, True, values=values)),
        screen=copy.deepcopy(
            {**{key: value for key, value in screen.items() if key != "context"}, "visible_context": context}
        ),
        history=copy.deepcopy(
            [{key: turn[key] for key in HISTORY_FIELDS if key in turn} for turn in history[-8:]]
        ),
    )


def offer_text_model(request):
    request["state"]["text_input"] = {
        "source": "text_model",
        "instructions": (
            "Only TYPE_TEXT and INSERT_TEXT ask the text model for input from the goal and screen. "
            "SELECT binds an observed option and never asks for generated text."
        ),
    }
    operations = request["questions"].get("operation", {}).get("criteria", {})
    if "TYPE_TEXT" in operations:
        operations["TYPE_TEXT"] = "Generate the text needed for this field from the goal, then enter it."
    return request


def capture_text_state(observation):
    return copy.deepcopy(
        {
            "screen": observation.screen,
            "elements": fingerprint(observation.nodes),
            "descriptions": [describe(node, True) for node in observation.nodes],
        }
    )
