"""Keyboard normalization helpers for pyautogui desktop actions."""

from __future__ import annotations

import platform
from collections.abc import Sequence
from typing import Optional

from northcua_runtime_shared.desktop_surface.models import KEYPRESS_EXACTLY_ONE_ERROR

from ._models import DesktopNativeError


class PyAutoGuiKeyMixin:
    """Keyboard helper mixin for pyautogui-backed desktop automation."""

    @staticmethod
    def _modifier_keys() -> set[str]:
        """Return the modifier-key set used to detect chords."""

        return {"ctrl", "alt", "shift", "command", "win"}

    def _target_platform(self) -> str:
        """Return the platform whose key aliases match the dispatch target."""

        return platform.system()

    def _is_hotkey_chord(self, normalized_keys: Sequence[str]) -> bool:
        """Return whether normalized keys represent a simultaneous chord."""

        if len(normalized_keys) < 2:
            return False
        modifiers = self._modifier_keys()
        return all(key in modifiers for key in normalized_keys[:-1]) and normalized_keys[-1] not in modifiers

    def _parse_chord_units(self, raw_keys: Sequence[Optional[str]]) -> list[list[str]]:
        """Parse user-supplied keys into ordered independent press units.

        Each element in ``keys`` is one unit. For example, ``"ctrl+l"`` becomes
        one hotkey unit while ``["ctrl+l", "tab"]`` becomes two units fired in
        order. Legacy callers that pass ``["ctrl", "shift", "p"]`` are still
        recognized by :meth:`_legacy_array_is_single_chord`.
        """

        alias_map = self._key_alias_map(self._target_platform())
        units: list[list[str]] = []
        for raw_key in raw_keys:
            if raw_key is None:
                continue
            normalized = str(raw_key).strip()
            if not normalized:
                continue
            if "+" in normalized and len(normalized) > 1:
                unit = [
                    alias_map.get(piece.strip().lower(), piece.strip().lower())
                    for piece in normalized.split("+")
                    if piece.strip()
                ]
            else:
                unit = [alias_map.get(normalized.lower(), normalized.lower())]
            if unit:
                units.append(unit)
        return units

    def _legacy_array_is_single_chord(self, units: Sequence[Sequence[str]]) -> bool:
        """Return whether single-key units preserve the old array-as-chord shape."""

        if len(units) < 2:
            return False
        if any(len(unit) != 1 for unit in units):
            return False
        return self._is_hotkey_chord([unit[0] for unit in units])

    def _keypress_raw_keys(self, key: Optional[str], keys: Optional[Sequence[str]]) -> list[str]:
        """Validate keypress input shape and return one raw key list.

        The model-visible contract says callers must provide exactly one of
        ``key`` or ``keys``. Keeping that validation here prevents local and
        remote backends from quietly drifting into different interpretations.
        """

        raw_keys = [str(item).strip() for item in list(keys or []) if item is not None and str(item).strip()]
        raw_key = str(key).strip() if key is not None else ""
        if raw_key and raw_keys:
            raise DesktopNativeError(KEYPRESS_EXACTLY_ONE_ERROR)
        if not raw_key and not raw_keys:
            raise DesktopNativeError(KEYPRESS_EXACTLY_ONE_ERROR)
        if raw_key:
            return [raw_key]
        return raw_keys

    def _dispatch_keypress_units(self, pyautogui, units: Sequence[Sequence[str]]) -> list[str]:
        """Dispatch parsed units and return the flattened normalized keys."""

        flat_keys = [token for unit in units for token in unit]
        if self._legacy_array_is_single_chord(units):
            pyautogui.hotkey(*flat_keys, interval=0.1)
            return flat_keys

        printable_buffer: list[str] = []
        for unit in units:
            if len(unit) == 1:
                key_name = unit[0]
                if self._is_printable_text_key(key_name):
                    printable_buffer.append(key_name)
                    continue
                if printable_buffer:
                    pyautogui.write("".join(printable_buffer), interval=0.01)
                    printable_buffer.clear()
                self._press_single_key(pyautogui, key_name)
            else:
                if printable_buffer:
                    pyautogui.write("".join(printable_buffer), interval=0.01)
                    printable_buffer.clear()
                pyautogui.hotkey(*unit, interval=0.1)
        if printable_buffer:
            pyautogui.write("".join(printable_buffer), interval=0.01)
        return flat_keys

    @staticmethod
    def _is_printable_text_key(normalized_key: str) -> bool:
        """Return whether a normalized key is best sent as literal text."""

        return len(normalized_key) == 1 and normalized_key.isprintable()

    @staticmethod
    def _press_single_key(pyautogui, normalized_key: str) -> None:
        """Press one normalized key, typing printable symbols literally."""

        if PyAutoGuiKeyMixin._is_printable_text_key(normalized_key):
            pyautogui.write(normalized_key, interval=0.01)
            return
        pyautogui.press(normalized_key)

    def _normalize_keys(self, key: Optional[str], keys: Optional[Sequence[str]]) -> list[str]:
        """Normalize input keys to pyautogui-compatible names."""

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

    def _normalize_single_key(self, key: str, *, action_name: str) -> str:
        """Normalize and validate a single hold/release key input.

        Args:
            key: User-facing key name.
            action_name: Name used in validation error messages.

        Returns:
            PyAutoGUI-compatible key name.

        Raises:
            DesktopNativeError: If the key is empty or expands to multiple keys.
        """

        normalized_keys = self._normalize_keys(key=key, keys=None)
        if len(normalized_keys) != 1:
            raise DesktopNativeError(f"{action_name} requires exactly one key.")
        return normalized_keys[0]

    @staticmethod
    def _key_alias_map(system_name: str) -> dict[str, str]:
        """Return pyautogui key aliases for the current platform."""

        meta_value = "command" if system_name == "Darwin" else "win"
        return {
            "ctrl": "ctrl",
            "control": "ctrl",
            "alt": "alt",
            "option": "alt",
            "opt": "alt",
            "shift": "shift",
            "enter": "enter",
            "return": "enter",
            "esc": "esc",
            "escape": "esc",
            "space": "space",
            "spacebar": "space",
            "tab": "tab",
            "backspace": "backspace",
            "bksp": "backspace",
            "del": "delete",
            "delete": "delete",
            "home": "home",
            "end": "end",
            "pgup": "pageup",
            "pageup": "pageup",
            "pgdn": "pagedown",
            "pagedown": "pagedown",
            "up": "up",
            "arrowup": "up",
            "down": "down",
            "arrowdown": "down",
            "left": "left",
            "arrowleft": "left",
            "right": "right",
            "arrowright": "right",
            "cmd": meta_value,
            "command": meta_value,
            "meta": meta_value,
            "super": meta_value,
            "win": meta_value,
            "windows": meta_value,
        }
