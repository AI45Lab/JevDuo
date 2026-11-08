"""Runtime-only browser backend command server for remote web sessions.

This server is the device-owned execution surface used by the controller-side
``RemoteWebActionBackend``. It accepts browser source refs such as ``@e39`` and
calls the actual browser backend directly. Model-visible ``web_*`` tools stay in
``action_server.py`` and continue to accept only compact numeric refs.
"""

from __future__ import annotations

from fastmcp import FastMCP

from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.invocation_context import current_mcp_invocation_context
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry

from ..domain.actions import (
    WebClickPointParams,
    WebCloseTabParams,
    WebDoubleClickPointParams,
    WebDragPointParams,
    WebDragRefParams,
    WebFillRefParams,
    WebFocusTabParams,
    WebNewTabParams,
    WebOpenParams,
    WebPressParams,
    WebRefParams,
    WebScreenshotParams,
    WebScrollParams,
    WebSelectOptionRefParams,
    WebTypeTextParams,
    WebWaitParams,
)
from ..domain.key_names import canonical_web_key_name
from ..observation.ref_alias import canonical_source_ref
from ..session.registry import WebAgentSessionRegistry
from .descriptors import web_agent_backend_contracts


def _source_ref(ref: object) -> str:
    return canonical_source_ref(ref)


def _result_data(result) -> object:
    return result.require_success().data


@NorthCUAMCPRegistry.register_decorator(
    "WebAgentBackendExecutor",
    tool_contracts=web_agent_backend_contracts(),
)
def create_web_agent_backend_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create runtime-only browser backend commands for remote dispatch."""

    del context
    registry = WebAgentSessionRegistry()
    mcp = FastMCP("NorthCUA Web Agent Backend Executor")

    def _registered():
        return registry.get_registered_session(current_mcp_invocation_context().session_id)

    async def _open(params: WebOpenParams) -> object:
        return _result_data(await _registered().client.open(params.url))

    async def _click_ref(params: WebRefParams) -> object:
        return _result_data(await _registered().client.click_ref(_source_ref(params.ref)))

    async def _hover_ref(params: WebRefParams) -> object:
        return _result_data(await _registered().client.hover_ref(_source_ref(params.ref)))

    async def _fill_ref(params: WebFillRefParams) -> object:
        return _result_data(await _registered().client.fill_ref(_source_ref(params.ref), params.text))

    async def _select_option_ref(params: WebSelectOptionRefParams) -> object:
        return _result_data(await _registered().client.select_option_ref(_source_ref(params.ref), params.values))

    async def _check_ref(params: WebRefParams) -> object:
        return _result_data(await _registered().client.check_ref(_source_ref(params.ref)))

    async def _uncheck_ref(params: WebRefParams) -> object:
        return _result_data(await _registered().client.uncheck_ref(_source_ref(params.ref)))

    async def _drag_ref(params: WebDragRefParams) -> object:
        return _result_data(
            await _registered().client.drag_ref(_source_ref(params.from_ref), _source_ref(params.to_ref))
        )

    async def _click_point(params: WebClickPointParams) -> object:
        return _result_data(await _registered().client.click_point(params.x, params.y, params.button))

    async def _double_click_point(params: WebDoubleClickPointParams) -> object:
        return _result_data(await _registered().client.double_click_point(params.x, params.y, params.button))

    async def _drag_point(params: WebDragPointParams) -> object:
        return _result_data(
            await _registered().client.drag_point(params.from_x, params.from_y, params.to_x, params.to_y)
        )

    async def _type_text(params: WebTypeTextParams) -> object:
        return _result_data(await _registered().client.type_text(params.text))

    async def _press(params: WebPressParams) -> object:
        return _result_data(await _registered().client.press(canonical_web_key_name(params.key)))

    async def _scroll(params: WebScrollParams) -> object:
        return _result_data(await _registered().client.scroll(params.direction, params.amount))

    async def _get_text(params: WebRefParams) -> object:
        return _result_data(await _registered().client.get_text(_source_ref(params.ref)))

    async def _get_url(params: dict[str, object]) -> object:
        del params
        return _result_data(await _registered().client.get_url())

    async def _wait(params: WebWaitParams) -> object:
        return _result_data(await _registered().client.wait(params))

    async def _screenshot(params: WebScreenshotParams) -> dict[str, object]:
        return (await _registered().client.screenshot(annotate=params.annotate)).model_dump(mode="json")

    async def _list_tabs(params: dict[str, object]) -> object:
        del params
        return _result_data(await _registered().client.list_tabs())

    async def _new_tab(params: WebNewTabParams) -> object:
        return _result_data(await _registered().client.new_tab(url=params.url, label=params.label))

    async def _close_tab(params: WebCloseTabParams) -> object:
        return _result_data(await _registered().client.close_tab(params.tab_ref))

    async def _focus_tab(params: WebFocusTabParams) -> object:
        return _result_data(await _registered().client.focus_tab(params.tab_ref))

    register_contract_handlers(
        mcp,
        web_agent_backend_contracts(),
        {
            "web_agent_backend_open": _open,
            "web_agent_backend_click_ref": _click_ref,
            "web_agent_backend_hover_ref": _hover_ref,
            "web_agent_backend_fill_ref": _fill_ref,
            "web_agent_backend_select_option_ref": _select_option_ref,
            "web_agent_backend_check_ref": _check_ref,
            "web_agent_backend_uncheck_ref": _uncheck_ref,
            "web_agent_backend_drag_ref": _drag_ref,
            "web_agent_backend_click_point": _click_point,
            "web_agent_backend_double_click_point": _double_click_point,
            "web_agent_backend_drag_point": _drag_point,
            "web_agent_backend_type_text": _type_text,
            "web_agent_backend_press": _press,
            "web_agent_backend_scroll": _scroll,
            "web_agent_backend_get_text": _get_text,
            "web_agent_backend_get_url": _get_url,
            "web_agent_backend_wait": _wait,
            "web_agent_backend_screenshot": _screenshot,
            "web_agent_backend_list_tabs": _list_tabs,
            "web_agent_backend_new_tab": _new_tab,
            "web_agent_backend_close_tab": _close_tab,
            "web_agent_backend_focus_tab": _focus_tab,
        },
    )
    return mcp
