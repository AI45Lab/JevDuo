"""Screenshot and window metadata helpers for OSWorld desktop backend."""

from __future__ import annotations

import base64
import io
import json
from typing import Optional

from PIL import Image

from northcua.core.enums import TargetKind
from northcua.core.models import TargetInfo

from ._models import DesktopCoordinateFrame
from ._types import JsonObject


class OSWorldWindowMixin:
    """Window and screenshot operations backed by OSWorld HTTP endpoints."""

    command_timeout_seconds: float
    screenshot_timeout_seconds: float

    def capture_desktop_screenshot(self, all_screens: bool = True) -> tuple[str, DesktopCoordinateFrame]:
        """Capture a fresh screenshot from the OSWorld VM.

        Args:
            all_screens: Retained for DesktopNativeBackend compatibility.

        Returns:
            Screenshot data URL and coordinate frame.
        """

        del all_screens
        image_bytes = self._get_bytes("/screenshot", timeout=self.screenshot_timeout_seconds)
        image = Image.open(io.BytesIO(image_bytes))
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return (
            f"data:image/png;base64,{encoded}",
            DesktopCoordinateFrame(left=0, top=0, width=image.width, height=image.height),
        )

    def capture_active_window_screenshot(self) -> Optional[str]:
        """Return a cropped active-window screenshot when geometry is available.

        Returns:
            Data URL for the active window crop, or None when OSWorld cannot
            provide active-window geometry.
        """

        window = self._get_active_window_info()
        rect = window.get("control_rect") if isinstance(window, dict) else None
        if not isinstance(rect, list) or len(rect) != 4:
            return None
        left, top, right, bottom = [int(value) for value in rect]
        width = max(right - left, 0)
        height = max(bottom - top, 0)
        if width <= 0 or height <= 0:
            return None

        image_bytes = self._get_bytes("/screenshot", timeout=self.screenshot_timeout_seconds)
        image = Image.open(io.BytesIO(image_bytes))
        cropped = image.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def get_open_windows(self, remove_empty: bool = True, refresh_app_windows: bool = True) -> list[JsonObject]:
        """Return best-effort open-window metadata using ``wmctrl`` when available.

        Args:
            remove_empty: Whether to drop empty-title windows.
            refresh_app_windows: Retained for DesktopNativeBackend compatibility.

        Returns:
            Window target dictionaries in the desktop-native MCP schema.
        """

        del refresh_app_windows
        payload = self._run_python(_OPEN_WINDOWS_SCRIPT, timeout=self.command_timeout_seconds)
        windows_payload = payload.get("output", "") if isinstance(payload, dict) else ""
        try:
            raw_windows = json.loads(str(windows_payload) or "[]")
        except json.JSONDecodeError:
            raw_windows = []

        targets: list[JsonObject] = []
        for index, item in enumerate(raw_windows, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "")
            if remove_empty and not title:
                continue
            x = int(item.get("x") or 0)
            y = int(item.get("y") or 0)
            width = max(int(item.get("width") or 0), 0)
            height = max(int(item.get("height") or 0), 0)
            if width <= 0 or height <= 0:
                continue
            target = TargetInfo(
                kind=TargetKind.WINDOW,
                id=str(item.get("id") or index),
                name=title or f"Window {index}",
                type=str(item.get("wm_class") or "window"),
                rect=[x, y, x + width, y + height],
            )
            targets.append(target.model_dump(mode="json"))
        return targets

    def get_active_window_info(self, field_list: Optional[list[str]] = None) -> Optional[JsonObject]:
        """Return best-effort active-window metadata gathered remotely.

        Args:
            field_list: Optional subset of fields to project.

        Returns:
            Active-window metadata, or None when unavailable.
        """

        field_map = self._get_active_window_info()
        if field_map is None:
            return None
        if not field_list:
            return field_map
        return {field: field_map.get(field) for field in field_list}

    def get_active_window_controls_target_info(self, field_list: Optional[list[str]] = None) -> list[JsonObject]:
        """Return control target metadata.

        Args:
            field_list: Retained for DesktopNativeBackend compatibility.

        Returns:
            Empty list because OSWorld does not expose a compatible control tree yet.
        """

        del field_list
        return []

    def _get_active_window_info(self) -> JsonObject | None:
        payload = self._run_python(
            _ACTIVE_WINDOW_SCRIPT,
            timeout=min(self.command_timeout_seconds, 30.0),
        )
        output = payload.get("output", "") if isinstance(payload, dict) else ""
        try:
            parsed = json.loads(str(output) or "{}")
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) and parsed else None


_OPEN_WINDOWS_SCRIPT = """
import json
import subprocess

def parse_line(line: str) -> dict[str, object] | None:
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None
    win_id, _, pid, x, y, width, height, wm_class, title = parts
    return {
        "id": str(win_id),
        "pid": int(pid),
        "x": int(x),
        "y": int(y),
        "width": int(width),
        "height": int(height),
        "wm_class": wm_class,
        "title": title,
    }

try:
    result = subprocess.run(
        ["wmctrl", "-lpGx"],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = [parse_line(line) for line in result.stdout.splitlines()]
    print(json.dumps([line for line in lines if line]))
except Exception:
    print("[]")
"""

_ACTIVE_WINDOW_SCRIPT = """
import json
import subprocess

result = {}
try:
    window_id = subprocess.run(
        ["xdotool", "getactivewindow"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if window_id:
        title = subprocess.run(
            ["xdotool", "getwindowname", window_id],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        geometry_output = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", window_id],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        geometry = {}
        for line in geometry_output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            geometry[key.strip()] = value.strip()
        x = int(geometry.get("X", "0"))
        y = int(geometry.get("Y", "0"))
        width = int(geometry.get("WIDTH", "0"))
        height = int(geometry.get("HEIGHT", "0"))
        result = {
            "window_id": str(window_id),
            "title": title,
            "control_text": title,
            "control_type": "window",
            "control_rect": [x, y, x + max(width, 0), y + max(height, 0)],
        }
except Exception:
    result = {}
print(json.dumps(result))
"""
