"""Contract source of truth for the generic web agent."""

from __future__ import annotations

from copy import deepcopy

from pydantic import BaseModel

from northcua_protocol.capabilities import Capability, JsonObject, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import (
    McpBinding,
    ProviderSchemaMode,
    ToolContract,
    ToolContractKind,
    ToolVisibility,
)

from ..domain.actions import (
    WebAgentToolName,
    WebClickPointParams,
    WebCloseTabParams,
    WebDoubleClickPointParams,
    WebDragPointParams,
    WebDragRefParams,
    WebFillRefParams,
    WebFocusTabParams,
    WebNewTabParams,
    WebNoteParams,
    WebOpenParams,
    WebPressParams,
    WebRefParams,
    WebRefsParams,
    WebScreenshotParams,
    WebScrollParams,
    WebSelectOptionRefParams,
    WebTypeTextParams,
    WebWaitParams,
)

_READ_ONLY_TOOLS = {
    WebAgentToolName.NOTE.value,
    WebAgentToolName.SNAPSHOT.value,
    WebAgentToolName.GET_TEXT.value,
    WebAgentToolName.GET_URL.value,
    WebAgentToolName.SCREENSHOT.value,
    WebAgentToolName.LIST_TABS.value,
}
_FOCUS_TOOLS = {
    WebAgentToolName.CLICK_REF.value,
    WebAgentToolName.HOVER_REF.value,
    WebAgentToolName.FILL_REF.value,
    WebAgentToolName.SELECT_OPTION_REF.value,
    WebAgentToolName.CHECK_REF.value,
    WebAgentToolName.UNCHECK_REF.value,
    WebAgentToolName.DRAG_REF.value,
    WebAgentToolName.CLICK_POINT.value,
    WebAgentToolName.DOUBLE_CLICK_POINT.value,
    WebAgentToolName.DRAG_POINT.value,
    WebAgentToolName.TYPE_TEXT.value,
    WebAgentToolName.PRESS.value,
    WebAgentToolName.SCROLL.value,
}
_PARAMS_BY_TOOL_NAME: dict[str, type[BaseModel] | None] = {
    WebAgentToolName.NOTE.value: WebNoteParams,
    WebAgentToolName.OPEN.value: WebOpenParams,
    WebAgentToolName.SNAPSHOT.value: None,
    WebAgentToolName.CLICK_REF.value: WebRefParams,
    WebAgentToolName.HOVER_REF.value: WebRefParams,
    WebAgentToolName.FILL_REF.value: WebFillRefParams,
    WebAgentToolName.SELECT_OPTION_REF.value: WebSelectOptionRefParams,
    WebAgentToolName.CHECK_REF.value: WebRefParams,
    WebAgentToolName.UNCHECK_REF.value: WebRefParams,
    WebAgentToolName.DRAG_REF.value: WebDragRefParams,
    WebAgentToolName.CLICK_POINT.value: WebClickPointParams,
    WebAgentToolName.DOUBLE_CLICK_POINT.value: WebDoubleClickPointParams,
    WebAgentToolName.DRAG_POINT.value: WebDragPointParams,
    WebAgentToolName.TYPE_TEXT.value: WebTypeTextParams,
    WebAgentToolName.PRESS.value: WebPressParams,
    WebAgentToolName.SCROLL.value: WebScrollParams,
    WebAgentToolName.GET_TEXT.value: WebRefParams,
    WebAgentToolName.GET_URL.value: None,
    WebAgentToolName.WAIT.value: WebWaitParams,
    WebAgentToolName.SCREENSHOT.value: WebScreenshotParams,
    WebAgentToolName.LIST_TABS.value: None,
    WebAgentToolName.NEW_TAB.value: WebNewTabParams,
    WebAgentToolName.CLOSE_TAB.value: WebCloseTabParams,
    WebAgentToolName.FOCUS_TAB.value: WebFocusTabParams,
}
_PROBE_PARAMS_BY_TOOL_NAME: dict[str, type[BaseModel] | None] = {
    "web_agent_snapshot": None,
    "web_agent_get_url": None,
    "web_agent_get_box": WebRefParams,
    "web_agent_get_boxes": WebRefsParams,
    "web_agent_get_ref_states": WebRefsParams,
    "web_agent_viewport": None,
}
_BACKEND_PARAMS_BY_TOOL_NAME: dict[str, type[BaseModel] | None] = {
    "web_agent_backend_open": WebOpenParams,
    "web_agent_backend_click_ref": WebRefParams,
    "web_agent_backend_hover_ref": WebRefParams,
    "web_agent_backend_fill_ref": WebFillRefParams,
    "web_agent_backend_select_option_ref": WebSelectOptionRefParams,
    "web_agent_backend_check_ref": WebRefParams,
    "web_agent_backend_uncheck_ref": WebRefParams,
    "web_agent_backend_drag_ref": WebDragRefParams,
    "web_agent_backend_click_point": WebClickPointParams,
    "web_agent_backend_double_click_point": WebDoubleClickPointParams,
    "web_agent_backend_drag_point": WebDragPointParams,
    "web_agent_backend_type_text": WebTypeTextParams,
    "web_agent_backend_press": WebPressParams,
    "web_agent_backend_scroll": WebScrollParams,
    "web_agent_backend_get_text": WebRefParams,
    "web_agent_backend_get_url": None,
    "web_agent_backend_wait": WebWaitParams,
    "web_agent_backend_screenshot": WebScreenshotParams,
    "web_agent_backend_list_tabs": None,
    "web_agent_backend_new_tab": WebNewTabParams,
    "web_agent_backend_close_tab": WebCloseTabParams,
    "web_agent_backend_focus_tab": WebFocusTabParams,
}
_WEB_AGENT_ACTION_SERVER_NAME = "WebAgentActionExecutor"
_WEB_AGENT_PROBE_SERVER_NAME = "WebAgentProbeExecutor"
_WEB_AGENT_BACKEND_SERVER_NAME = "WebAgentBackendExecutor"


