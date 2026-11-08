"""Screenshot persistence helpers for desktop-native MCP servers."""

from __future__ import annotations

import os
import tempfile
import time
import uuid

from northcua.runtime.mcp.types import DesktopNativeActionResultPayload, DesktopNativeScreenshotPayload

from .backend import DesktopNativeBackend, save_data_url_to_path
from .image_encoding import image_data_url_file_extension
from .state import DesktopNativeServerState


def _coordinate_policy_payload(state: DesktopNativeServerState) -> dict[str, object]:
    return {
        "input_space": state.coordinate_policy.input_space.value,
        "clamp_to_bounds": state.coordinate_policy.clamp_to_bounds,
    }


def _coordinate_frame_payload(state: DesktopNativeServerState) -> dict[str, int]:
    frame = state.coordinate_frame
    return {
        "left": frame.left,
        "top": frame.top,
        "width": frame.width,
        "height": frame.height,
    }


def capture_action_screenshot(
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
    *,
    all_screens: bool = False,
    stage_timings: dict[str, object] | None = None,
) -> DesktopNativeScreenshotPayload:
    """Capture a post-action desktop screenshot and persist it locally.

    Args:
        state: Shared desktop-native server state to update.
        backend: Desktop backend used for screenshot capture.
        all_screens: Whether to capture the full virtual desktop.

    Returns:
        Action screenshot payload with ``screenshot_path`` and
        ``screenshot_format`` keys. The path remains a temp-file local path for
        compatibility with existing action-result consumers.

    Raises:
        DesktopNativeError: If the backend cannot capture a screenshot.
        ValueError: If the backend returns a non-image data URL.
    """

    capture_started_at = time.perf_counter()
    image_url, frame = backend.capture_desktop_screenshot(all_screens=all_screens)
    if stage_timings is not None:
        stage_timings["screenshot_backend_capture_ms"] = int((time.perf_counter() - capture_started_at) * 1000)
    state.coordinate_frame = frame
    state.latest_desktop_screenshot_metadata = {
        "all_screens": all_screens,
        "width": frame.width,
        "height": frame.height,
        "left": frame.left,
        "top": frame.top,
    }
    screenshot_path = os.path.join(
        tempfile.gettempdir(),
        f"northcua-action-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}{image_data_url_file_extension(image_url)}",
    )
    save_started_at = time.perf_counter()
    save_data_url_to_path(image_url, screenshot_path)
    if stage_timings is not None:
        stage_timings["screenshot_save_ms"] = int((time.perf_counter() - save_started_at) * 1000)
        stage_timings["screenshot_bytes"] = os.path.getsize(screenshot_path)
    return {
        "screenshot_path": screenshot_path,
        "screenshot_format": "local_path",
    }


def build_action_result(
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
    text: str,
    *,
    tool_input_model: dict[str, object] | None = None,
    tool_input_pixels: dict[str, object] | None = None,
    tool_input_runtime_pixels: dict[str, object] | None = None,
    stage_timings: dict[str, object] | None = None,
) -> DesktopNativeActionResultPayload:
    """Build the standardized action result payload with a screenshot.

    Args:
        state: Shared desktop-native server state to update.
        backend: Desktop backend used for screenshot capture.
        text: Plain-text action feedback.
        tool_input_model: Model-facing input payload before coordinate mapping.
        tool_input_pixels: Screenshot-pixel payload for overlays/evidence.
        tool_input_runtime_pixels: Runtime-absolute pixel payload for diagnostics.

    Returns:
        Tool result payload with ``text``, ``screenshot_path``, and
        ``screenshot_format`` keys.

    Raises:
        DesktopNativeError: If post-action screenshot capture fails.
        ValueError: If screenshot persistence receives a non-image data URL.
    """

    result_started_at = time.perf_counter()
    timings = dict(stage_timings or {})
    coordinate_policy = _coordinate_policy_payload(state)
    coordinate_frame = _coordinate_frame_payload(state)
    wait_started_at = time.perf_counter()
    time.sleep(0.5)
    timings["post_action_wait_ms"] = int((time.perf_counter() - wait_started_at) * 1000)
    screenshot_started_at = time.perf_counter()
    screenshot_payload = capture_action_screenshot(state, backend, stage_timings=timings)
    timings["post_action_screenshot_total_ms"] = int((time.perf_counter() - screenshot_started_at) * 1000)
    timings["action_result_total_ms"] = int((time.perf_counter() - result_started_at) * 1000)
    result: DesktopNativeActionResultPayload = {
        "text": text,
        "coordinate_policy": coordinate_policy,
        "coordinate_frame": coordinate_frame,
        **screenshot_payload,
        "northcua_stage_timings": timings,
    }
    if tool_input_model is not None:
        result["tool_input_model"] = tool_input_model
    if tool_input_pixels is not None:
        result["tool_input_pixels"] = tool_input_pixels
    if tool_input_runtime_pixels is not None:
        result["tool_input_runtime_pixels"] = tool_input_runtime_pixels
    return result


def build_screenshot_result(
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
    text: str,
    *,
    all_screens: bool = False,
) -> DesktopNativeActionResultPayload:
    """Build a screenshot-only result without sending desktop input first.

    Args:
        state: Shared desktop-native server state to update.
        backend: Desktop backend used for screenshot capture.
        text: Plain-text screenshot feedback.
        all_screens: Whether to capture the full virtual desktop.

    Returns:
        Tool result payload with ``text``, ``screenshot_path``, and
        ``screenshot_format`` keys.

    Raises:
        DesktopNativeError: If screenshot capture fails.
        ValueError: If screenshot persistence receives a non-image data URL.
    """

    timings: dict[str, object] = {}
    screenshot_started_at = time.perf_counter()
    screenshot_payload = capture_action_screenshot(
        state,
        backend,
        all_screens=all_screens,
        stage_timings=timings,
    )
    timings["screenshot_tool_total_ms"] = int((time.perf_counter() - screenshot_started_at) * 1000)
    return {
        "text": text,
        "coordinate_policy": _coordinate_policy_payload(state),
        "coordinate_frame": _coordinate_frame_payload(state),
        "tool_input_model": {"all_screens": all_screens},
        "tool_input_pixels": {"all_screens": all_screens},
        "northcua_stage_timings": timings,
        **screenshot_payload,
    }
