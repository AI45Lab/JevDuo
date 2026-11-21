"""Context passed to an independently supplied text generator."""

import copy
from dataclasses import dataclass
from typing import Any

from .policy import HISTORY_FIELDS
from .ref_diagnostics import summarize_refs
from .screen import describe, fingerprint, target_identity


@dataclass(frozen=True)
class TextRequest:
    goal: str
    field: dict[str, Any]
    screen: dict[str, Any]
    history: list[dict[str, Any]]
    operation: str = "TYPE_TEXT"
    reference_context: dict | None = None


def same_text_window(before, after):
    return (
        bool(before.screen.get("app") and before.screen.get("window_id"))
        and all(after.screen.get(key) == before.screen.get(key) for key in ("app", "window_id", "root"))
        and after.screen.get("complete") is True
        and not after.screen.get("unclassified_surfaces")
    )


def unchanged_text_target(before, after, target):
    for key in ("app", "window_id", "window", "surface", "root"):
        if after.screen.get(key) != before.screen.get(key):
            return False
    if after.screen.get("complete") is not True or after.screen.get("unclassified_surfaces"):
        return False
    matches = [node for node in after.nodes if target_identity(node) == target_identity(target)]
    return (
        len(matches) == 1
        and matches[0].get("ref_actionable") is not False
        and all(
            matches[0].get(key) == target.get(key)
            for key in ("value", "states", "available_actions", "description", "placeholder")
        )
    )


def build_text_request(goal, screen, node, history, *, values=True):
    field = copy.deepcopy(describe(node, True, values=values))
    if screen.get("clipboard_text"):
        field["input_constraints"] = {
            "route": "native_then_guarded_clipboard_replace",
            "literal_text_only": True,
            "guidance": (
                "TYPE_TEXT requests the exact complete field value, including required line breaks and tabs. "
                "After a positively undelivered unsupported native replacement, the executor may use pinned "
                "clipboard paste only with a complete readable original value, proven focus and full selection, "
                "then exact readback. No insertion fallback or key instructions encoded as text are supported. "
                "A field value readback does not establish form submission, persistence, or goal completion."
            ),
        }
    elif screen.get("headed_text") and not {"SetValue", "TypeText"}.intersection(node.get("available_actions", [])):
        field["input_constraints"] = {
            "route": "physical_keyboard",
            "literal_text_only": True,
            "guidance": (
                "This field has no native semantic text action. Keyboard text rejects navigation control "
                "characters; line feeds need separate native multiline proof. Do not encode key presses "
                "as field text. Use ordinary spaces for incidental indentation, and single-line JSON or "
                "other equivalent single-line content when line breaks are not required by the goal. "
                "Preserve explicitly requested formatting; decline if safe literal content is unavailable."
            ),
        }
    context = [
        {key: value for key, value in entry.items() if values or key != "value"}
        for entry in screen.get("context", [])
    ]
    presented = {key: value for key, value in screen.items() if key not in {"context", "unaddressable_targets"}}
    diagnostics = summarize_refs(screen, values=values)
    if diagnostics:
        presented["ref_diagnostics"] = diagnostics
    return TextRequest(
        goal=goal,
        field=field,
        screen=copy.deepcopy(
            {**presented, "visible_context": context}
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
            "SELECT and SELECT_ITEM bind observed options and never ask for generated text. "
            "TYPE_TEXT replaces the whole field. INSERT_TEXT types at its current caret or into its "
            "current selection; it does not select or clear the field. Inspect the observed selection "
            "and original goal before choosing the text operation. "
            "To enter a new name, path, search value or other field content, choose the editable textfield or "
            "combobox and a text operation. SELECT only picks an existing listed option; it cannot supply a "
            "new value. Navigating an existing folder does not name or save a new file."
        ),
    }
    operations = request["questions"].get("operation", {}).get("criteria", {})
    if "TYPE_TEXT" in operations:
        operations["TYPE_TEXT"] = "Generate the complete intended field value from the goal, then replace the whole field."
    if "INSERT_TEXT" in operations:
        operations["INSERT_TEXT"] = (
            "Generate text for the observed caret or current selected range, then type it there. "
            "This does not select or clear the whole field. Verify the selection scope first."
        )
    return request


def capture_text_state(observation):
    screen = copy.deepcopy(observation.screen)
    if "unaddressable_targets" in screen:
        screen["unaddressable_targets"] = [
            {key: value for key, value in node.items() if key != "ref_id"}
            for node in screen["unaddressable_targets"]
        ]
    return copy.deepcopy(
        {
            "screen": screen,
            "elements": fingerprint(observation.nodes),
            "descriptions": [describe(node, True) for node in observation.nodes],
        }
    )
