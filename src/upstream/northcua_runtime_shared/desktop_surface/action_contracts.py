"""Tool contracts for desktop-native action tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from northcua_protocol.capabilities import Capability, ToolRiskLevel
from northcua_runtime_shared.desktop_surface.models import (
    KEYPRESS_KEY_DESCRIPTION,
    KEYPRESS_KEYS_DESCRIPTION,
    KEYPRESS_TOOL_DESCRIPTION,
    TYPE_TARGET_KIND_DESCRIPTION,
    TYPE_TEXT_DESCRIPTION,
    TextTargetKind,
)
from northcua_runtime_shared.tool_contracts import McpBinding, ToolContract, ToolVisibility

DESKTOP_NATIVE_ACTION_SERVER_NAME = "DesktopNativeActionExecutor"


class DesktopActionParams(BaseModel):
    """Base parameter model for desktop action tools."""

    model_config = ConfigDict(extra="forbid")


class DesktopNoteParams(DesktopActionParams):
    """Parameters for the desktop ``note`` tool."""

    key: str = Field(
        default="",
        max_length=160,
        description="Stable memory key. Reuse a key to replace its previous value; omit to append.",
    )
    text: str = Field(
        description=(
            "The exact information to preserve, such as copied values, confirmed facts, "
            "or a mistake/prevention reminder."
        ),
        max_length=8000,
    )
    reason: str = Field(
        default="",
        description=(
            "Why this note matters for later steps, for example "
            "'needed before filling the form' or 'avoid retyping the wrong email'."
        ),
        max_length=2000,
    )
    kind: Literal["fact", "mistake", "precaution", "plan", "constraint", "decision", "other"] = Field(
        default="fact",
        description="The category of information being preserved.",
    )


class DesktopScreenshotParams(DesktopActionParams):
    """Parameters for the desktop action ``screenshot`` tool."""

    all_screens: bool = Field(
        default=False,
        description=(
            "Set to true to capture the full virtual desktop across all monitors. "
            "Leave false to capture the primary display only."
        ),
    )


class DesktopPointParams(DesktopActionParams):
    """Single pixel coordinate for native desktop actions."""

    x: float = Field(description="Horizontal coordinate.")
    y: float = Field(description="Vertical coordinate.")


class DesktopClickParams(DesktopPointParams):
    """Parameters for the desktop ``click`` tool."""

    button: str = Field(
        default="left",
        description='Mouse button to click: "left", "right", or "middle". Use "right" for a right-click/context menu.',
    )


class DesktopDoubleClickParams(DesktopPointParams):
    """Parameters for the desktop ``double_click`` tool."""

    button: str = Field(default="left", description='Mouse button to use: "left", "right", or "middle".')


class DesktopDragParams(DesktopActionParams):
    """Parameters for the desktop ``drag`` tool."""

    path: list[DesktopPointParams] = Field(description="Ordered drag path.", min_length=2)
    button: str = Field(default="left", description='Mouse button to hold during drag: "left", "right", or "middle".')


class DesktopMoveParams(DesktopPointParams):
    """Parameters for the desktop ``move`` tool."""


class DesktopScrollParams(DesktopActionParams):
    """Parameters for the desktop ``scroll`` tool."""

    x: float | None = Field(default=None, description="Optional horizontal coordinate.")
    y: float | None = Field(default=None, description="Optional vertical coordinate.")
    delta_x: float = Field(default=0, description="Horizontal scroll delta.")
    delta_y: float = Field(default=0, description="Vertical scroll delta; positive scrolls up, negative scrolls down.")


class DesktopTypeParams(DesktopActionParams):
    """Parameters for the desktop ``type`` tool."""

    text: str = Field(description=TYPE_TEXT_DESCRIPTION)
    target_kind: Literal["auto", "editable", "terminal"] = Field(
        default=TextTargetKind.AUTO.value,
        description=TYPE_TARGET_KIND_DESCRIPTION,
    )


class DesktopKeypressParams(DesktopActionParams):
    """Parameters for the desktop ``keypress`` tool."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "key": {"type": "string"},
                        "keys": {"type": "null"},
                    },
                    "required": ["key"],
                },
                {
                    "properties": {
                        "key": {"type": "null"},
                        "keys": {"items": {"type": "string"}, "type": "array"},
                    },
                    "required": ["keys"],
                },
            ]
        },
    )

    key: str | None = Field(default=None, description=KEYPRESS_KEY_DESCRIPTION)
    keys: list[str] | None = Field(default=None, description=KEYPRESS_KEYS_DESCRIPTION)

    @model_validator(mode="after")
    def _validate_exactly_one(self) -> "DesktopKeypressParams":
        if (self.key is None) == (self.keys is None):
            raise ValueError("keypress action requires exactly one of `key` or `keys`.")
        return self


class DesktopWaitParams(DesktopActionParams):
    """Parameters for the desktop ``wait`` tool."""

    ms: int = Field(default=1000, description="Wait duration in milliseconds.")


