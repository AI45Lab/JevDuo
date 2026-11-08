"""Shared model for desktop MCP tool-surface registrations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from northcua_protocol.capabilities import ToolDescriptor


class TextTargetKind(str, Enum):
    """Focused editable-control kind used by the desktop ``type`` tool.

    The value is model-visible because different focused controls require
    different paste shortcuts, especially on Linux. ``AUTO`` preserves the
    historical best-effort behavior for old traces and callers.
    """

    AUTO = "auto"
    EDITABLE = "editable"
    TERMINAL = "terminal"

    @classmethod
    def coerce(cls, value: object) -> "TextTargetKind":
        """Return a target kind from a model/runtime supplied value.

        Args:
            value: String or enum value supplied by a caller.

        Returns:
            Normalized text target kind.

        Raises:
            ValueError: If the value is not a supported target kind.
        """

        if isinstance(value, cls):
            return value
        normalized = str(value or cls.AUTO.value).strip().lower()
        return cls(normalized)


TYPE_TEXT_DESCRIPTION = (
    "Literal text to enter into the currently focused editable control. "
    "Make sure the intended field is focused; for documents, editors, "
    "browser inputs, and terminal prompts, click or otherwise focus the "
    "exact insertion point before calling this tool. Use `target_kind` "
    "to choose paste semantics: `auto` preserves the legacy best-effort "
    "behavior; `editable` is for normal editable controls such as browser "
    "fields, LibreOffice cells/documents, filename dialogs, and code editors "
    "and uses the ordinary paste shortcut; `terminal` is only for shell "
    "prompts, REPLs, terminal emulators, and IDE-integrated terminals and "
    "uses the terminal paste shortcut on Linux. Do not choose `terminal` for "
    "LibreOffice or normal editors because it may trigger Paste Special or "
    "other shortcuts. Do not choose `editable` for Linux terminals because "
    "Ctrl+V may insert a literal ^V instead of pasting. Avoid pasting large "
    "blocks directly into terminals: newlines may execute early, some terminal "
    "apps show Unsafe Paste confirmation prompts, and terminal paste semantics "
    "vary across environments. For large multi-line code, JSON/YAML, here-docs, "
    "or REPL sessions, prefer opening a file, writing the content there, saving "
    "it, and then executing the file or command; otherwise use "
    "`editable`/`terminal` deliberately and verify the next screenshot."
)

TYPE_TARGET_KIND_DESCRIPTION = (
    "Focused control kind for text entry. `auto` keeps backward-compatible "
    "best-effort behavior. `editable` means a normal editable control, "
    "document, form field, browser input, filename dialog, or code editor; "
    "the backend uses ordinary paste semantics. `terminal` means a shell, "
    "REPL, terminal emulator, or IDE-integrated terminal; on Linux the "
    "backend uses terminal paste semantics such as Ctrl+Shift+V. If you are "
    "not in a terminal, choose `editable`."
)

KEYPRESS_EXACTLY_ONE_ERROR = "keypress action requires exactly one of `key` or `keys`."

KEYPRESS_TOOL_DESCRIPTION = (
    "Press one key or a key chord/ordered key sequence. Provide exactly "
    "one of `key` or `keys`. In `keys`, each array item is one ordered "
    'press unit. Use `keys=["ctrl+l", "ctrl+l", "tab"]` to send '
    'Ctrl+L, Ctrl+L, then Tab; use `keys=["ctrl", "shift", "p"]` '
    "only when you mean one legacy chord. Prefer platform-native "
    "modifier names in the tool call itself: on macOS prefer `cmd`; "
    "on Windows or Linux prefer `win`. Aliases such as `meta`, "
    "`command`, `super`, and `windows` are accepted. Make sure the "
    "intended target has focus before sending shortcuts. Use this for "
    "shortcuts, navigation, enter/escape, repeated chords, and explicit "
    "symbol key presses. Returns plain-text feedback plus the post-action "
    "screenshot path and format."
)

KEYPRESS_KEY_DESCRIPTION = "Single key to press. Use this only when `keys` is omitted."

KEYPRESS_KEYS_DESCRIPTION = (
    "Ordered key press units. Use this only when `key` is omitted. Each array "
    "item is one independent press unit fired in order. Put `+` inside a "
    'single item for a simultaneous chord: `keys=["ctrl+l", "ctrl+l", '
    '"tab"]` sends Ctrl+L, Ctrl+L, then Tab. Legacy '
    '`keys=["ctrl", "shift", "p"]` is accepted as one Ctrl+Shift+P '
    "chord. Use platform-native modifiers: `cmd` on macOS, `win` on "
    "Windows/Linux."
)


@dataclass(frozen=True, slots=True)
class DesktopToolRegistration:
    """Binding between one desktop MCP tool descriptor and its server name.

    Attributes:
        server_name: MCP server that owns the tool.
        descriptor: Protocol descriptor mirrored from the real MCP surface.
    """

    server_name: str
    descriptor: ToolDescriptor


__all__ = [
    "DesktopToolRegistration",
    "KEYPRESS_EXACTLY_ONE_ERROR",
    "KEYPRESS_KEY_DESCRIPTION",
    "KEYPRESS_KEYS_DESCRIPTION",
    "KEYPRESS_TOOL_DESCRIPTION",
    "TYPE_TARGET_KIND_DESCRIPTION",
    "TYPE_TEXT_DESCRIPTION",
    "TextTargetKind",
]
