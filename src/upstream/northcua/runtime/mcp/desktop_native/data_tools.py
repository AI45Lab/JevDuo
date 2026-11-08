"""Tool registration helpers for desktop-native data MCP tools."""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastmcp import FastMCP

from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.types import DesktopNativeDataURL, DesktopNativeMetadataPayload
from northcua_runtime_shared.desktop_surface.data_contracts import (
    DesktopAppTargetInfoParams,
    DesktopAppWindowControlsTargetInfoParams,
    DesktopAppWindowInfoParams,
    DesktopCaptureDesktopScreenshotParams,
    DesktopCaptureWindowScreenshotParams,
    desktop_data_contracts,
)

from .backend import DesktopCoordinateFrame, DesktopNativeBackend
from .state import DesktopNativeServerState, backend_error

logger = logging.getLogger(__name__)


def _register_desktop_native_data_tools(
    data_mcp: FastMCP,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> None:
    register_contract_handlers(data_mcp, desktop_data_contracts(), _desktop_data_handler_map(state, backend))


def _record_desktop_frame_metadata(
    state: DesktopNativeServerState,
    *,
    all_screens: bool,
    frame: DesktopCoordinateFrame,
) -> None:
    state.coordinate_frame = frame
    state.latest_desktop_screenshot_metadata = {
        "all_screens": all_screens,
        "width": frame.width,
        "height": frame.height,
        "left": frame.left,
        "top": frame.top,
    }


def _desktop_data_handler_map(
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> dict[str, Callable[..., object]]:
    return {
        "capture_desktop_screenshot": lambda params: _capture_desktop_screenshot(params, state, backend),
        "capture_window_screenshot": lambda params: _capture_window_screenshot(params, state, backend),
        "get_desktop_app_target_info": lambda params: _get_desktop_app_target_info(params, state, backend),
        "get_app_window_info": lambda params: _get_app_window_info(params, state, backend),
        "get_app_window_controls_target_info": lambda params: _get_app_window_controls_target_info(
            params, state, backend
        ),
    }


def _capture_desktop_screenshot(
    params: DesktopCaptureDesktopScreenshotParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeDataURL:
    try:
        logger.info("DesktopNativeDataExecutor capturing desktop screenshot (all_screens=%s)", params.all_screens)
        image_url, frame = backend.capture_desktop_screenshot(all_screens=params.all_screens)
        _record_desktop_frame_metadata(state, all_screens=params.all_screens, frame=frame)
        return image_url
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeDataExecutor capture_desktop_screenshot failed")
        raise backend_error(exc)


def _capture_window_screenshot(
    params: DesktopCaptureWindowScreenshotParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeDataURL:
    del params
    try:
        logger.info("DesktopNativeDataExecutor capturing active window screenshot")
        image_url = backend.capture_active_window_screenshot()
        if image_url:
            return image_url

        logger.info("Active window screenshot unavailable; falling back to desktop screenshot")
        image_url, frame = backend.capture_desktop_screenshot(all_screens=False)
        _record_desktop_frame_metadata(state, all_screens=False, frame=frame)
        return image_url
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeDataExecutor capture_window_screenshot failed")
        raise backend_error(exc)


def _get_desktop_app_target_info(
    params: DesktopAppTargetInfoParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> list[DesktopNativeMetadataPayload]:
    try:
        logger.info("DesktopNativeDataExecutor collecting open windows")
        open_windows = backend.get_open_windows(
            remove_empty=params.remove_empty,
            refresh_app_windows=params.refresh_app_windows,
        )
        state.latest_open_windows = open_windows
        return open_windows
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeDataExecutor get_desktop_app_target_info failed")
        raise backend_error(exc)


def _get_app_window_info(
    params: DesktopAppWindowInfoParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeMetadataPayload:
    try:
        logger.info("DesktopNativeDataExecutor collecting active window info")
        window_info = backend.get_active_window_info(field_list=params.field_list) or {}
        state.latest_active_window_info = window_info or None
        return window_info
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeDataExecutor get_app_window_info failed")
        raise backend_error(exc)


def _get_app_window_controls_target_info(
    params: DesktopAppWindowControlsTargetInfoParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> list[DesktopNativeMetadataPayload]:
    try:
        logger.info("DesktopNativeDataExecutor collecting active window controls")
        controls = backend.get_active_window_controls_target_info(field_list=params.field_list)
        state.cached_controls = controls
        return controls
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeDataExecutor get_app_window_controls_target_info failed")
        raise backend_error(exc)
