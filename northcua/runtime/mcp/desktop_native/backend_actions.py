"""Action and window helpers for the pyautogui desktop backend."""

from __future__ import annotations

import logging
import platform
from typing import Optional, Sequence, cast

from northcua.core.enums import TargetKind
from northcua.core.models import TargetInfo
from northcua_runtime_shared.desktop_surface.models import TextTargetKind

from ._models import DesktopNativeError, DesktopNativePoint
from ._types import JsonObject

# Pyright analyzes this mixin before it is composed with the support/window/key
# mixins in ``PyAutoGuiDesktopBackend``. The referenced helpers are provided by
# those sibling mixins at runtime, so this file suppresses only that structural
# attribute-access noise.
# pyright: reportAttributeAccessIssue=false

logger = logging.getLogger(__name__)


class PyAutoGuiDesktopBackendActionMixin:
    """Window, pointer, scroll, text, and key action helpers."""

    def get_open_windows(self, remove_empty: bool = True, refresh_app_windows: bool = True) -> list[JsonObject]:
        """Get normalized open-window target info.

        Args:
            remove_empty: Whether to filter empty-title windows.
            refresh_app_windows: Retained for compatibility.

        Returns:
            Window target dictionaries.

        Raises:
            DesktopNativeEnvironmentError: If pywinctl is unavailable.
        """
        del refresh_app_windows

        pywinctl = self._import_pywinctl()
        windows = pywinctl.getAllWindows()

        target_info_list: list[JsonObject] = []
        for index, window in enumerate(windows, start=1):
            rect = self._window_rect(window)
            title = self._window_title(window)
            if remove_empty and not title:
                continue
            if rect is None:
                continue

            target_info = TargetInfo(
                kind=TargetKind.WINDOW,
                id=self._window_id(window, index),
                name=title or self._window_app_name(window) or f"Window {index}",
                type=self._window_app_name(window) or "window",
                rect=rect,
            )
            target_info_list.append(cast(JsonObject, target_info.model_dump(mode="json")))

        return target_info_list

    def get_active_window_info(self, field_list: Optional[list[str]] = None) -> Optional[JsonObject]:
        """Get the active window in UICollector-compatible shape.

        Args:
            field_list: Requested field list.

        Returns:
            Window info dictionary or None.

        Raises:
            DesktopNativeEnvironmentError: If pywinctl is unavailable.
        """
        pywinctl = self._import_pywinctl()
        window = pywinctl.getActiveWindow()
        if window is None:
            return None

        rect = self._window_rect(window)
        title = self._window_title(window)
        if rect is None:
            return None

        field_map: JsonObject = {
            "control_text": title or self._window_app_name(window) or "",
            "control_type": "window",
            "control_rect": rect,
        }

        if not field_list:
            return field_map

        return {field: field_map.get(field) for field in field_list}

    def get_active_window_controls_target_info(self, field_list: Optional[list[str]] = None) -> list[JsonObject]:
        """Return an empty control list for v1 desktop-native prompting.

        Args:
            field_list: Requested field list.

        Returns:
            Empty control list.
        """
        del field_list
        return []

    def click(self, x: float, y: float, button: str = "left") -> str:
        """Click a pixel coordinate.

        Args:
            x: Screenshot-relative x coordinate.
            y: Screenshot-relative y coordinate.
            button: Mouse button.

        Returns:
            Plain-text result.
        """
        pyautogui = self._import_pyautogui()
        button = self._normalize_button(button)
        pyautogui.click(int(round(x)), int(round(y)), button=button)
        return f"Clicked at ({int(round(x))}, {int(round(y))}) with {button} button."

    def double_click(self, x: float, y: float, button: str = "left") -> str:
        """Double-click a pixel coordinate.

        Args:
            x: Screenshot-relative x coordinate.
            y: Screenshot-relative y coordinate.
            button: Mouse button.

        Returns:
            Plain-text result.
        """
        pyautogui = self._import_pyautogui()
        button = self._normalize_button(button)
        pyautogui.click(
            int(round(x)),
            int(round(y)),
            button=button,
            clicks=2,
            interval=0.1,
        )
        return f"Double-clicked at ({int(round(x))}, {int(round(y))}) with {button} button."

    def drag(self, path: Sequence[DesktopNativePoint], button: str = "left") -> str:
        """Drag through a path of points.

        Args:
            path: Ordered drag path.
            button: Mouse button to hold.

        Returns:
            Plain-text result.

        Raises:
            DesktopNativeError: If fewer than two points are provided.
        """
        if len(path) < 2:
            raise DesktopNativeError("drag action requires at least two points.")

        pyautogui = self._import_pyautogui()
        button = self._normalize_button(button)
        start = path[0]

        pyautogui.moveTo(int(round(start.x)), int(round(start.y)))
        pyautogui.mouseDown(button=button)
        try:
            for point in path[1:]:
                pyautogui.moveTo(int(round(point.x)), int(round(point.y)))
        finally:
            pyautogui.mouseUp(button=button)

        end = path[-1]
        return (
            f"Dragged with {button} button from ({int(round(start.x))}, "
            f"{int(round(start.y))}) to ({int(round(end.x))}, {int(round(end.y))})."
        )

    def mouse_down(self, button: str = "left") -> str:
        """Press and hold a mouse button at the current pointer location.

        This backend primitive is intentionally not registered as a
        model-visible MCP tool yet; it exists for controlled runtime adapters
        and future policy-gated tool surfaces.

        Args:
            button: Mouse button to press.

        Returns:
            Plain-text result.
        """

        pyautogui = self._import_pyautogui()
        button = self._normalize_button(button)
        pyautogui.mouseDown(button=button)
        return f"Pressed and held {button} mouse button."

    def mouse_up(self, button: str = "left") -> str:
        """Release a mouse button at the current pointer location.

        Args:
            button: Mouse button to release.

        Returns:
            Plain-text result.
        """

        pyautogui = self._import_pyautogui()
        button = self._normalize_button(button)
        pyautogui.mouseUp(button=button)
        return f"Released {button} mouse button."

    def move(self, x: float, y: float) -> str:
        """Move the pointer to a pixel coordinate.

        Args:
            x: Screenshot-relative x coordinate.
            y: Screenshot-relative y coordinate.

        Returns:
            Plain-text result.
        """
        pyautogui = self._import_pyautogui()
        pyautogui.moveTo(int(round(x)), int(round(y)))
        return f"Moved pointer to ({int(round(x))}, {int(round(y))})."

    def scroll(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> str:
        """Scroll at the current pointer or a provided coordinate.

        Args:
            x: Optional screenshot-relative x coordinate.
            y: Optional screenshot-relative y coordinate.
            delta_x: Horizontal scroll delta.
            delta_y: Vertical pyautogui scroll delta; positive scrolls up,
                negative scrolls down.

        Returns:
            Plain-text result.
        """
        pyautogui = self._import_pyautogui()

        mouse_x: Optional[int] = None
        mouse_y: Optional[int] = None
        if x is not None and y is not None:
            mouse_x = int(round(x))
            mouse_y = int(round(y))
            pyautogui.moveTo(mouse_x, mouse_y)

        if delta_y:
            if mouse_x is not None and mouse_y is not None:
                pyautogui.vscroll(int(round(delta_y)), x=mouse_x, y=mouse_y)
            else:
                pyautogui.vscroll(int(round(delta_y)))
        if delta_x:
            if mouse_x is not None and mouse_y is not None:
                pyautogui.hscroll(int(round(delta_x)), x=mouse_x, y=mouse_y)
            else:
                pyautogui.hscroll(int(round(delta_x)))

        return f"Scrolled (delta_x={int(round(delta_x))}, delta_y={int(round(delta_y))})" + (
            f" at ({mouse_x}, {mouse_y})." if mouse_x is not None and mouse_y is not None else "."
        )

    def type_text(self, text: str, target_kind: TextTargetKind = TextTargetKind.AUTO) -> str:
        """Type raw text.

        Args:
            text: Text to type.
            target_kind: Focused control kind used for paste semantics.

        Returns:
            Plain-text result.

        Raises:
            DesktopNativeError: If explicit paste semantics are requested but
                clipboard paste support is unavailable.
        """
        target_kind = TextTargetKind.coerce(target_kind)
        text = str(text or "")
        if text and self._type_text_via_clipboard(text, target_kind=target_kind):
            return f"Typed {len(text)} character(s)."

        if target_kind != TextTargetKind.AUTO:
            raise DesktopNativeError(
                f"type target_kind={target_kind.value!r} requires clipboard paste support, "
                "but the clipboard paste attempt failed."
            )

        if platform.system() == "Darwin" and text:
            if self._type_text_macos(text):
                return f"Typed {len(text)} character(s)."

        pyautogui = self._import_pyautogui()
        pyautogui.write(text, interval=0.01)
        return f"Typed {len(text)} character(s)."

    def key_down(self, key: str) -> str:
        """Press and hold one normalized key.

        Args:
            key: Single key to press and hold.

        Returns:
            Plain-text result.

        Raises:
            DesktopNativeError: If the input does not normalize to one key.
        """

        normalized_key = self._normalize_single_key(key, action_name="key_down")
        pyautogui = self._import_pyautogui()
        pyautogui.keyDown(normalized_key)
        return f"Pressed and held key: {normalized_key}."

    def key_up(self, key: str) -> str:
        """Release one normalized key.

        Args:
            key: Single key to release.

        Returns:
            Plain-text result.

        Raises:
            DesktopNativeError: If the input does not normalize to one key.
        """

        normalized_key = self._normalize_single_key(key, action_name="key_up")
        pyautogui = self._import_pyautogui()
        pyautogui.keyUp(normalized_key)
        return f"Released key: {normalized_key}."

    def keypress(self, key: Optional[str] = None, keys: Optional[Sequence[str]] = None) -> str:
        """Press one or more keys.

        Args:
            key: Single key input.
            keys: Chord or sequence input.

        Returns:
            Plain-text result.

        Raises:
            DesktopNativeError: If no key input is provided.
        """
        units = self._parse_chord_units(self._keypress_raw_keys(key, keys))
        if not units:
            raise DesktopNativeError("keypress action requires `key` or `keys`.")
        normalized_keys = [token for unit in units for token in unit]

        logger.info(
            "DesktopNativeActionExecutor normalized keypress raw_key=%s raw_keys=%s units=%s normalized_keys=%s",
            key,
            list(keys or []),
            units,
            normalized_keys,
        )

        pyautogui = self._import_pyautogui()
        normalized_keys = self._dispatch_keypress_units(pyautogui, units)

        return f"Pressed keys: {'+'.join(normalized_keys)}."
