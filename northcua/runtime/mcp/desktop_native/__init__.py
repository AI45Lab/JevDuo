"""Desktop-native MCP support package."""

from .backend import (
    DesktopCoordinateFrame,
    DesktopNativeBackend,
    DesktopNativeEnvironmentError,
    DesktopNativeError,
    DesktopNativePoint,
    PyAutoGuiDesktopBackend,
    create_desktop_native_backend,
    save_data_url_to_path,
)
from .image import build_action_result, build_screenshot_result, capture_action_screenshot
from .state import DesktopNativeServerState, backend_error

__all__ = [
    "DesktopCoordinateFrame",
    "DesktopNativeBackend",
    "DesktopNativeEnvironmentError",
    "DesktopNativeError",
    "DesktopNativePoint",
    "DesktopNativeServerState",
    "PyAutoGuiDesktopBackend",
    "backend_error",
    "build_action_result",
    "build_screenshot_result",
    "capture_action_screenshot",
    "create_desktop_native_backend",
    "save_data_url_to_path",
]
