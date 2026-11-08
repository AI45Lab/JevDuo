"""Shared state for desktop-native MCP servers."""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastmcp.exceptions import ToolError

from northcua.core.coordinates import CoordinateMapper, CoordinatePoint, CoordinatePolicy
from northcua.core.enums import CoordinateSpace
from northcua.runtime.mcp.types import DesktopNativeMetadataPayload

from .backend import DesktopCoordinateFrame, DesktopNativeBackend, create_desktop_native_backend

logger = logging.getLogger(__name__)


class DesktopNativeServerState:
    """Shared singleton state for desktop-native data and action MCP servers."""

    _instance = None
    _initialized = False

    def __new__(cls):
        """Return the shared desktop-native server state instance.

        Returns:
            Singleton DesktopNativeServerState instance.
        """

        if cls._instance is None:
            cls._instance = super(DesktopNativeServerState, cls).__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize shared desktop-native caches once."""

        if not self._initialized:
            self.backend: DesktopNativeBackend | None = None
            self.latest_desktop_screenshot_metadata: DesktopNativeMetadataPayload = {}
            self.latest_open_windows: list[DesktopNativeMetadataPayload] = []
            self.latest_active_window_info: DesktopNativeMetadataPayload | None = None
            self.coordinate_frame = DesktopCoordinateFrame()
            self.coordinate_policy = CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS)
            self.cached_controls: list[DesktopNativeMetadataPayload] = []
            DesktopNativeServerState._initialized = True

    def get_backend(self, backend_factory: Callable[[], DesktopNativeBackend] | None = None) -> DesktopNativeBackend:
        """Get or initialize the shared backend.

        Args:
            backend_factory: Optional backend factory override.

        Returns:
            Shared desktop-native backend.
        """

        if self.backend is None:
            factory = backend_factory or create_desktop_native_backend
            self.backend = factory()
        return self.backend

    def reset(self) -> None:
        """Reset shared cached desktop-native state."""

        self.backend = None
        self.latest_desktop_screenshot_metadata = {}
        self.latest_open_windows = []
        self.latest_active_window_info = None
        self.coordinate_frame = DesktopCoordinateFrame()
        self.coordinate_policy = CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS)
        self.cached_controls = []

    def set_coordinate_policy(self, policy: CoordinatePolicy) -> None:
        """Update the shared coordinate interpretation policy.

        Args:
            policy: Coordinate policy used for future point translations.
        """

        self.coordinate_policy = policy

    def translate_point(self, x: float, y: float) -> tuple[int, int]:
        """Translate screenshot-relative coordinates into OS coordinates.

        Args:
            x: Horizontal input coordinate.
            y: Vertical input coordinate.

        Returns:
            Absolute desktop coordinate pair.
        """

        return self.coordinate_frame.translate(x, y, policy=self.coordinate_policy)

    def translate_frame_point(self, x: float, y: float) -> tuple[int, int]:
        """Translate configured model coordinates into screenshot pixels.

        Args:
            x: Horizontal model-facing coordinate.
            y: Vertical model-facing coordinate.

        Returns:
            Screenshot-relative pixel coordinate pair.
        """

        point = CoordinateMapper.to_frame_point(
            CoordinatePoint(x=x, y=y),
            policy=self.coordinate_policy,
            frame=self.coordinate_frame.as_runtime_frame(),
        )
        return point.as_int_tuple()


def backend_error(exc: Exception) -> ToolError:
    """Convert backend exceptions into MCP tool errors.

    Args:
        exc: Backend exception to expose through MCP.

    Returns:
        ToolError preserving the original error text.
    """

    logger.error("Desktop-native MCP error: %s", exc)
    return ToolError(str(exc))
