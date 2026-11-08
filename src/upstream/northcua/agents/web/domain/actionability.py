"""Shared actionability feedback for web actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class WebActionabilityFeedback:
    """Concise model-facing feedback extracted from browser action logs."""

    code: str
    hint: str
    concise_log_summary: str = ""
    feedback_source: str = "playwright"


_ACTIONABILITY_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "element_not_receiving_events",
        (
            "intercepts pointer events",
            "does not receive pointer events",
            "subtree intercepts pointer events",
            "not receiving pointer events",
            "pointer-events:none",
            "pointer-events: none",
        ),
        "The target is covered or is a pass-through overlay container. Choose the real visible control, close the blocker, scroll, or capture a fresh snapshot.",
    ),
    (
        "element_disabled",
        ("element is not enabled", "not enabled", "disabled", "aria-disabled"),
        "The target is disabled. Do not retry the same ref; satisfy the enabling condition or choose another target.",
    ),
    (
        "element_not_visible",
        ("element is not visible", "not visible", "element is hidden", "not in viewport"),
        "The target is not currently visible. Scroll, wait for it to appear, or choose a visible ref from a fresh snapshot.",
    ),
    (
        "wrong_action_for_element",
        (
            "not a checkbox",
            "not a radio button",
            "not an editable element",
            "element is not editable",
            "element is not an <input>",
            "element is not an input",
            "element is not a <textarea>",
            "element is not a textarea",
            "element is not contenteditable",
            "not a <select> element",
            "not a select element",
        ),
        "Use the latest observation to pick an action that matches the element role, such as fill_ref for inputs or click_ref for links/buttons.",
    ),
)


def classify_actionability_message(message: str, *, source: str = "playwright") -> WebActionabilityFeedback | None:
    """Return actionability feedback when ``message`` contains a known cause."""

    lower_message = message.lower()
    for code, patterns, hint in _ACTIONABILITY_PATTERNS:
        if any(pattern in lower_message for pattern in patterns):
            return WebActionabilityFeedback(
                code=code,
                hint=hint,
                concise_log_summary=concise_log_summary(message, patterns),
                feedback_source=source,
            )
    return None


def concise_log_summary(message: str, patterns: tuple[str, ...] = ()) -> str:
    """Return the most relevant single-line browser failure summary."""

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    lowered_patterns = tuple(pattern.lower() for pattern in patterns if pattern)
    for line in lines:
        lower_line = line.lower()
        if any(pattern in lower_line for pattern in lowered_patterns):
            return _truncate(line)
    for line in lines:
        lower_line = line.lower()
        if "locator resolved to" in lower_line or "waiting for" in lower_line:
            return _truncate(line)
    if not lines:
        return ""
    return _truncate(lines[0])


def _truncate(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
