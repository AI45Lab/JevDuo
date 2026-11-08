"""Model-visible MCP action server for the generic web agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastmcp import FastMCP

from northcua.core.image_payloads import image_data_url_from_path
from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry

from ..backend import WebActionCommandResult
from ..domain.actions import (
    WebAgentActionType,
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
    WebScreenshotParams,
    WebScrollParams,
    WebSelectOptionRefParams,
    WebTypeTextParams,
    WebWaitParams,
    action_from_tool_name,
    action_tool_name_for_action,
)
from ..domain.key_names import canonical_web_key_name
from ..session.models import RegisteredWebAgentSession
from .action_service import WebAgentMCPActionService
from .descriptors import web_agent_action_contracts

BrowserOperation = Callable[[RegisteredWebAgentSession], Awaitable[WebActionCommandResult]]


def _attach_action_overlay_image_url(image_urls: dict[str, object], overlay_path: str) -> None:
    """Attach the pre-action overlay as an inline image for remote materialization."""

    overlay_url = image_data_url_from_path(overlay_path)
    if overlay_url:
        image_urls.setdefault("action_overlay_image_url", overlay_url)


def _register_page_tools(mcp: FastMCP, service: WebAgentMCPActionService) -> None:
    """Register page-level navigation and observation tools."""

    register_contract_handlers(
        mcp,
        web_agent_action_contracts(),
        _web_action_handler_map(service),
        error_handler=lambda tool_id, exc: _web_action_error_payload(service, tool_id, exc),
    )


def _web_action_error_payload(
    service: WebAgentMCPActionService,
    tool_id: str,
    exc: Exception,
) -> dict[str, object]:
    action = action_from_tool_name(tool_id) or WebAgentActionType.WAIT
    service.record_preflight_tool_invocation(action)
    return service.error_payload(action, exc)


def _web_action_handler_map(service: WebAgentMCPActionService) -> dict[str, Callable[..., object]]:
    return {
        action_tool_name_for_action(WebAgentActionType.NOTE): lambda params: _web_note(params, service),
        action_tool_name_for_action(WebAgentActionType.OPEN): lambda params: _web_open(params, service),
        action_tool_name_for_action(WebAgentActionType.SNAPSHOT): lambda params: _web_snapshot(params, service),
        action_tool_name_for_action(WebAgentActionType.CLICK_REF): lambda params: _web_click_ref(params, service),
        action_tool_name_for_action(WebAgentActionType.HOVER_REF): lambda params: _web_hover_ref(params, service),
        action_tool_name_for_action(WebAgentActionType.FILL_REF): lambda params: _web_fill_ref(params, service),
        action_tool_name_for_action(WebAgentActionType.SELECT_OPTION_REF): lambda params: _web_select_option_ref(
            params, service
        ),
        action_tool_name_for_action(WebAgentActionType.CHECK_REF): lambda params: _web_check_ref(params, service),
        action_tool_name_for_action(WebAgentActionType.UNCHECK_REF): lambda params: _web_uncheck_ref(params, service),
        action_tool_name_for_action(WebAgentActionType.DRAG_REF): lambda params: _web_drag_ref(params, service),
        action_tool_name_for_action(WebAgentActionType.CLICK_POINT): lambda params: _web_click_point(params, service),
        action_tool_name_for_action(WebAgentActionType.DOUBLE_CLICK_POINT): lambda params: _web_double_click_point(
            params, service
        ),
        action_tool_name_for_action(WebAgentActionType.DRAG_POINT): lambda params: _web_drag_point(params, service),
        action_tool_name_for_action(WebAgentActionType.TYPE_TEXT): lambda params: _web_type_text(params, service),
        action_tool_name_for_action(WebAgentActionType.PRESS): lambda params: _web_press(params, service),
        action_tool_name_for_action(WebAgentActionType.SCROLL): lambda params: _web_scroll(params, service),
        action_tool_name_for_action(WebAgentActionType.GET_TEXT): lambda params: _web_get_text(params, service),
        action_tool_name_for_action(WebAgentActionType.GET_URL): lambda params: _web_get_url(params, service),
        action_tool_name_for_action(WebAgentActionType.WAIT): lambda params: _web_wait(params, service),
        action_tool_name_for_action(WebAgentActionType.SCREENSHOT): lambda params: _web_screenshot(params, service),
        action_tool_name_for_action(WebAgentActionType.LIST_TABS): lambda params: _web_list_tabs(params, service),
        action_tool_name_for_action(WebAgentActionType.NEW_TAB): lambda params: _web_new_tab(params, service),
        action_tool_name_for_action(WebAgentActionType.CLOSE_TAB): lambda params: _web_close_tab(params, service),
        action_tool_name_for_action(WebAgentActionType.FOCUS_TAB): lambda params: _web_focus_tab(params, service),
    }


async def _run_mutating_browser_action(
    *,
    action: WebAgentActionType,
    params,
    service: WebAgentMCPActionService,
    text: str,
    operation: BrowserOperation,
) -> dict[str, object]:
    async def _call(_session_id: str, registered) -> dict[str, object]:
        action_params = params.model_dump(mode="json", exclude_none=True)
        overlay_params = service.action_overlay_params(
            registered,
            action=action,
            params=action_params,
        )
        action_overlay_path = await service.capture_pre_action_observation(
            registered,
            action=action,
            params=overlay_params,
        )
        result = await operation(registered)
        command_payload = service.project_command_payload_refs(registered, service.command_result_data(result))
        observation, image_urls, observation_error = await service.best_effort_post_action_observation_payload(
            registered,
            action=action,
        )
        _attach_action_overlay_image_url(image_urls, action_overlay_path)
        return service.attach_observation_payload(
            service.ok_payload(
                action=action,
                text=text,
                result=command_payload,
                params=action_params,
            ),
            observation=observation,
            image_urls=image_urls,
            observation_error=observation_error,
        )

    return await service.run_with_session(action, _call)


async def _web_note(params: WebNoteParams, _service: WebAgentMCPActionService) -> dict[str, object]:
    clean_text = params.text.strip()
    clean_reason = params.reason.strip()
    note_kind = params.kind
    result_text = f"[{note_kind}] {clean_text}"
    if clean_reason:
        result_text = f"{result_text}\nReason: {clean_reason}"
    return {
        "text": result_text,
        "status": "recorded",
        "action": WebAgentActionType.NOTE.value,
        "kind": note_kind,
        "note_text": clean_text,
        "reason": clean_reason,
        "screenshot_policy": "none",
    }


async def _web_open(params: WebOpenParams, service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(session_id: str, registered) -> dict[str, object]:
        del session_id
        action_overlay_path = await service.capture_pre_action_observation(
            registered,
            action=WebAgentActionType.OPEN,
            params=params.model_dump(mode="json"),
        )
        validated_url = service.validate_url(registered, params.url)
        result = await registered.client.open(validated_url)
        payload = service.command_result_data(result)
        observation, image_urls, observation_error = await service.best_effort_post_action_observation_payload(
            registered,
            action=WebAgentActionType.OPEN,
        )
        _attach_action_overlay_image_url(image_urls, action_overlay_path)
        return service.attach_observation_payload(
            service.ok_payload(
                action=WebAgentActionType.OPEN,
                text=f"Opened {validated_url}.",
                result=payload,
                params=params.model_dump(mode="json"),
            ),
            observation=observation,
            image_urls=image_urls,
            observation_error=observation_error,
        )

    return await service.run_with_session(WebAgentActionType.OPEN, _call)


async def _web_snapshot(_params: dict[str, object], service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(_session_id: str, registered) -> dict[str, object]:
        snapshot = await registered.client.snapshot()
        service.validate_observed_url(registered, snapshot.url)
        observation = service.observation_projector.build_bundle(
            service._snapshot_observation_context(snapshot, observation_id="snapshot-tool")
        )
        service.update_ref_aliases_from_observation(
            registered,
            service._snapshot_observation_context(snapshot, observation_id="snapshot-tool"),
        )
        observation_payload = observation.model_dump(mode="json")
        return service.ok_payload(
            action=WebAgentActionType.SNAPSHOT,
            text="Captured current snapshot.",
            result={
                "url": observation.page.url,
                "title": observation.page.title,
                "snapshot": observation.snapshot.text,
                "refs": observation.snapshot.refs,
                "ref_count": observation.snapshot.ref_count,
                "observation": observation_payload,
            },
        )

    return await service.run_with_session(WebAgentActionType.SNAPSHOT, _call)


async def _web_click_ref(params: WebRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.CLICK_REF,
        params=params,
        service=service,
        text=f"Clicked {params.ref}.",
        operation=lambda registered: registered.client.click_ref(service.resolve_model_ref(registered, params.ref)),
    )


async def _web_hover_ref(params: WebRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.HOVER_REF,
        params=params,
        service=service,
        text=f"Hovered {params.ref}.",
        operation=lambda registered: registered.client.hover_ref(service.resolve_model_ref(registered, params.ref)),
    )


async def _web_fill_ref(params: WebFillRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.FILL_REF,
        params=params,
        service=service,
        text=f"Filled {params.ref}.",
        operation=lambda registered: registered.client.fill_ref(
            service.resolve_model_ref(registered, params.ref), params.text
        ),
    )


async def _web_select_option_ref(
    params: WebSelectOptionRefParams,
    service: WebAgentMCPActionService,
) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.SELECT_OPTION_REF,
        params=params,
        service=service,
        text=f"Selected option values on {params.ref}.",
        operation=lambda registered: registered.client.select_option_ref(
            service.resolve_model_ref(registered, params.ref),
            params.values,
        ),
    )


async def _web_check_ref(params: WebRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.CHECK_REF,
        params=params,
        service=service,
        text=f"Checked {params.ref}.",
        operation=lambda registered: registered.client.check_ref(service.resolve_model_ref(registered, params.ref)),
    )


async def _web_uncheck_ref(params: WebRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.UNCHECK_REF,
        params=params,
        service=service,
        text=f"Unchecked {params.ref}.",
        operation=lambda registered: registered.client.uncheck_ref(service.resolve_model_ref(registered, params.ref)),
    )


async def _web_drag_ref(params: WebDragRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.DRAG_REF,
        params=params,
        service=service,
        text=f"Dragged {params.from_ref} to {params.to_ref}.",
        operation=lambda registered: registered.client.drag_ref(
            service.resolve_model_ref(registered, params.from_ref),
            service.resolve_model_ref(registered, params.to_ref),
        ),
    )


async def _web_click_point(params: WebClickPointParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.CLICK_POINT,
        params=params,
        service=service,
        text=f"Clicked viewport point ({params.x}, {params.y}).",
        operation=lambda registered: registered.client.click_point(params.x, params.y, params.button),
    )


async def _web_double_click_point(
    params: WebDoubleClickPointParams,
    service: WebAgentMCPActionService,
) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.DOUBLE_CLICK_POINT,
        params=params,
        service=service,
        text=f"Double-clicked viewport point ({params.x}, {params.y}).",
        operation=lambda registered: registered.client.double_click_point(params.x, params.y, params.button),
    )


async def _web_drag_point(params: WebDragPointParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.DRAG_POINT,
        params=params,
        service=service,
        text=f"Dragged viewport point ({params.from_x}, {params.from_y}) to ({params.to_x}, {params.to_y}).",
        operation=lambda registered: registered.client.drag_point(
            params.from_x,
            params.from_y,
            params.to_x,
            params.to_y,
        ),
    )


async def _web_type_text(params: WebTypeTextParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.TYPE_TEXT,
        params=params,
        service=service,
        text="Typed text.",
        operation=lambda registered: registered.client.type_text(params.text),
    )


async def _web_press(params: WebPressParams, service: WebAgentMCPActionService) -> dict[str, object]:
    canonical_key = canonical_web_key_name(params.key)
    canonical_params = params.model_copy(update={"key": canonical_key})
    return await _run_mutating_browser_action(
        action=WebAgentActionType.PRESS,
        params=canonical_params,
        service=service,
        text=f"Pressed {canonical_key}.",
        operation=lambda registered: registered.client.press(canonical_key),
    )


async def _web_scroll(params: WebScrollParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.SCROLL,
        params=params,
        service=service,
        text=f"Scrolled {params.direction.value}.",
        operation=lambda registered: registered.client.scroll(params.direction, params.amount),
    )


async def _web_get_text(params: WebRefParams, service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(_session_id: str, registered) -> dict[str, object]:
        result = await registered.client.get_text(service.resolve_model_ref(registered, params.ref))
        return service.ok_payload(
            action=WebAgentActionType.GET_TEXT,
            text=f"Read text from {params.ref}.",
            result=service.command_result_data(result),
            params=params.model_dump(mode="json"),
        )

    return await service.run_with_session(WebAgentActionType.GET_TEXT, _call)


async def _web_get_url(_params: dict[str, object], service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(_session_id: str, registered) -> dict[str, object]:
        result = await registered.client.get_url()
        payload = service.command_result_data(result)
        if isinstance(payload, dict):
            service.validate_observed_url(registered, str(payload.get("url") or ""))
        elif isinstance(payload, str):
            service.validate_observed_url(registered, payload)
        return service.ok_payload(
            action=WebAgentActionType.GET_URL,
            text="Read current URL.",
            result=payload,
        )

    return await service.run_with_session(WebAgentActionType.GET_URL, _call)


async def _web_wait(params: WebWaitParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.WAIT,
        params=params,
        service=service,
        text="Wait completed.",
        operation=lambda registered: registered.client.wait(params),
    )


async def _web_screenshot(params: WebScreenshotParams, service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(session_id: str, registered) -> dict[str, object]:
        del session_id
        await service.validate_current_url(registered)
        snapshot = await registered.client.snapshot() if params.annotate else None
        screenshot = await registered.client.screenshot(annotate=False)
        screenshot = service.persist_screenshot_if_needed(registered, screenshot)
        observation_payload: dict[str, object] | None = None
        if params.annotate and snapshot is not None:
            screenshot = await service.annotate_screenshot_with_marks(registered, screenshot, snapshot)
            observation = service._snapshot_observation_context(
                snapshot,
                observation_id="screenshot-tool",
            ).model_copy(
                update={"screenshot": screenshot},
            )
            service.update_ref_aliases_from_observation(registered, observation)
            observation_payload = service.observation_projector.bundle_dict(observation)
        payload = service.ok_payload(
            action=WebAgentActionType.SCREENSHOT,
            text="Captured annotated viewport screenshot." if params.annotate else "Captured screenshot.",
            result=service.screenshot_result_payload(screenshot),
            params=params.model_dump(mode="json"),
        )
        if observation_payload is not None:
            payload["observation"] = observation_payload
        if screenshot.data_url:
            payload["screenshot_url"] = screenshot.data_url
        return payload

    return await service.run_with_session(WebAgentActionType.SCREENSHOT, _call)


async def _web_list_tabs(_params: dict[str, object], service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(_session_id: str, registered) -> dict[str, object]:
        result = await registered.client.list_tabs()
        return service.ok_payload(
            action=WebAgentActionType.LIST_TABS,
            text="Listed browser tabs.",
            result=service.command_result_data(result),
        )

    return await service.run_with_session(WebAgentActionType.LIST_TABS, _call)


async def _web_new_tab(params: WebNewTabParams, service: WebAgentMCPActionService) -> dict[str, object]:
    async def _call(_session_id: str, registered) -> dict[str, object]:
        action_overlay_path = await service.capture_pre_action_observation(
            registered,
            action=WebAgentActionType.NEW_TAB,
            params=params.model_dump(mode="json", exclude_none=True),
        )
        validated_url = service.validate_url(registered, params.url) if params.url else None
        result = await registered.client.new_tab(url=validated_url, label=params.label)
        payload = service.command_result_data(result)
        observation, image_urls, observation_error = await service.best_effort_post_action_observation_payload(
            registered,
            action=WebAgentActionType.NEW_TAB,
        )
        _attach_action_overlay_image_url(image_urls, action_overlay_path)
        payload_params = params.model_dump(mode="json", exclude_none=True)
        if validated_url:
            payload_params["url"] = validated_url
        return service.attach_observation_payload(
            service.ok_payload(
                action=WebAgentActionType.NEW_TAB,
                text="Opened a new browser tab.",
                result=payload,
                params=payload_params,
            ),
            observation=observation,
            image_urls=image_urls,
            observation_error=observation_error,
        )

    return await service.run_with_session(WebAgentActionType.NEW_TAB, _call)


async def _web_close_tab(params: WebCloseTabParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.CLOSE_TAB,
        params=params,
        service=service,
        text="Closed browser tab.",
        operation=lambda registered: registered.client.close_tab(params.tab_ref),
    )


async def _web_focus_tab(params: WebFocusTabParams, service: WebAgentMCPActionService) -> dict[str, object]:
    return await _run_mutating_browser_action(
        action=WebAgentActionType.FOCUS_TAB,
        params=params,
        service=service,
        text=f"Focused browser tab {params.tab_ref}.",
        operation=lambda registered: registered.client.focus_tab(params.tab_ref),
    )


@NorthCUAMCPRegistry.register_decorator(
    "WebAgentActionExecutor",
    tool_contracts=web_agent_action_contracts(),
)
def create_web_agent_action_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create the model-visible action MCP server for the generic web agent.

    Args:
        context: MCP factory context supplied by the NorthCUA registry.

    Returns:
        FastMCP server exposing typed web action tools.
    """

    del context
    mcp = FastMCP("NorthCUA Web Agent Action Executor")
    service = WebAgentMCPActionService()
    _register_page_tools(mcp, service)
    return mcp
