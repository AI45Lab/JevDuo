"""Typed action/tool contracts for the generic NorthCUA web agent."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError


class WebAgentActionType(str, Enum):
    """Stable internal action kinds for model-visible web tools."""

    NOTE = "note"
    OPEN = "open"
    SNAPSHOT = "snapshot"
    CLICK_REF = "click_ref"
    HOVER_REF = "hover_ref"
    FILL_REF = "fill_ref"
    SELECT_OPTION_REF = "select_option_ref"
    CHECK_REF = "check_ref"
    UNCHECK_REF = "uncheck_ref"
    DRAG_REF = "drag_ref"
    CLICK_POINT = "click_point"
    DOUBLE_CLICK_POINT = "double_click_point"
    DRAG_POINT = "drag_point"
    TYPE_TEXT = "type_text"
    PRESS = "press"
    SCROLL = "scroll"
    GET_TEXT = "get_text"
    GET_URL = "get_url"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    LIST_TABS = "list_tabs"
    NEW_TAB = "new_tab"
    CLOSE_TAB = "close_tab"
    FOCUS_TAB = "focus_tab"


class WebAgentToolName(str, Enum):
    """Provider-visible tool names exposed by the web action MCP server."""

    NOTE = "web_note"
    OPEN = "web_open"
    SNAPSHOT = "web_snapshot"
    CLICK_REF = "web_click_ref"
    HOVER_REF = "web_hover_ref"
    FILL_REF = "web_fill_ref"
    SELECT_OPTION_REF = "web_select_option_ref"
    CHECK_REF = "web_check_ref"
    UNCHECK_REF = "web_uncheck_ref"
    DRAG_REF = "web_drag_ref"
    CLICK_POINT = "web_click_point"
    DOUBLE_CLICK_POINT = "web_double_click_point"
    DRAG_POINT = "web_drag_point"
    TYPE_TEXT = "web_type_text"
    PRESS = "web_press"
    SCROLL = "web_scroll"
    GET_TEXT = "web_get_text"
    GET_URL = "web_get_url"
    WAIT = "web_wait"
    SCREENSHOT = "web_screenshot"
    LIST_TABS = "web_list_tabs"
    NEW_TAB = "web_new_tab"
    CLOSE_TAB = "web_close_tab"
    FOCUS_TAB = "web_focus_tab"


class WebScrollDirection(str, Enum):
    """Supported page scroll directions."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class WebMouseButton(str, Enum):
    """Supported mouse buttons for viewport point actions."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class WebWaitUntil(str, Enum):
    """Supported deterministic browser wait modes."""

    DURATION = "duration"
    LOAD = "load"
    TEXT = "text"
    URL = "url"
    SELECTOR = "selector"


ACTION_TOOL_NAME_BY_TYPE: dict[WebAgentActionType, WebAgentToolName] = {
    WebAgentActionType.NOTE: WebAgentToolName.NOTE,
    WebAgentActionType.OPEN: WebAgentToolName.OPEN,
    WebAgentActionType.SNAPSHOT: WebAgentToolName.SNAPSHOT,
    WebAgentActionType.CLICK_REF: WebAgentToolName.CLICK_REF,
    WebAgentActionType.HOVER_REF: WebAgentToolName.HOVER_REF,
    WebAgentActionType.FILL_REF: WebAgentToolName.FILL_REF,
    WebAgentActionType.SELECT_OPTION_REF: WebAgentToolName.SELECT_OPTION_REF,
    WebAgentActionType.CHECK_REF: WebAgentToolName.CHECK_REF,
    WebAgentActionType.UNCHECK_REF: WebAgentToolName.UNCHECK_REF,
    WebAgentActionType.DRAG_REF: WebAgentToolName.DRAG_REF,
    WebAgentActionType.CLICK_POINT: WebAgentToolName.CLICK_POINT,
    WebAgentActionType.DOUBLE_CLICK_POINT: WebAgentToolName.DOUBLE_CLICK_POINT,
    WebAgentActionType.DRAG_POINT: WebAgentToolName.DRAG_POINT,
    WebAgentActionType.TYPE_TEXT: WebAgentToolName.TYPE_TEXT,
    WebAgentActionType.PRESS: WebAgentToolName.PRESS,
    WebAgentActionType.SCROLL: WebAgentToolName.SCROLL,
    WebAgentActionType.GET_TEXT: WebAgentToolName.GET_TEXT,
    WebAgentActionType.GET_URL: WebAgentToolName.GET_URL,
    WebAgentActionType.WAIT: WebAgentToolName.WAIT,
    WebAgentActionType.SCREENSHOT: WebAgentToolName.SCREENSHOT,
    WebAgentActionType.LIST_TABS: WebAgentToolName.LIST_TABS,
    WebAgentActionType.NEW_TAB: WebAgentToolName.NEW_TAB,
    WebAgentActionType.CLOSE_TAB: WebAgentToolName.CLOSE_TAB,
    WebAgentActionType.FOCUS_TAB: WebAgentToolName.FOCUS_TAB,
}
ACTION_TYPE_BY_TOOL_NAME = {tool.value: action for action, tool in ACTION_TOOL_NAME_BY_TYPE.items()}


def action_tool_name_for_action(action: WebAgentActionType) -> str:
    """Return the model-visible tool name for an internal action kind.

    Args:
        action: Stable internal action identifier produced by the web runner.

    Returns:
        Provider-facing MCP tool name that should appear in the tool surface.
    """

    return ACTION_TOOL_NAME_BY_TYPE[action].value


def action_from_tool_name(tool_name: str) -> WebAgentActionType | None:
    """Resolve an internal action kind from a provider-visible tool name.

    Args:
        tool_name: Name received from the model/tool-call layer.

    Returns:
        Matching internal action type, or ``None`` when the tool belongs to a
        different agent surface.
    """

    return ACTION_TYPE_BY_TOOL_NAME.get(tool_name)


def all_web_agent_tool_names() -> set[str]:
    """Return all provider-visible web-agent tool names.

    Returns:
        Set used by tool-call parsers to decide whether a call should be routed
        to the web-agent action service.
    """

    return {tool.value for tool in WebAgentToolName}


WebRefInput = str | int


def canonical_web_ref(ref: object) -> str:
    """Return the model-visible ref spelling.

    Args:
        ref: Element reference from the latest model-visible observation.
            Numeric aliases such as ``3`` are preferred. Explicit source refs
            that already start with ``@`` are preserved for internal/debug
            callers.

    Returns:
        Trimmed numeric model ref or explicit ``@`` source ref.

    Raises:
        ValueError: If the reference is empty or contains whitespace.
    """

    if isinstance(ref, bool):
        raise ValueError("web element ref cannot be a boolean.")
    if isinstance(ref, int):
        stripped = str(ref)
    elif isinstance(ref, str):
        stripped = ref.strip()
    else:
        raise ValueError("web element ref must be a string or integer label.")
    if not stripped:
        raise ValueError("web element ref cannot be empty.")
    if any(char.isspace() for char in stripped):
        raise ValueError("web element ref cannot contain whitespace.")
    if stripped.startswith("@"):
        if not stripped[1:]:
            raise ValueError("web element ref cannot be empty.")
        return stripped
    if stripped.startswith("e") and stripped[1:].isdigit():
        return f"@{stripped}"
    return stripped


def _normalize_optional_token(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} cannot be empty.")
    if any(char.isspace() for char in stripped):
        raise ValueError(f"{field_name} cannot contain whitespace.")
    return stripped


class WebNoteParams(BaseModel):
    """Arguments for ``web_note``.

    The note is working memory for later browser steps. It does not interact
    with the page and never captures a screenshot or ref snapshot.
    """

    key: str = Field(
        default="",
        max_length=160,
        description="Stable memory key. Reuse a key to replace its previous value; omit to append.",
    )
    text: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "Durable, exact information to preserve for later web steps, such as copied values, confirmed facts, "
            "page constraints, decisions, or a mistake/prevention reminder. Use sparingly; do not record routine "
            "reasoning or every next action."
        ),
    )
    reason: str = Field(
        default="",
        max_length=2000,
        description=(
            "Why this note matters for later web steps, for example "
            "'needed before filling the form' or 'avoid selecting the wrong tab'."
        ),
    )
    kind: Literal["fact", "mistake", "precaution", "plan", "constraint", "decision", "other"] = Field(
        default="fact",
        description="The category of information being preserved.",
    )

    @field_validator("text", mode="after")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("web_note requires non-empty text.")
        return stripped

    @field_validator("reason", mode="after")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return value.strip()


class WebOpenParams(BaseModel):
    """Arguments for ``web_open``.

    Attributes:
        url: Absolute or browser-resolvable URL to load before the next
            observation. Validation only checks that the value is non-empty;
            URL policy is enforced by the action service.
    """

    url: str = Field(
        min_length=1,
        description=(
            "Absolute URL or browser-resolvable URL to open after web navigation policy validation. "
            "On success this mutating action returns a fresh post-action observation when enabled."
        ),
    )


class WebRefParams(BaseModel):
    """Arguments for a single element-ref action.

    Attributes:
        ref: Stable element reference from the latest snapshot observation.
            The page may reject stale refs after navigation or DOM updates.
    """

    ref: WebRefInput = Field(
        description=(
            "Numeric element ref from the latest snapshot or SoM observation, for example 3. Use ref actions whenever "
            "a reliable target ref exists; fall back to viewport point actions only for visual/custom targets without "
            "a reliable ref."
        ),
    )

    @field_validator("ref", mode="before")
    @classmethod
    def _canonicalize_ref(cls, value: object) -> str:
        return canonical_web_ref(value)


class WebRefsParams(BaseModel):
    """Arguments for runtime-only multi-ref probes.

    Attributes:
        refs: Snapshot element refs to inspect in one runtime call.
    """

    refs: list[WebRefInput] = Field(
        min_length=1,
        max_length=256,
        description=("Numeric element refs from the latest snapshot or SoM observation."),
    )

    @field_validator("refs", mode="before")
    @classmethod
    def _canonicalize_refs(cls, values: object) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("web refs must be a list.")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            ref = canonical_web_ref(value)
            if ref in seen:
                continue
            normalized.append(ref)
            seen.add(ref)
        return normalized


class WebFillRefParams(WebRefParams):
    """Arguments for ``web_fill_ref``.

    Attributes:
        text: Text to replace into the referenced editable control.
    """

    text: str = Field(
        description=(
            "Replacement text to set in the referenced editable element. The value replaces existing content; "
            "use this instead of a click/type sequence when a reliable input ref exists. Use web_type_text only "
            "when you intentionally need literal keystrokes into the currently focused element."
        ),
    )


class WebSelectOptionRefParams(WebRefParams):
    """Arguments for ``web_select_option_ref``.

    Attributes:
        values: One or more HTML ``<select>`` option values to select.
    """

    values: list[str] = Field(
        min_length=1,
        description=(
            "HTML option value strings to select on the referenced <select> element. Use the option value, "
            "not the visible label, unless the page uses the same string for both. Prefer this ref action over "
            "clicking dropdown coordinates when the select element has a reliable ref."
        ),
    )

    @field_validator("values", mode="after")
    @classmethod
    def _normalize_values(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if not value.strip():
                raise ValueError("web_select_option_ref values cannot contain empty entries.")
            normalized.append(value)
        return normalized


class WebDragRefParams(BaseModel):
    """Arguments for ``web_drag_ref``.

    Attributes:
        from_ref: Snapshot element ref to drag from.
        to_ref: Snapshot element ref to drop onto.
    """

    from_ref: WebRefInput = Field(
        description=(
            "Numeric element ref to drag from, taken from the latest snapshot or SoM observation. Use this before "
            "coordinate drag when the source exposes a reliable ref."
        ),
    )
    to_ref: WebRefInput = Field(
        description=(
            "Numeric element ref to drop onto, taken from the latest snapshot or SoM observation. Use this before "
            "coordinate drag when the destination exposes a reliable ref."
        ),
    )

    @field_validator("from_ref", "to_ref", mode="before")
    @classmethod
    def _canonicalize_ref(cls, value: object) -> str:
        return canonical_web_ref(value)


class WebClickPointParams(BaseModel):
    """Arguments for ``web_click_point``.

    Coordinates are CSS pixels relative to the current viewport top-left.
    Document-scroll and screenshot-image coordinates are not accepted.

    Attributes:
        x: Viewport-relative CSS x coordinate.
        y: Viewport-relative CSS y coordinate.
        button: Mouse button to click.
    """

    x: float = Field(
        ge=0,
        description=(
            "Current viewport CSS x coordinate from the viewport's top-left corner. Use only when the target has "
            "no reliable numeric ref in the latest snapshot or direct SoM labels."
        ),
    )
    y: float = Field(
        ge=0,
        description=(
            "Current viewport CSS y coordinate from the viewport's top-left corner. Use only when the target has "
            "no reliable numeric ref in the latest snapshot or direct SoM labels."
        ),
    )
    button: WebMouseButton = Field(
        default=WebMouseButton.LEFT,
        description=(
            "Mouse button to click: left is the normal primary click, right opens context menus, "
            "middle usually opens links in a background tab. Defaults to left."
        ),
    )


class WebDoubleClickPointParams(WebClickPointParams):
    """Arguments for ``web_double_click_point``."""


class WebDragPointParams(BaseModel):
    """Arguments for ``web_drag_point``.

    Coordinates are CSS pixels relative to the current viewport top-left. The
    action uses the left mouse button to align with desktop drag schemas.

    Attributes:
        from_x: Viewport-relative drag start x coordinate.
        from_y: Viewport-relative drag start y coordinate.
        to_x: Viewport-relative drag destination x coordinate.
        to_y: Viewport-relative drag destination y coordinate.
    """

    from_x: float = Field(
        ge=0,
        description=(
            "Current viewport CSS x coordinate where the drag starts. Use coordinate drag only when drag_ref is "
            "unavailable because either endpoint lacks a reliable numeric ref; if one endpoint has a ref, use its current "
            "bbox center as the corresponding point."
        ),
    )
    from_y: float = Field(
        ge=0,
        description=(
            "Current viewport CSS y coordinate where the drag starts. Use coordinate drag only when drag_ref is "
            "unavailable because either endpoint lacks a reliable numeric ref; if one endpoint has a ref, use its current "
            "bbox center as the corresponding point."
        ),
    )
    to_x: float = Field(
        ge=0,
        description=(
            "Current viewport CSS x coordinate where the drag ends. Use coordinate drag only when drag_ref is "
            "unavailable because either endpoint lacks a reliable numeric ref; if one endpoint has a ref, use its current "
            "bbox center as the corresponding point."
        ),
    )
    to_y: float = Field(
        ge=0,
        description=(
            "Current viewport CSS y coordinate where the drag ends. Use coordinate drag only when drag_ref is "
            "unavailable because either endpoint lacks a reliable numeric ref; if one endpoint has a ref, use its current "
            "bbox center as the corresponding point."
        ),
    )


class WebTypeTextParams(BaseModel):
    """Arguments for ``web_type_text``.

    Attributes:
        text: Literal keyboard text to send to the currently focused element.
    """

    text: str = Field(
        description=(
            "Literal text to type into the currently focused element. Prefer web_fill_ref for replacing text in "
            "a known input ref; use this for custom editors, incremental typing, or cases where keystroke behavior "
            "matters."
        ),
    )


class WebPressParams(BaseModel):
    """Arguments for ``web_press``.

    Attributes:
        key: Browser key name such as ``Enter`` or ``ArrowDown``.
    """

    key: str = Field(
        min_length=1,
        description=(
            "Browser key name such as Enter, Tab, ArrowDown, ArrowUp, Escape, Backspace, or a single character. "
            "Common aliases such as Left, Right, Up, Down, Return, Esc, Ctrl, and Cmd are normalized before "
            "execution. "
            "Use only after verifying or establishing the focused element when the key affects page state."
        ),
    )


class WebScrollParams(BaseModel):
    """Arguments for ``web_scroll``.

    Attributes:
        direction: Direction of the viewport scroll.
        amount: Positive CSS pixel distance to scroll.
    """

    direction: WebScrollDirection = Field(
        description="Viewport scroll direction: up/down move vertically; left/right move horizontally."
    )
    amount: int = Field(default=600, ge=1, description="Positive CSS pixel distance to scroll in the chosen direction.")


class WebWaitParams(BaseModel):
    """Arguments for ``web_wait``.

    The wait target fields are mutually exclusive: specify exactly one of
    ``duration_ms``, ``load_state``, ``text``, ``url_pattern``, or ``selector``.
    Duration waits are intentionally explicit so accidental empty waits do not
    turn into silent no-ops.

    Attributes:
        duration_ms: Milliseconds to wait explicitly.
        load_state: Browser load state to wait for.
        text: Text that should appear before continuing.
        url_pattern: URL pattern that should match before continuing.
        selector: DOM selector that should resolve before continuing.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "description": (
                "Mutually exclusive web_wait arguments. Provide exactly one of duration_ms, load_state, text, "
                "url_pattern, or selector. Conflicting targets are rejected; no target takes priority. "
                "Omit all unused fields; do not send null or invented placeholder values."
            )
        },
    )

    duration_ms: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Milliseconds to wait explicitly. Mutually exclusive with load_state, text, url_pattern, and selector; "
            "use only this field for a plain timed pause."
        ),
    )
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = Field(
        default=None,
        description=(
            "Browser load state to wait for: domcontentloaded means the DOM parsed, load means page load fired, "
            "networkidle means network activity quieted briefly. Mutually exclusive with duration_ms, text, "
            "url_pattern, and selector."
        ),
    )
    text: str | None = Field(
        default=None,
        description=(
            "Text that should appear before continuing. Mutually exclusive with duration_ms, load_state, "
            "url_pattern, and selector."
        ),
    )
    url_pattern: str | None = Field(
        default=None,
        description=(
            "URL pattern that should match before continuing. Mutually exclusive with duration_ms, load_state, "
            "text, and selector."
        ),
    )
    selector: str | None = Field(
        default=None,
        description=(
            "DOM selector that should resolve before continuing. Mutually exclusive with duration_ms, load_state, "
            "text, and url_pattern."
        ),
    )

    @staticmethod
    def _is_wildcard_url_pattern(value: str | None) -> bool:
        if value is None:
            return False
        return value.strip() in {"*", "**", ".*"}

    @model_validator(mode="after")
    def _exactly_one_wait_target(self) -> "WebWaitParams":
        for field_name in ("text", "url_pattern", "selector"):
            value = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                setattr(self, field_name, None)

        target_fields = (
            "duration_ms",
            "load_state",
            "text",
            "url_pattern",
            "selector",
        )
        received_targets: list[str] = []
        if self.duration_ms is not None:
            received_targets.append("duration_ms")
        received_targets.extend(field_name for field_name in target_fields[1:] if bool(getattr(self, field_name)))
        if len(received_targets) != 1:
            received = ", ".join(received_targets) if received_targets else "<none>"
            raise PydanticCustomError(
                "web_wait_target_combination",
                "web_wait expects exactly one wait target; received: {received}.",
                {
                    "received": received,
                    "received_targets": tuple(received_targets),
                },
            )

        if self._is_wildcard_url_pattern(self.url_pattern):
            raise ValueError("web_wait url_pattern must be specific; wildcard patterns are not valid wait targets.")
        return self