def _web_wait_parameters_schema() -> JsonObject:
    """Return the provider-visible exactly-one schema for ``web_wait``."""

    schema = dict(WebWaitParams.model_json_schema())
    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, dict):  # pragma: no cover - guarded by Pydantic
        raise TypeError("WebWaitParams JSON Schema must define object properties")

    branches: list[JsonObject] = []
    for field_name in ("duration_ms", "load_state", "text", "url_pattern", "selector"):
        property_schema = deepcopy(raw_properties[field_name])
        variants = property_schema.pop("anyOf", None)
        if isinstance(variants, list):
            non_null = next(
                (variant for variant in variants if isinstance(variant, dict) and variant.get("type") != "null"),
                None,
            )
            if non_null is not None:
                property_schema.update(deepcopy(non_null))
        property_schema.pop("default", None)
        branches.append(
            {
                "type": "object",
                "properties": {field_name: property_schema},
                "required": [field_name],
                "additionalProperties": False,
            }
        )
    schema["oneOf"] = branches
    return schema


_WEB_AGENT_MCP_REGISTRATION_HOOK = "northcua.agents.web.mcp.registration:ensure_web_agent_mcp_servers_registered"
_WEB_AGENT_ACTION_TIMEOUT_SECONDS = 60.0
_POST_OBSERVATION_SUFFIX = (
    " Returns a fresh post-action observation when enabled: path-free page state, latest refs/Set-of-Mark "
    "grounding, a clean page image, and for spatial actions an action-overlay history image. Images are attached "
    "to this tool result only when prompt image budget allows."
)