_ACTION_TOOL_MODELS: tuple[tuple[str, type[BaseModel], Capability, float, bool, bool, ToolRiskLevel], ...] = (
    ("note", DesktopNoteParams, Capability.DESKTOP_INPUT, 2.0, False, False, ToolRiskLevel.LOW),
    ("screenshot", DesktopScreenshotParams, Capability.DESKTOP_SCREENSHOT, 10.0, False, False, ToolRiskLevel.LOW),
    ("click", DesktopClickParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.MEDIUM),
    ("double_click", DesktopDoubleClickParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.MEDIUM),
    ("drag", DesktopDragParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.HIGH),
    ("move", DesktopMoveParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.MEDIUM),
    ("scroll", DesktopScrollParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.MEDIUM),
    ("type", DesktopTypeParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.HIGH),
    ("keypress", DesktopKeypressParams, Capability.DESKTOP_INPUT, 10.0, True, True, ToolRiskLevel.HIGH),
    ("wait", DesktopWaitParams, Capability.DESKTOP_INPUT, 10.0, False, False, ToolRiskLevel.LOW),
)

_TYPE_TOOL_DESCRIPTION = (
    TYPE_TEXT_DESCRIPTION + " Prefer `keypress` for shortcuts, navigation keys, enter/escape, or exact key behavior. "
    "Returns plain-text feedback plus the post-action screenshot path and format."
)

_ACTION_DESCRIPTION_BY_TOOL_ID = {
    "note": (
        "Record important text for later steps without interacting with the desktop. Use this as explicit working "
        "memory for facts you just discovered, values that must be entered later, mistakes to avoid repeating, "
        "constraints, precautions, and next-step plans. The tool returns the note in its text result so it remains "
        "visible in tool-call history even when screenshot history is compacted."
    ),
    "screenshot": (
        "Capture a fresh desktop screenshot without sending any click, key, or typing input. Use this when you need "
        "an updated visual read of the desktop before deciding the next action. Returns plain-text feedback plus the "
        "screenshot path and format."
    ),
    "click": (
        "Click a desktop coordinate interpreted using the configured coordinate policy. Returns a dictionary with "
        "plain-text action feedback plus the post-action screenshot path and format."
    ),
    "double_click": (
        "Double-click a desktop coordinate interpreted using the configured coordinate policy. Returns a dictionary "
        "with plain-text action feedback plus the post-action screenshot path and format."
    ),
    "drag": (
        "Drag through an ordered list of desktop coordinates interpreted using the configured coordinate policy. The "
        "path must contain at least two points. Returns plain-text feedback plus the post-action screenshot path and "
        "format."
    ),
    "move": (
        "Move the mouse pointer to a desktop coordinate interpreted using the configured coordinate policy. Returns "
        "plain-text feedback plus the post-action screenshot path and format."
    ),
    "scroll": (
        "Scroll the desktop at the current pointer location or at a specific desktop coordinate interpreted using the "
        "configured coordinate policy. Vertical deltas follow pyautogui wheel semantics: positive `delta_y` scrolls "
        "up and negative `delta_y` scrolls down. Returns plain-text feedback plus the post-action screenshot path "
        "and format."
    ),
    "type": _TYPE_TOOL_DESCRIPTION,
    "keypress": KEYPRESS_TOOL_DESCRIPTION,
    "wait": (
        "Wait without sending input. The tool sleeps for the requested time and then captures a fresh screenshot. "
        "Returns plain-text feedback plus the post-wait screenshot path and format."
    ),
}


def desktop_action_contracts() -> tuple[ToolContract, ...]:
    """Return source-of-truth contracts for desktop-native action tools."""

    return tuple(
        ToolContract(
            tool_id=tool_id,
            description=_ACTION_DESCRIPTION_BY_TOOL_ID[tool_id],
            namespace="desktop_native",
            agent_kind="desktop",
            visibility=ToolVisibility.MODEL_VISIBLE,
            capability=capability,
            params_model=params_model,
            timeout_seconds=timeout_seconds,
            side_effect=side_effect,
            requires_focus=requires_focus,
            risk_level=risk_level,
            binding=McpBinding(server_name=DESKTOP_NATIVE_ACTION_SERVER_NAME),
        )
        for tool_id, params_model, capability, timeout_seconds, side_effect, requires_focus, risk_level in _ACTION_TOOL_MODELS
    )


__all__ = [
    "DESKTOP_NATIVE_ACTION_SERVER_NAME",
    "DesktopClickParams",
    "DesktopDoubleClickParams",
    "DesktopDragParams",
    "DesktopKeypressParams",
    "DesktopMoveParams",
    "DesktopNoteParams",
    "DesktopPointParams",
    "DesktopScreenshotParams",
    "DesktopScrollParams",
    "DesktopTypeParams",
    "DesktopWaitParams",
    "desktop_action_contracts",
]
