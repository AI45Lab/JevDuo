"""Shared desktop-native backend models and contracts."""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from pydantic import BaseModel

from northcua.core.coordinates import CoordinateFrame as RuntimeCoordinateFrame
from northcua.core.coordinates import (
    CoordinateMapper,
    CoordinatePoint,
    CoordinatePolicy,
)
from northcua.core.enums import CoordinateSpace
from northcua_runtime_shared.desktop_surface.models import TextTargetKind

from ._types import JsonObject


def save_data_url_to_path(image_url: str, destination: str) -> None:
    """Persist a data URL image to disk.

    Args:
        image_url: Data URL such as ``data:image/png;base64,...``.
        destination: Target filesystem path.

    Raises:
        ValueError: If the input is not an image data URL.
    """
    if not image_url.startswith("data:image/"):
        raise ValueError("Expected image data URL")
    _, base64_payload = image_url.split(",", 1)
    image_bytes = base64.b64decode(base64_payload)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(image_bytes)


class DesktopNativePoint(BaseModel):
    """Single pixel coordinate for native desktop actions.

    Fields:
        x: Horizontal desktop coordinate.
        y: Vertical desktop coordinate.
    """

    x: float
    y: float


@dataclass
class DesktopCoordinateFrame:
    """Coordinate frame for the current desktop screenshot.

    Attributes:
        left: Absolute desktop x offset.
        top: Absolute desktop y offset.
        width: Screenshot width in pixels.
        height: Screenshot height in pixels.
    """

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    def as_runtime_frame(self) -> RuntimeCoordinateFrame:
        """Return the generic runtime frame used by shared mappers.

        Returns:
            Shared coordinate frame with desktop offsets.
        """

        return RuntimeCoordinateFrame(
            width=self.width,
            height=self.height,
            offset_x=self.left,
            offset_y=self.top,
        )

    def translate(self, x: float, y: float, *, policy: CoordinatePolicy | None = None) -> tuple[int, int]:
        """Translate screenshot-relative coordinates into OS coordinates.

        Args:
            x: Screenshot-relative x position.
            y: Screenshot-relative y position.
            policy: Optional coordinate interpretation policy.

        Returns:
            Absolute desktop coordinates.
        """
        resolved_policy = policy or CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS)
        point = CoordinateMapper.to_runtime_point(
            CoordinatePoint(x=x, y=y),
            policy=resolved_policy,
            frame=self.as_runtime_frame(),
        )
        return point.as_int_tuple()


class DesktopNativeError(RuntimeError):
    """Base exception for desktop-native MCP failures."""


class DesktopNativeEnvironmentError(DesktopNativeError):
    """Error raised when the current desktop environment is unsupported."""


class DesktopNativeBackend(ABC):
    """Abstract backend for cross-platform desktop-native automation."""

    @abstractmethod
    def capture_desktop_screenshot(self, all_screens: bool = True) -> tuple[str, DesktopCoordinateFrame]:
        """Capture a desktop screenshot.

        Args:
            all_screens: Whether to capture the full virtual desktop.

        Returns:
            Data URL and coordinate frame for the captured screenshot.
        """

    @abstractmethod
    def capture_active_window_screenshot(self) -> Optional[str]:
        """Capture the active window screenshot if available.

        Returns:
            Screenshot data URL, or None if unavailable.
        """

    @abstractmethod
    def get_open_windows(self, remove_empty: bool = True, refresh_app_windows: bool = True) -> list[JsonObject]:
        """Get open desktop windows.

        Args:
            remove_empty: Whether to remove empty-title windows.
            refresh_app_windows: Cache hint retained for compatibility.

        Returns:
            Normalized target dictionaries.
        """

    @abstractmethod
    def get_active_window_info(self, field_list: Optional[list[str]] = None) -> Optional[JsonObject]:
        """Get active window metadata in UICollector-compatible format.

        Args:
            field_list: Requested field list.

        Returns:
            Window info dictionary, or None if unavailable.
        """

    @abstractmethod
    def get_active_window_controls_target_info(self, field_list: Optional[list[str]] = None) -> list[JsonObject]:
        """Get current active-window controls in target format.

        Args:
            field_list: Requested field list.

        Returns:
            Control target list.
        """

    @abstractmethod
    def click(self, x: float, y: float, button: str = "left") -> str:
        """Click a pixel coordinate.

        Args:
            x: Horizontal desktop coordinate.
            y: Vertical desktop coordinate.
            button: Mouse button to click.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def double_click(self, x: float, y: float, button: str = "left") -> str:
        """Double-click a pixel coordinate.

        Args:
            x: Horizontal desktop coordinate.
            y: Vertical desktop coordinate.
            button: Mouse button to use.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def drag(self, path: Sequence[DesktopNativePoint], button: str = "left") -> str:
        """Drag through a coordinate path.

        Args:
            path: Ordered desktop coordinate path.
            button: Mouse button to hold during the drag.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def mouse_down(self, button: str = "left") -> str:
        """Press and hold a mouse button at the current pointer position.

        Args:
            button: Mouse button to press.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def mouse_up(self, button: str = "left") -> str:
        """Release a mouse button at the current pointer position.

        Args:
            button: Mouse button to release.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def move(self, x: float, y: float) -> str:
        """Move the pointer to a pixel coordinate.

        Args:
            x: Horizontal desktop coordinate.
            y: Vertical desktop coordinate.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def scroll(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> str:
        """Scroll the pointer position.

        Args:
            x: Optional horizontal desktop coordinate.
            y: Optional vertical desktop coordinate.
            delta_x: Horizontal scroll delta.
            delta_y: Vertical pyautogui scroll delta; positive scrolls up,
                negative scrolls down.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def type_text(self, text: str, target_kind: TextTargetKind = TextTargetKind.AUTO) -> str:
        """Type raw text.

        Args:
            text: Literal text to type.
            target_kind: Focused control kind used to choose paste semantics.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def key_down(self, key: str) -> str:
        """Press and hold one keyboard key.

        Args:
            key: Single key to press and hold.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def key_up(self, key: str) -> str:
        """Release one keyboard key.

        Args:
            key: Single key to release.

        Returns:
            Plain-text backend action result.
        """

    @abstractmethod
    def keypress(self, key: Optional[str] = None, keys: Optional[Sequence[str]] = None) -> str:
        """Press one or more keys.

        Args:
            key: Optional single key to press.
            keys: Optional key chord or ordered sequence.

        Returns:
            Plain-text backend action result.
        """
