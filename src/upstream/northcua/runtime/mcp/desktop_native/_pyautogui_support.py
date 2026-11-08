"""Support mixin for pyautogui desktop-native backend helpers."""

from __future__ import annotations

import logging
import os
import platform
from typing import Optional

from PIL import Image

from ._models import DesktopNativeEnvironmentError
from ._pyautogui_clipboard import PyAutoGuiClipboardMixin
from ._pyautogui_keys import PyAutoGuiKeyMixin
from ._pyautogui_windowing import PyAutoGuiWindowMixin
from ._types import WindowRect
from .image_encoding import encode_desktop_image_to_data_url

logger = logging.getLogger(__name__)


class PyAutoGuiSupportMixin(PyAutoGuiKeyMixin, PyAutoGuiClipboardMixin, PyAutoGuiWindowMixin):
    """Platform adapters and conversion helpers for the pyautogui backend.

    The mixin keeps optional metadata helpers fail-open: window titles,
    application names, and identifiers are skipped or defaulted when third-party
    window objects expose incomplete values. Action primitives remain
    fail-closed and let pyautogui/subprocess failures propagate to the MCP tool
    adapter.
    """

    def _ensure_supported_environment(self) -> None:
        """Ensure the current runtime supports native desktop automation.

        Raises:
            DesktopNativeEnvironmentError: If the platform/session is unsupported.
        """
        system_name = platform.system()
        if system_name not in {"Windows", "Darwin", "Linux"}:
            raise DesktopNativeEnvironmentError(f"Unsupported desktop platform: {system_name}.")

        if system_name == "Linux":
            session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
            display = os.environ.get("DISPLAY", "").strip()
            wayland_display = os.environ.get("WAYLAND_DISPLAY", "").strip()

            if session_type == "wayland" and not display:
                raise DesktopNativeEnvironmentError(
                    "Wayland is not supported for DesktopNativeActionExecutor in v1. Use an X11 session instead."
                )

            if not display and wayland_display:
                raise DesktopNativeEnvironmentError(
                    "Wayland is not supported for DesktopNativeActionExecutor in v1. Use an X11 session instead."
                )

            if not display:
                raise DesktopNativeEnvironmentError("Desktop-native automation on Linux requires a GUI X11 session.")

    def _import_pyautogui(self):
        """Import pyautogui lazily.

        Returns:
            Imported pyautogui module.

        Raises:
            DesktopNativeEnvironmentError: If pyautogui is unavailable.
        """
        try:
            import pyautogui  # type: ignore[import-untyped]
        except Exception as exc:
            logger.exception("Failed to import pyautogui for desktop-native backend")
            raise DesktopNativeEnvironmentError(
                "pyautogui is unavailable. Install the desktop-native dependencies "
                "and ensure GUI permissions are granted."
            ) from exc

        pyautogui.FAILSAFE = False
        if platform.system() == "Darwin":
            import Quartz
            from ._macos_keyboard import MacOSKeyboardAdapter

            adapter = getattr(self, "_macos_keyboard_adapter", None)
            if adapter is None:
                adapter = MacOSKeyboardAdapter(pyautogui, Quartz)
                self._macos_keyboard_adapter = adapter
            return adapter
        return pyautogui

    def _import_mss(self):
        """Import mss lazily.

        Returns:
            Imported mss module.

        Raises:
            DesktopNativeEnvironmentError: If mss is unavailable.
        """
        try:
            import mss  # type: ignore[import-untyped]
        except Exception as exc:
            logger.exception("Failed to import mss for desktop-native backend")
            raise DesktopNativeEnvironmentError(
                "mss is unavailable. Install the desktop-native dependencies first."
            ) from exc

        return mss

    def _import_pywinctl(self):
        """Import pywinctl lazily.

        Returns:
            Imported pywinctl module.

        Raises:
            DesktopNativeEnvironmentError: If pywinctl is unavailable.
        """
        try:
            import pywinctl  # type: ignore[import-untyped]
        except Exception as exc:
            logger.exception("Failed to import pywinctl for desktop-native backend")
            raise DesktopNativeEnvironmentError(
                "pywinctl is unavailable. Install the desktop-native dependencies first."
            ) from exc

        return pywinctl

    def _get_active_window_rect(self) -> Optional[WindowRect]:
        """Get the active window rectangle if available.

        Returns:
            Rectangle as [left, top, right, bottom], or None.

        Raises:
            DesktopNativeEnvironmentError: If pywinctl is unavailable.
        """
        pywinctl = self._import_pywinctl()
        window = pywinctl.getActiveWindow()
        if window is None:
            return None
        return self._window_rect(window)

    @staticmethod
    def _screenshot_to_data_url(screenshot: object) -> str:
        """Encode an MSS screenshot into a PNG data URL.

        Args:
            screenshot: MSS screenshot object.

        Returns:
            Image data URL.
        """
        image = Image.frombytes(
            "RGB",
            (int(getattr(screenshot, "width")), int(getattr(screenshot, "height"))),
            getattr(screenshot, "rgb"),
        )
        return encode_desktop_image_to_data_url(image)

    @staticmethod
    def _pil_image_to_data_url(image: Image.Image) -> str:
        """Encode a Pillow screenshot into an image data URL."""

        return encode_desktop_image_to_data_url(image)

    @staticmethod
    def _normalize_button(button: str) -> str:
        """Normalize mouse buttons to pyautogui-compatible values.

        Args:
            button: Requested button.

        Returns:
            Normalized button.
        """
        normalized = str(button or "left").strip().lower()
        if normalized == "wheel":
            return "middle"
        if normalized not in {"left", "right", "middle"}:
            return "left"
        return normalized
