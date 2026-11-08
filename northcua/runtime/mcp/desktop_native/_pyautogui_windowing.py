"""Window metadata helpers for pyautogui desktop-native backends."""

from __future__ import annotations

import logging
from typing import Optional

from ._types import WindowRect

logger = logging.getLogger(__name__)


class PyAutoGuiWindowMixin:
    """Best-effort window metadata extraction helpers."""

    @staticmethod
    def _call_or_value(value: object) -> object:
        """Resolve a property or zero-arg method."""

        return value() if callable(value) else value

    def _window_rect(self, window: object) -> Optional[WindowRect]:
        """Extract [left, top, right, bottom] from a window object."""

        left = self._call_or_value(getattr(window, "left", None))
        top = self._call_or_value(getattr(window, "top", None))
        width = self._call_or_value(getattr(window, "width", None))
        height = self._call_or_value(getattr(window, "height", None))

        if None in {left, top, width, height}:
            logger.debug(
                "Skipping window with incomplete rectangle: left=%r top=%r width=%r height=%r",
                left,
                top,
                width,
                height,
            )
            return None

        try:
            left_i = int(left)
            top_i = int(top)
            width_i = int(width)
            height_i = int(height)
        except (TypeError, ValueError):
            logger.debug(
                "Skipping window with non-integer rectangle: left=%r top=%r width=%r height=%r",
                left,
                top,
                width,
                height,
                exc_info=True,
            )
            return None

        if width_i <= 0 or height_i <= 0:
            logger.debug("Skipping window with non-positive size: width=%s height=%s", width_i, height_i)
            return None

        return [left_i, top_i, left_i + width_i, top_i + height_i]

    def _window_title(self, window: object) -> str:
        """Get a window title."""

        title = self._call_or_value(getattr(window, "title", "")) or ""
        return str(title)

    def _window_app_name(self, window: object) -> str:
        """Get the application or process name for a window if available."""

        app_name = getattr(window, "getAppName", None)
        if callable(app_name):
            try:
                return str(app_name() or "")
            except Exception:
                logger.debug("Failed to read window app name", exc_info=True)
                return ""
        return ""

    def _window_id(self, window: object, fallback_index: int) -> str:
        """Get a stable-ish window identifier."""

        for attr in ("getHandle", "_hWnd", "handle"):
            value = getattr(window, attr, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    logger.debug("Failed to read window identifier attribute %s", attr, exc_info=True)
                    value = None
            if value not in (None, ""):
                return str(value)
        return str(fallback_index)