class WebScreenshotParams(BaseModel):
    """Arguments for ``web_screenshot``.

    Attributes:
        annotate: Whether NorthCUA should draw current-viewport Set-of-Mark
            direct numeric ref labels and bbox metadata.
    """

    model_config = ConfigDict(extra="forbid")

    annotate: bool = Field(
        default=False,
        description=(
            "When false, capture a clean screenshot. When true, capture a current-viewport Set-of-Mark screenshot "
            "with direct numeric ref labels and bboxes for visible interactive refs. Pass the visible numeric label directly "
            "to ref tools."
        ),
    )


class WebNewTabParams(BaseModel):
    """Arguments for ``web_new_tab``.

    Attributes:
        url: Optional URL to load in the new tab. URL policy is enforced by the
            action service when provided.
        label: Optional stable label that can later be used as ``tab_ref``.
    """

    url: str | None = Field(
        default=None, description="Optional URL to load in the new tab after URL policy validation."
    )
    label: str | None = Field(
        default=None,
        description="Optional stable tab label usable later as tab_ref for web_focus_tab or web_close_tab.",
    )

    @field_validator("url", "label", mode="after")
    @classmethod
    def _normalize_optional_token(cls, value: str | None, info) -> str | None:
        if info.field_name == "url":
            return value.strip() if isinstance(value, str) and value.strip() else None
        return _normalize_optional_token(value, field_name=str(info.field_name))


class WebCloseTabParams(BaseModel):
    """Arguments for ``web_close_tab``.

    Attributes:
        tab_ref: Optional stable tab id or label. When omitted, the active tab
            is closed.
    """

    tab_ref: str | None = Field(
        default=None,
        description="Stable tab id or label from web_list_tabs. Omit to close the active tab.",
    )

    @field_validator("tab_ref", mode="after")
    @classmethod
    def _normalize_tab_ref(cls, value: str | None) -> str | None:
        return _normalize_optional_token(value, field_name="tab_ref")


class WebFocusTabParams(BaseModel):
    """Arguments for ``web_focus_tab``.

    Attributes:
        tab_ref: Stable tab id or label from ``web_list_tabs``.
    """

    tab_ref: str = Field(
        min_length=1,
        description="Stable tab id or label from web_list_tabs, such as t1 or a label created by web_new_tab.",
    )

    @field_validator("tab_ref", mode="after")
    @classmethod
    def _normalize_tab_ref(cls, value: str) -> str:
        normalized = _normalize_optional_token(value, field_name="tab_ref")
        if normalized is None:
            raise ValueError("tab_ref cannot be empty.")
        return normalized
