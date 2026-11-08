"""Desktop-native backend implementation and public import surface."""

from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from PIL import Image, ImageStat

from northcua.core.desktop_runtime_environment import desktop_backend_from_environment

from ._models import (
    DesktopCoordinateFrame,
    DesktopNativeBackend,
    DesktopNativeEnvironmentError,
    DesktopNativeError,
    DesktopNativePoint,
    save_data_url_to_path,
)
from ._pyautogui_support import PyAutoGuiSupportMixin
from ._types import JsonObject
from .backend_actions import PyAutoGuiDesktopBackendActionMixin

logger = logging.getLogger(__name__)

_MSS_GRAB_RETRIES = 3
_MSS_GRAB_RETRY_SECONDS = 0.15
_UNUSABLE_DARK_MEAN_MAX = 2.0
_UNUSABLE_DARK_STDDEV_MAX = 1.0


class PyAutoGuiDesktopBackend(PyAutoGuiDesktopBackendActionMixin, PyAutoGuiSupportMixin, DesktopNativeBackend):
    """Desktop-native backend implemented with pyautogui, mss, and pywinctl.

    Observation helpers are conservative about platform gaps: active-window
    screenshot capture returns ``None`` when no usable window rectangle exists,
    and v1 control extraction returns an empty list because there is no stable
    cross-platform control tree. Action helpers do not hide backend failures.
    """

    def __init__(self) -> None:
        """Initialize the backend and validate environment support.

        Raises:
            DesktopNativeEnvironmentError: If the current OS/session cannot
                support native desktop automation.
        """
        self._ensure_supported_environment()

    def capture_desktop_screenshot(self, all_screens: bool = True) -> tuple[str, DesktopCoordinateFrame]:
        """Capture a desktop screenshot using mss.

        Args:
            all_screens: Whether to capture the full virtual desktop.

        Returns:
            Screenshot data URL and coordinate frame.

        Raises:
            DesktopNativeEnvironmentError: If mss is unavailable.
        """
        capture = self._capture_desktop_screenshot_with_mss(all_screens=all_screens)
        if capture is not None:
            return capture
        return self._capture_desktop_screenshot_with_pyautogui()

    def _capture_desktop_screenshot_with_mss(
        self,
        *,
        all_screens: bool,
    ) -> tuple[str, DesktopCoordinateFrame] | None:
        """Capture a desktop screenshot with mss, retrying transient macOS failures."""

        mss = self._import_mss()
        last_error: Exception | None = None
        for attempt in range(1, _MSS_GRAB_RETRIES + 1):
            with mss.mss() as sct:
                if not sct.monitors:
                    logger.warning("mss exposed no monitors; falling back to pyautogui screenshot")
                    return None
                for monitor in self._candidate_mss_monitors(sct.monitors, all_screens=all_screens):
                    try:
                        screenshot = sct.grab(monitor)
                    except Exception as exc:
                        last_error = exc
                        logger.debug(
                            "mss monitor grab failed; trying next monitor",
                            extra={"monitor": monitor, "error": str(exc), "attempt": attempt},
                        )
                        continue
                    frame = DesktopCoordinateFrame(
                        left=int(monitor.get("left", 0)),
                        top=int(monitor.get("top", 0)),
                        width=int(monitor.get("width", screenshot.width)),
                        height=int(monitor.get("height", screenshot.height)),
                    )
                    return self._screenshot_to_data_url(screenshot), frame
            if attempt < _MSS_GRAB_RETRIES:
                time.sleep(_MSS_GRAB_RETRY_SECONDS)
        logger.warning(
            "mss desktop screenshot capture failed after %s attempts; falling back to pyautogui screenshot: %s",
            _MSS_GRAB_RETRIES,
            last_error,
        )
        return None

    @staticmethod
    def _candidate_mss_monitors(
        monitors: Sequence[dict[str, int]],
        *,
        all_screens: bool,
    ) -> tuple[dict[str, int], ...]:
        """Return preferred mss monitors plus fallbacks for flaky capture backends."""

        preferred_index = 0 if all_screens and len(monitors) > 1 else 1
        if preferred_index >= len(monitors):
            preferred_index = 0
        ordered_indexes = [preferred_index]
        ordered_indexes.extend(index for index in range(len(monitors)) if index != preferred_index)
        return tuple(monitors[index] for index in ordered_indexes)

    def _capture_desktop_screenshot_with_pyautogui(self) -> tuple[str, DesktopCoordinateFrame]:
        """Capture a primary-screen screenshot when mss exposes no monitors."""

        pyautogui = self._import_pyautogui()
        screenshot = pyautogui.screenshot()
        if self._is_unusable_dark_image(screenshot):
            raise DesktopNativeError(
                "Desktop screenshot capture fell back to pyautogui, but the fallback image was all black. "
                "Check macOS Screen Recording permissions or use a working mss/display capture session."
            )
        width, height = getattr(screenshot, "size", None) or pyautogui.size()
        frame = DesktopCoordinateFrame(left=0, top=0, width=int(width), height=int(height))
        return self._pil_image_to_data_url(screenshot), frame

    @staticmethod
    def _is_unusable_dark_image(image: Image.Image) -> bool:
        """Return whether a fallback screenshot is effectively all black."""

        stats = ImageStat.Stat(image.convert("L"))
        return stats.mean[0] <= _UNUSABLE_DARK_MEAN_MAX and stats.stddev[0] <= _UNUSABLE_DARK_STDDEV_MAX

    def capture_active_window_screenshot(self) -> Optional[str]:
        """Capture the currently active window when available.

        Returns:
            Screenshot data URL if capture succeeds, otherwise None.

        Raises:
            DesktopNativeEnvironmentError: If windowing or screenshot
                dependencies are unavailable.
        """
        rect = self._get_active_window_rect()
        if rect is None:
            return None

        left, top, right, bottom = rect
        width = max(right - left, 0)
        height = max(bottom - top, 0)
        if width <= 0 or height <= 0:
            return None

        mss = self._import_mss()

        with mss.mss() as sct:
            screenshot = sct.grab(
                {
                    "left": int(left),
                    "top": int(top),
                    "width": int(width),
                    "height": int(height),
                }
            )
            return self._screenshot_to_data_url(screenshot)


def create_desktop_native_backend() -> DesktopNativeBackend:
    """Create the default desktop-native backend instance.

    Returns:
        Desktop-native backend.

    Raises:
        DesktopNativeEnvironmentError: If the platform/session is unsupported
            or required desktop-native dependencies are unavailable.
    """
    backend_name = desktop_backend_from_environment()
    if backend_name == "osworld":
        from .osworld_backend import OSWorldDesktopBackend

        return OSWorldDesktopBackend()
    return PyAutoGuiDesktopBackend()
