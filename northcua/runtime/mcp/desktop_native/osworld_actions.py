"""Input action helpers for the OSWorld desktop backend."""

from __future__ import annotations

import json
import textwrap
from typing import Optional, Sequence, cast

from northcua_runtime_shared.desktop_surface.models import TextTargetKind

from ._models import DesktopNativeError, DesktopNativePoint
from ._types import JsonObject


class OSWorldActionMixin:
    """Mouse and keyboard operations executed through remote pyautogui."""

    command_timeout_seconds: float

    def click(self, x: float, y: float, button: str = "left") -> str:
        """Click a coordinate in the OSWorld VM.

        Args:
            x: X coordinate in screenshot/runtime pixels.
            y: Y coordinate in screenshot/runtime pixels.
            button: Mouse button name.

        Returns:
            Human-readable action result.
        """

        button = self._normalize_button(button)
        self._run_pyautogui(
            f"pyautogui.click({int(round(x))}, {int(round(y))}, button={button!r})",
        )
        return f"Clicked at ({int(round(x))}, {int(round(y))}) with {button} button."

    def double_click(self, x: float, y: float, button: str = "left") -> str:
        """Double-click a coordinate in the OSWorld VM.

        Args:
            x: X coordinate in screenshot/runtime pixels.
            y: Y coordinate in screenshot/runtime pixels.
            button: Mouse button name.

        Returns:
            Human-readable action result.
        """

        button = self._normalize_button(button)
        self._run_pyautogui(
            (f"pyautogui.click({int(round(x))}, {int(round(y))}, button={button!r}, clicks=2, interval=0.1)"),
        )
        return f"Double-clicked at ({int(round(x))}, {int(round(y))}) with {button} button."

    def drag(self, path: Sequence[DesktopNativePoint], button: str = "left") -> str:
        """Drag through a sequence of points.

        Args:
            path: Ordered drag path.
            button: Mouse button to hold during the drag.

        Returns:
            Human-readable action result.

        Raises:
            DesktopNativeError: If fewer than two points are provided.
        """

        if len(path) < 2:
            raise DesktopNativeError("drag action requires at least two points.")
        button = self._normalize_button(button)
        serialized_path = json.dumps([[int(round(point.x)), int(round(point.y))] for point in path])
        script = textwrap.dedent(f"""\
            import json
            import pyautogui

            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            points = json.loads({serialized_path!r})
            start_x, start_y = points[0]
            pyautogui.moveTo(start_x, start_y)
            pyautogui.mouseDown(button={button!r})
            try:
                for x, y in points[1:]:
                    pyautogui.moveTo(x, y)
            finally:
                pyautogui.mouseUp(button={button!r})
            """)
        self._run_python(script, timeout=self.command_timeout_seconds)
        start = path[0]
        end = path[-1]
        return (
            f"Dragged with {button} button from ({int(round(start.x))}, {int(round(start.y))}) "
            f"to ({int(round(end.x))}, {int(round(end.y))})."
        )

    def mouse_down(self, button: str = "left") -> str:
        """Press and hold a mouse button.

        Args:
            button: Mouse button name.

        Returns:
            Human-readable action result.
        """

        button = self._normalize_button(button)
        self._run_pyautogui(f"pyautogui.mouseDown(button={button!r})")
        return f"Pressed and held {button} mouse button."

    def mouse_up(self, button: str = "left") -> str:
        """Release a mouse button.

        Args:
            button: Mouse button name.

        Returns:
            Human-readable action result.
        """

        button = self._normalize_button(button)
        self._run_pyautogui(f"pyautogui.mouseUp(button={button!r})")
        return f"Released {button} mouse button."

    def move(self, x: float, y: float) -> str:
        """Move the pointer to a coordinate.

        Args:
            x: X coordinate in screenshot/runtime pixels.
            y: Y coordinate in screenshot/runtime pixels.

        Returns:
            Human-readable action result.
        """

        self._run_pyautogui(f"pyautogui.moveTo({int(round(x))}, {int(round(y))})")
        return f"Moved pointer to ({int(round(x))}, {int(round(y))})."

    def scroll(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> str:
        """Scroll at an optional coordinate.

        Args:
            x: Optional X coordinate to move to before scrolling.
            y: Optional Y coordinate to move to before scrolling.
            delta_x: Horizontal scroll delta.
            delta_y: Vertical pyautogui scroll delta; positive scrolls up,
                negative scrolls down.

        Returns:
            Human-readable action result.
        """

        mouse_x = int(round(x)) if x is not None else None
        mouse_y = int(round(y)) if y is not None else None
        lines: list[str] = []
        if mouse_x is not None and mouse_y is not None:
            lines.append(f"pyautogui.moveTo({mouse_x}, {mouse_y})")
        if delta_y:
            lines.append(f"pyautogui.vscroll({int(round(delta_y))})")
        if delta_x:
            lines.append(f"pyautogui.hscroll({int(round(delta_x))})")
        if not lines:
            return "Scrolled (delta_x=0, delta_y=0)."
        self._run_pyautogui("\n".join(lines))
        return f"Scrolled (delta_x={int(round(delta_x))}, delta_y={int(round(delta_y))})" + (
            f" at ({mouse_x}, {mouse_y})." if mouse_x is not None and mouse_y is not None else "."
        )

    def type_text(self, text: str, target_kind: TextTargetKind = TextTargetKind.AUTO) -> str:
        """Type text through pyautogui in the OSWorld VM.

        Args:
            text: Text to type.
            target_kind: Focused control kind used to choose paste semantics.

        Returns:
            Human-readable action result.
        """

        target_kind = TextTargetKind.coerce(target_kind)
        text = str(text or "")
        if target_kind == TextTargetKind.AUTO:
            self._run_pyautogui(f"pyautogui.write({text!r}, interval=0.01)")
        else:
            serialized = json.dumps(
                {
                    "text": text,
                    "target_kind": target_kind.value,
                }
            )
            self._run_python(
                _TYPE_TEXT_CLIPBOARD_SCRIPT.format(serialized=serialized),
                timeout=self.command_timeout_seconds,
            )
        return f"Typed {len(text)} character(s)."

    def key_down(self, key: str) -> str:
        """Press and hold one key.

        Args:
            key: User-facing key name.

        Returns:
            Human-readable action result.
        """

        normalized_key = self._normalize_single_key(key, action_name="key_down")
        self._run_pyautogui(f"pyautogui.keyDown({normalized_key!r})")
        return f"Pressed and held key: {normalized_key}."

    def key_up(self, key: str) -> str:
        """Release one key.

        Args:
            key: User-facing key name.

        Returns:
            Human-readable action result.
        """

        normalized_key = self._normalize_single_key(key, action_name="key_up")
        self._run_pyautogui(f"pyautogui.keyUp({normalized_key!r})")
        return f"Released key: {normalized_key}."

    def keypress(self, key: Optional[str] = None, keys: Optional[Sequence[str]] = None) -> str:
        """Press one key, chord, or key sequence.

        Args:
            key: Single key input.
            keys: Optional sequence or chord input.

        Returns:
            Human-readable action result.

        Raises:
            DesktopNativeError: If no key input is provided.
        """

        units = self._parse_chord_units(self._keypress_raw_keys(key, keys))
        if not units:
            raise DesktopNativeError("keypress action requires `key` or `keys`.")
        normalized_keys = [token for unit in units for token in unit]
        if self._legacy_array_is_single_chord(units):
            joined = ", ".join(repr(item) for item in normalized_keys)
            self._run_pyautogui(f"pyautogui.hotkey({joined}, interval=0.1)")
            return f"Pressed keys: {'+'.join(normalized_keys)}."

        serialized = json.dumps(units)
        self._run_pyautogui(_CHORD_UNITS_SCRIPT.format(serialized=serialized))
        return f"Pressed keys: {'+'.join(normalized_keys)}."

    def _target_platform(self) -> str:
        """Return Linux because pyautogui dispatches inside the OSWorld VM."""

        return "Linux"

    def _normalize_keys(self, key: Optional[str], keys: Optional[Sequence[str]]) -> list[str]:
        """Normalize user-facing keys for the remote Linux VM instead of the host OS."""

        raw_keys = list(keys or [])
        if not raw_keys and key:
            raw_keys = [key]

        normalized_keys: list[str] = []
        alias_map = self._key_alias_map(self._target_platform())
        for raw_key in raw_keys:
            if raw_key is None:
                continue
            normalized = str(raw_key).strip()
            if not normalized:
                continue
            pieces = (
                [piece.strip() for piece in normalized.split("+")]
                if "+" in normalized and len(normalized) > 1
                else [normalized]
            )
            for piece in pieces:
                if not piece:
                    continue
                lowered = piece.lower()
                normalized_keys.append(alias_map.get(lowered, lowered))
        return normalized_keys

    def _run_pyautogui(self, body: str) -> JsonObject:
        indented_body = body.replace("\n", "\n            ")
        script = textwrap.dedent(f"""
            import pyautogui

            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            {indented_body}
            """)
        return self._run_python(script, timeout=self.command_timeout_seconds)

    def _run_python(self, script: str, *, timeout: float) -> JsonObject:
        payload: JsonObject = {"code": textwrap.dedent(script)}
        response = self._request(
            "POST",
            "/run_python",
            json_payload=payload,
            timeout=timeout + 5.0,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise DesktopNativeError("OSWorld python execution returned a non-object payload.")
        if str(data.get("status", "")).lower() == "error":
            raise DesktopNativeError(
                str(data.get("error") or data.get("message") or "OSWorld python execution failed.")
            )
        return cast(JsonObject, data)


_CHORD_UNITS_SCRIPT = """
import json

units = json.loads({serialized!r})
printable_buffer = []
for unit in units:
    if len(unit) == 1:
        key_name = unit[0]
        if len(key_name) == 1 and key_name.isprintable():
            printable_buffer.append(key_name)
        else:
            if printable_buffer:
                pyautogui.write("".join(printable_buffer), interval=0.01)
                printable_buffer = []
            pyautogui.press(key_name)
    else:
        if printable_buffer:
            pyautogui.write("".join(printable_buffer), interval=0.01)
            printable_buffer = []
        pyautogui.hotkey(*unit, interval=0.05)
if printable_buffer:
    pyautogui.write("".join(printable_buffer), interval=0.01)
"""

_TYPE_TEXT_CLIPBOARD_SCRIPT = """
import json
import shutil
import subprocess
import time

payload = json.loads({serialized!r})
text = str(payload.get("text", ""))
target_kind = str(payload.get("target_kind", "editable"))

def _copy_with(command):
    try:
        subprocess.run(command, input=text, text=True, capture_output=True, check=True, timeout=10)
        return True
    except Exception:
        return False

copied = False
if shutil.which("xclip"):
    copied = _copy_with(["xclip", "-selection", "clipboard"])
    _copy_with(["xclip", "-selection", "primary"])
elif shutil.which("xsel"):
    copied = _copy_with(["xsel", "--clipboard", "--input"])
    _copy_with(["xsel", "--primary", "--input"])

if not copied:
    raise RuntimeError("target_kind requires xclip or xsel in the OSWorld desktop environment")

time.sleep(0.05)
if target_kind == "terminal":
    pyautogui.hotkey("ctrl", "shift", "v", interval=0.05)
else:
    pyautogui.hotkey("ctrl", "v", interval=0.05)
time.sleep(0.05)
"""