_DESCRIPTION_BY_TOOL_NAME = {
    WebAgentToolName.NOTE.value: (
        "Record important text for later web steps without interacting with the page. Use this sparingly as explicit "
        "durable working memory for discovered facts, values to enter later, mistakes to avoid, constraints, "
        "precautions, and decisions. Do not record routine reasoning or every next action. The tool returns the note "
        "in its text result so it remains visible in tool-call history."
    ),
    WebAgentToolName.OPEN.value: (
        "Open a URL in the current browser session after URL policy validation." + _POST_OBSERVATION_SUFFIX
    ),
    WebAgentToolName.SNAPSHOT.value: (
        "Get the current DOM/accessibility snapshot with numeric element refs such as 3. This read-only "
        "inspection tool does not attach an automatic post-action image."
    ),
    WebAgentToolName.CLICK_REF.value: (
        "Click a numeric element ref from the latest snapshot or direct SoM label, e.g. 3. Use this instead "
        "of viewport coordinates whenever the target has a reliable ref. SoM overlays draw the exact numeric ref "
        "accepted by this tool."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.HOVER_REF.value: (
        "Hover a numeric element ref from the latest snapshot or direct SoM label, e.g. 3. Use this for "
        "hover menus or tooltips when a reliable ref exists; use coordinates only for custom visual targets "
        "without refs."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.FILL_REF.value: (
        "Replace the value of an input-like element ref with text. Use this instead of click/type sequences when "
        "the target field has a reliable numeric ref; use raw typing only when you intentionally need literal keystrokes "
        "into the already focused element." + _POST_OBSERVATION_SUFFIX
    ),
    WebAgentToolName.SELECT_OPTION_REF.value: (
        "Select one or more HTML option value strings on a select element ref. Use this for native dropdowns "
        "instead of clicking option coordinates when a reliable numeric select ref exists."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.CHECK_REF.value: (
        "Set a checkbox or radio element ref to checked without toggling it off accidentally. Use this instead of "
        "clicking when the control has a reliable numeric ref."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.UNCHECK_REF.value: (
        "Set a checkbox element ref to unchecked without toggling it on accidentally. Use this instead of clicking "
        "when the checkbox has a reliable numeric ref."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.DRAG_REF.value: (
        "Drag one numeric element ref from the latest snapshot or direct SoM label onto another element ref. Use this "
        "before coordinate drag when both source and target expose reliable refs."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.CLICK_POINT.value: (
        "Click current viewport CSS coordinates only when no reliable element ref exists, such as canvas, SVG, "
        "custom widgets, or visual-only targets. If the target has a stable numeric ref in the latest snapshot or SoM, "
        "use the ref click action instead. Coordinates come from the current viewport, not document-scroll or "
        "screenshot-image coordinates."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.DOUBLE_CLICK_POINT.value: (
        "Double-click current viewport CSS coordinates only when no reliable element ref exists and the UI truly "
        "requires a double click. If the target has a stable numeric ref, use the ref click action. Coordinates come from "
        "the current viewport, not document-scroll or screenshot-image coordinates."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.DRAG_POINT.value: (
        "Drag from one current viewport CSS coordinate to another using the left mouse button. Use this only when "
        "drag_ref is unavailable because the source or destination lacks a reliable numeric ref; coordinates are current "
        "viewport CSS pixels."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.TYPE_TEXT.value: (
        "Type literal text into the currently focused element. Prefer fill_ref when replacing the value of a "
        "known input ref; use this for incremental typing, shortcuts that require focus, or custom editors."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.PRESS.value: (
        "Press a keyboard key, such as Enter, Tab, ArrowDown, or Escape. Use this for focused controls, keyboard "
        "navigation, submissions, or shortcuts after you are confident focus is correct."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.SCROLL.value: (
        "Scroll the current viewport up, down, left, or right by a CSS-pixel amount. Use this to reveal targets "
        "that are not visible or not present in the current viewport snapshot before falling back to coordinates."
    )
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.GET_TEXT.value: "Read visible text from a numeric element ref in the latest snapshot or direct SoM label.",
    WebAgentToolName.GET_URL.value: "Read the current page URL without changing browser state.",
    WebAgentToolName.WAIT.value: (
        "Wait for one mutually exclusive target. Choose exactly one of: duration_ms for a plain timed pause, "
        "load_state for page lifecycle, text for visible text, url_pattern for navigation, or selector for a DOM "
        "selector. Use this when the page is unsettled or a condition must become true before the next action. "
        "Do not combine wait fields; conflicting targets return a retryable parameter error and no target takes "
        "priority. Omit unused fields; do not provide null or invented placeholder values." + _POST_OBSERVATION_SUFFIX
    ),
    WebAgentToolName.SCREENSHOT.value: (
        "Capture an explicit current-viewport visual inspection screenshot. annotate=false returns a clean screenshot. "
        "annotate=true returns a current-viewport Set-of-Mark screenshot with direct numeric ref labels and bboxes for "
        "visible interactive elements. Inspect long pages viewport by viewport with web_scroll. "
        "This tool returns an image block when available "
        "using the same tool-result image-window budget as post-action images, but does not expose screenshot paths."
    ),
    WebAgentToolName.LIST_TABS.value: "List open browser tabs and their stable tab refs, such as t1 or a label set by web_new_tab.",
    WebAgentToolName.NEW_TAB.value: "Open a new browser tab, optionally with a URL and stable label usable as tab_ref."
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.CLOSE_TAB.value: "Close the active browser tab or a tab by stable tab_ref from web_list_tabs."
    + _POST_OBSERVATION_SUFFIX,
    WebAgentToolName.FOCUS_TAB.value: "Switch focus to a browser tab by stable tab ref from web_list_tabs."
    + _POST_OBSERVATION_SUFFIX,
}


def web_agent_action_contracts() -> tuple[ToolContract, ...]:
    """Return source-of-truth contracts for model-visible web-agent actions."""

    return tuple(
        ToolContract(
            tool_id=tool_name,
            description=_DESCRIPTION_BY_TOOL_NAME[tool_name],
            namespace="web",
            agent_kind="web",
            kind=ToolContractKind.TOOL,
            visibility=ToolVisibility.MODEL_VISIBLE,
            capability=Capability.BROWSER_PAGE,
            params_model=params_model,
            json_schema=_web_wait_parameters_schema() if tool_name == WebAgentToolName.WAIT.value else None,
            provider_schema_mode=(
                ProviderSchemaMode.UNION if tool_name == WebAgentToolName.WAIT.value else ProviderSchemaMode.COMPATIBLE
            ),
            timeout_seconds=_WEB_AGENT_ACTION_TIMEOUT_SECONDS,
            side_effect=tool_name not in _READ_ONLY_TOOLS,
            requires_focus=tool_name in _FOCUS_TOOLS,
            risk_level=ToolRiskLevel.LOW,
            binding=McpBinding(
                server_name=_WEB_AGENT_ACTION_SERVER_NAME,
                registration_hook=_WEB_AGENT_MCP_REGISTRATION_HOOK,
            ),
        )
        for tool_name, params_model in _PARAMS_BY_TOOL_NAME.items()
    )


def web_agent_probe_contracts() -> tuple[ToolContract, ...]:
    """Return source-of-truth contracts for runtime-only web-agent probes."""

    return tuple(
        ToolContract(
            tool_id=tool_name,
            namespace="web",
            agent_kind="web",
            kind=ToolContractKind.PROBE,
            visibility=ToolVisibility.RUNTIME_ONLY,
            capability=Capability.BROWSER_PAGE,
            params_model=params_model,
            timeout_seconds=_WEB_AGENT_ACTION_TIMEOUT_SECONDS,
            side_effect=False,
            requires_focus=False,
            risk_level=ToolRiskLevel.LOW,
            binding=McpBinding(
                server_name=_WEB_AGENT_PROBE_SERVER_NAME,
                registration_hook=_WEB_AGENT_MCP_REGISTRATION_HOOK,
            ),
        )
        for tool_name, params_model in _PROBE_PARAMS_BY_TOOL_NAME.items()
    )


def web_agent_backend_contracts() -> tuple[ToolContract, ...]:
    """Return runtime-only browser backend commands for remote controller dispatch.

    These contracts are intentionally separate from the model-visible
    ``web_*`` action tools. The controller-side remote backend has already
    resolved model-visible numeric refs into browser-owned source refs, so it
    must call this internal execution surface on the device instead of nesting
    through the model-facing MCP action service.
    """

    return tuple(
        ToolContract(
            tool_id=tool_name,
            namespace="web",
            agent_kind="web",
            kind=ToolContractKind.ACTION,
            visibility=ToolVisibility.RUNTIME_ONLY,
            capability=Capability.BROWSER_PAGE,
            params_model=params_model,
            timeout_seconds=_WEB_AGENT_ACTION_TIMEOUT_SECONDS,
            side_effect=tool_name
            not in {
                "web_agent_backend_get_text",
                "web_agent_backend_get_url",
                "web_agent_backend_screenshot",
                "web_agent_backend_list_tabs",
            },
            requires_focus=tool_name
            in {
                "web_agent_backend_click_ref",
                "web_agent_backend_hover_ref",
                "web_agent_backend_fill_ref",
                "web_agent_backend_select_option_ref",
                "web_agent_backend_check_ref",
                "web_agent_backend_uncheck_ref",
                "web_agent_backend_drag_ref",
                "web_agent_backend_click_point",
                "web_agent_backend_double_click_point",
                "web_agent_backend_drag_point",
                "web_agent_backend_type_text",
                "web_agent_backend_press",
                "web_agent_backend_scroll",
            },
            risk_level=ToolRiskLevel.LOW,
            binding=McpBinding(
                server_name=_WEB_AGENT_BACKEND_SERVER_NAME,
                registration_hook=_WEB_AGENT_MCP_REGISTRATION_HOOK,
            ),
        )
        for tool_name, params_model in _BACKEND_PARAMS_BY_TOOL_NAME.items()
    )
