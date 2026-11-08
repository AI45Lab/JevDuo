"""Badge text helpers for history action overlays."""

from __future__ import annotations

from northcua.agentkernel.actions.protocol import ActionCommandInfo


class OverlayBadgeTextFormatter:
    """Format compact badge text for replay overlays."""

    _KEY_ALIASES = {
        "ctrl": "Ctrl",
        "control": "Ctrl",
        "cmd": "Cmd",
        "command": "Cmd",
        "meta": "Cmd",
        "win": "Win",
        "alt": "Alt",
        "option": "Alt",
        "shift": "Shift",
        "enter": "Enter",
        "return": "Enter",
        "tab": "Tab",
        "space": "Space",
        "escape": "Esc",
        "esc": "Esc",
        "backspace": "Backspace",
        "delete": "Delete",
        "home": "Home",
        "end": "End",
        "pageup": "PageUp",
        "pagedown": "PageDown",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
    }

    def __init__(self, *, badges_enabled: bool, max_badge_text: int) -> None:
        """Initialize badge formatting policy.

        Args:
            badges_enabled: Whether badge text should be emitted.
            max_badge_text: Maximum badge text length before truncation.
        """

        self.badges_enabled = badges_enabled
        self.max_badge_text = max(max_badge_text, 4)

    def badge_text(self, text: str, allow_empty: bool = True) -> str:
        """Normalize badge text according to the current badge settings.

        Args:
            text: Raw badge text.
            allow_empty: Whether empty text should return an empty badge.

        Returns:
            Truncated badge text, or an empty string when badges are disabled.
        """

        if not self.badges_enabled:
            return ""
        normalized = str(text or "").strip()
        if not normalized and allow_empty:
            return ""
        if len(normalized) <= self.max_badge_text:
            return normalized
        return normalized[: self.max_badge_text - 3].rstrip() + "..."

    def scroll_badge_text(self, *, delta_x: int, delta_y: int) -> str:
        """Format badge text for scroll actions without a spatial anchor.

        Args:
            delta_x: Horizontal scroll delta.
            delta_y: Vertical scroll delta.

        Returns:
            Human-readable scroll badge text.
        """

        directions: list[str] = []
        if delta_y > 0:
            directions.append("up")
        elif delta_y < 0:
            directions.append("down")
        if delta_x > 0:
            directions.append("right")
        elif delta_x < 0:
            directions.append("left")
        if not directions:
            return self.badge_text("Scroll", allow_empty=False)
        return self.badge_text(
            "Scroll " + " + ".join(direction.title() for direction in directions),
            allow_empty=False,
        )

    def keypress_badge_text(self, action: ActionCommandInfo) -> str:
        """Format badge text for a keypress action.

        Args:
            action: Keypress action command.

        Returns:
            Human-readable keypress badge text.
        """

        arguments = action.arguments or {}
        raw_keys = arguments.get("keys")
        if isinstance(raw_keys, (list, tuple)):
            normalized_keys = [self.display_key(key) for key in raw_keys]
        else:
            normalized_keys = [self.display_key(arguments.get("key"))]
        normalized_keys = [key for key in normalized_keys if key]
        if not normalized_keys:
            return "Keypress"
        return "Keypress: " + "+".join(normalized_keys)

    @staticmethod
    def type_badge_text(action: ActionCommandInfo) -> str:
        """Format badge text for a type action.

        Args:
            action: Type action command.

        Returns:
            Human-readable type badge text.
        """

        text_value = str((action.arguments or {}).get("text") or "")
        return "Type: " + text_value

    @staticmethod
    def wait_badge_text(action: ActionCommandInfo) -> str:
        """Format badge text for a wait action.

        Args:
            action: Wait action command.

        Returns:
            Human-readable wait badge text.
        """

        milliseconds = action.arguments.get("ms") if action.arguments else 0
        try:
            milliseconds = int(round(float(milliseconds)))
        except (TypeError, ValueError):
            milliseconds = 0
        return f"Wait {milliseconds}ms"

    @staticmethod
    def move_badge_text(action: ActionCommandInfo) -> str:
        """Format badge text for a pointer move action.

        Args:
            action: Move action command.

        Returns:
            Human-readable pointer movement badge text.
        """

        arguments = action.arguments or {}
        try:
            x_value = int(round(float(arguments.get("x", 0))))
            y_value = int(round(float(arguments.get("y", 0))))
        except (TypeError, ValueError):
            x_value = 0
            y_value = 0
        return f"Move to ({x_value}, {y_value})"

    def display_key(self, key: object) -> str:
        """Convert one raw key token into a human-readable label.

        Args:
            key: Raw key token from action arguments.

        Returns:
            Display label for badge text.
        """

        normalized = str(key or "").strip()
        if not normalized:
            return ""
        lookup_key = normalized.lower().replace("_", "")
        if lookup_key in self._KEY_ALIASES:
            return self._KEY_ALIASES[lookup_key]
        if len(normalized) == 1:
            return normalized.upper()
        return normalized.replace("_", " ").title()
