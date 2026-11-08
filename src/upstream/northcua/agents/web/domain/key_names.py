"""Canonical Web keyboard key names shared by action backends.

The model-facing ``web_press`` contract accepts common browser key aliases, but
the executor side needs one stable spelling so local and remote Playwright
sessions behave identically.
"""

from __future__ import annotations

_KEY_ALIASES: dict[str, str] = {
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "arrowup": "ArrowUp",
    "backspace": "Backspace",
    "cmd": "Meta",
    "command": "Meta",
    "control": "Control",
    "ctrl": "Control",
    "delete": "Delete",
    "del": "Delete",
    "down": "ArrowDown",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "left": "ArrowLeft",
    "meta": "Meta",
    "option": "Alt",
    "alt": "Alt",
    "return": "Enter",
    "right": "ArrowRight",
    "shift": "Shift",
    "space": "Space",
    "spacebar": "Space",
    "tab": "Tab",
    "up": "ArrowUp",
}


def canonical_web_key_name(key: str) -> str:
    """Return a browser-executor key name with stable alias handling.

    The helper preserves single-character keys and normalizes each segment of a
    Playwright-style key chord independently, e.g. ``ctrl+left`` becomes
    ``Control+ArrowLeft``.
    """

    raw_key = key.strip()
    if not raw_key:
        return key
    if "+" not in raw_key:
        return _canonical_key_part(raw_key)
    parts = [_canonical_key_part(part.strip()) for part in raw_key.split("+") if part.strip()]
    return "+".join(parts) if parts else raw_key


def _canonical_key_part(part: str) -> str:
    if len(part) == 1:
        return part
    return _KEY_ALIASES.get(part.replace(" ", "").lower(), part)
