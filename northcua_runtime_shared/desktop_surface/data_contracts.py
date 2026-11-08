"""Tool contracts for desktop-native data tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from northcua_protocol.capabilities import Capability, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import McpBinding, ToolContract, ToolVisibility

DESKTOP_NATIVE_DATA_SERVER_NAME = "DesktopNativeDataExecutor"


class DesktopDataParams(BaseModel):
    """Base parameter model for desktop data tools."""

    model_config = ConfigDict(extra="forbid")


class DesktopCaptureDesktopScreenshotParams(DesktopDataParams):
    """Parameters for desktop screenshot capture."""

    all_screens: bool = Field(
        default=False,
        description=(
            "Set to true to capture the full virtual desktop across all monitors. "
            "Set to false to capture only the primary display."
        ),
    )


class DesktopCaptureWindowScreenshotParams(DesktopDataParams):
    """Parameters for active-window screenshot capture."""


class DesktopAppTargetInfoParams(DesktopDataParams):
    """Parameters for listing desktop app/window targets."""

    remove_empty: bool = Field(
        default=True,
        description="Set to true to drop empty or unusable window entries from the returned list.",
    )
    refresh_app_windows: bool = Field(
        default=True,
        description=(
            "Compatibility flag retained for existing callers. Leave as true unless you need to mirror an older caller."
        ),
    )


class DesktopAppWindowInfoParams(DesktopDataParams):
    """Parameters for active-window metadata capture."""

    field_list: list[str] = Field(
        description=(
            "List of active-window field names to request, such as `control_text`, `control_type`, or `control_rect`."
        ),
    )


class DesktopAppWindowControlsTargetInfoParams(DesktopDataParams):
    """Parameters for active-window control target metadata."""

    field_list: list[str] = Field(
        description="List of control field names to request for each returned control item.",
    )


_DATA_TOOL_MODELS: tuple[tuple[str, type[BaseModel], Capability, float], ...] = (
    ("capture_desktop_screenshot", DesktopCaptureDesktopScreenshotParams, Capability.DESKTOP_SCREENSHOT, 10.0),
    ("capture_window_screenshot", DesktopCaptureWindowScreenshotParams, Capability.DESKTOP_SCREENSHOT, 10.0),
    ("get_desktop_app_target_info", DesktopAppTargetInfoParams, Capability.DESKTOP_WINDOW, 10.0),
    ("get_app_window_info", DesktopAppWindowInfoParams, Capability.DESKTOP_WINDOW, 10.0),
    (
        "get_app_window_controls_target_info",
        DesktopAppWindowControlsTargetInfoParams,
        Capability.DESKTOP_WINDOW,
        10.0,
    ),
)


def desktop_data_contracts() -> tuple[ToolContract, ...]:
    """Return source-of-truth contracts for desktop-native data tools."""

    return tuple(
        ToolContract(
            tool_id=tool_id,
            namespace="desktop_native",
            agent_kind="desktop",
            visibility=ToolVisibility.RUNTIME_ONLY,
            capability=capability,
            params_model=params_model,
            timeout_seconds=timeout_seconds,
            side_effect=False,
            requires_focus=False,
            risk_level=ToolRiskLevel.LOW,
            binding=McpBinding(server_name=DESKTOP_NATIVE_DATA_SERVER_NAME),
        )
        for tool_id, params_model, capability, timeout_seconds in _DATA_TOOL_MODELS
    )


__all__ = [
    "DESKTOP_NATIVE_DATA_SERVER_NAME",
    "DesktopAppTargetInfoParams",
    "DesktopAppWindowControlsTargetInfoParams",
    "DesktopAppWindowInfoParams",
    "DesktopCaptureDesktopScreenshotParams",
    "DesktopCaptureWindowScreenshotParams",
    "desktop_data_contracts",
]
