"""FastMCP tool registration helpers for the Playwright browser runtime."""

from __future__ import annotations

from typing import Callable, Optional, cast

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from northcua.runtime.browser.contracts import PLAYWRIGHT_BROWSER_TOOL_CONTRACTS
from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.invocation_context import current_mcp_invocation_context

from .backend import BrowserElementInfo, BrowserTabInfo, PlaywrightBrowserBackend, PythonPlaywrightBrowserBackend
from .mcp_payloads import (
    _normalize_url,
    _render_snapshot_markdown,
    _render_tabs_markdown,
)


class PlaywrightBrowserServerState:
    """Shared singleton state for the local Playwright MCP server.

    Attributes:
        backends_by_session: Browser backend cache keyed by invocation session.
        snapshots_by_session: Element-ref cache keyed by session and tab index.
    """

    _instance: Optional["PlaywrightBrowserServerState"] = None

    def __new__(cls) -> "PlaywrightBrowserServerState":
        """Return the singleton server state instance.

        Returns:
            Shared Playwright browser server state.
        """

        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.backends_by_session = {}
            cls._instance.snapshots_by_session = {}
        return cls._instance

    def reset(self) -> None:
        """Stop tracked backends and clear all cached snapshot references."""

        for backend in self.backends_by_session.values():
            backend.stop()
        self.backends_by_session = {}
        self.snapshots_by_session = {}


def _register_playwright_browser_tools(
    mcp: FastMCP,
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> None:
    register_contract_handlers(
        mcp,
        PLAYWRIGHT_BROWSER_TOOL_CONTRACTS,
        _browser_handler_map(state=state, backend_factory=backend_factory),
    )


def _session_key() -> str:
    return current_mcp_invocation_context().session_id or "__default__"


def _get_backend(
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None = None,
) -> PlaywrightBrowserBackend:
    key = _session_key()
    backend = state.backends_by_session.get(key)
    if backend is None:
        backend = backend_factory() if backend_factory is not None else PythonPlaywrightBrowserBackend()
        state.backends_by_session[key] = backend
    return backend


def _clear_snapshot_refs(state: PlaywrightBrowserServerState, tab_index: int | None = None) -> None:
    key = _session_key()
    if tab_index is None:
        state.snapshots_by_session.pop(key, None)
        return
    state.snapshots_by_session.setdefault(key, {}).pop(tab_index, None)


def _current_tab_index(backend: PlaywrightBrowserBackend) -> int:
    for tab in backend.list_tabs():
        if tab.is_active:
            return tab.index
    return 0


def _resolve_selector(
    state: PlaywrightBrowserServerState,
    backend: PlaywrightBrowserBackend,
    *,
    ref: str | None,
    selector: str | None,
) -> str:
    if selector:
        return str(selector)
    if not ref:
        raise ToolError("Either ref or selector is required.")
    snapshot_refs = state.snapshots_by_session.get(_session_key(), {}).get(_current_tab_index(backend), {})
    element = snapshot_refs.get(str(ref))
    if element is None:
        raise ToolError(f"Unknown ref '{ref}'. Run browser_snapshot first to refresh element refs.")
    return element.selector


def _browser_handler_map(
    *,
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> dict[str, Callable[[dict[str, object]], str]]:
    return {
        "browser_tabs": lambda params: _browser_tabs(params, state, backend_factory),
        "browser_navigate": lambda params: _browser_navigate(params, state, backend_factory),
        "browser_navigate_back": lambda params: _browser_navigate_back(params, state, backend_factory),
        "browser_navigate_forward": lambda params: _browser_navigate_forward(params, state, backend_factory),
        "browser_reload": lambda params: _browser_reload(params, state, backend_factory),
        "browser_snapshot": lambda params: _browser_snapshot(params, state, backend_factory),
        "browser_click": lambda params: _browser_click(params, state, backend_factory),
        "browser_drag": lambda params: _browser_drag(params, state, backend_factory),
        "browser_hover": lambda params: _browser_hover(params, state, backend_factory),
        "browser_select_option": lambda params: _browser_select_option(params, state, backend_factory),
        "browser_generate_locator": lambda params: _browser_generate_locator(params, state, backend_factory),
        "browser_check": lambda params: _browser_check(params, state, backend_factory),
        "browser_uncheck": lambda params: _browser_uncheck(params, state, backend_factory),
    }


def _browser_tabs(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    normalized_action = str(params.get("action") or "").lower()
    index = cast(int | None, params.get("index"))
    if normalized_action == "list":
        pass
    elif normalized_action == "new":
        backend.new_tab()
        _clear_snapshot_refs(state)
    elif normalized_action == "close":
        backend.close_tab(index=index)
        _clear_snapshot_refs(state)
    elif normalized_action == "select":
        if index is None:
            raise ToolError("Tab index is required for select.")
        backend.select_tab(index)
        _clear_snapshot_refs(state)
    else:
        raise ToolError("Unsupported browser_tabs action. Expected one of list, new, close, or select.")
    return _render_tabs_markdown(backend.list_tabs())


def _browser_navigate(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    final_url = backend.navigate(_normalize_url(str(params["url"])))
    _clear_snapshot_refs(state, _current_tab_index(backend))
    return f"Navigated to {final_url}."


def _browser_navigate_back(
    _params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    backend.navigate_back()
    _clear_snapshot_refs(state, _current_tab_index(backend))
    return "Navigated back."


def _browser_navigate_forward(
    _params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    backend.navigate_forward()
    _clear_snapshot_refs(state, _current_tab_index(backend))
    return "Navigated forward."


def _browser_reload(
    _params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    backend.reload()
    _clear_snapshot_refs(state, _current_tab_index(backend))
    return "Reloaded the current page."


def _browser_snapshot(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    snapshot = backend.capture_snapshot(
        selector=_optional_str(params.get("selector")), depth=_optional_int(params.get("depth"))
    )
    ref_map: dict[str, BrowserElementInfo] = {
        f"e{index + 1}": element for index, element in enumerate(snapshot.elements)
    }
    state.snapshots_by_session.setdefault(_session_key(), {})[_current_tab_index(backend)] = ref_map
    markdown = _render_snapshot_markdown(snapshot, ref_map)
    return markdown


def _browser_click(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    ref = _optional_str(params.get("ref"))
    selector = _optional_str(params.get("selector"))
    double_click = bool(params.get("doubleClick"))
    resolved_selector = _resolve_selector(state, backend, ref=ref, selector=selector)
    backend.click(
        resolved_selector,
        button=_optional_str(params.get("button")),
        double_click=double_click,
        modifiers=_optional_str_list(params.get("modifiers")),
    )
    return f"Successfully {'double clicked' if double_click else 'clicked'} {ref or resolved_selector}."


def _browser_drag(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    start_ref = _optional_str(params.get("startRef"))
    end_ref = _optional_str(params.get("endRef"))
    start_selector = _optional_str(params.get("startSelector"))
    end_selector = _optional_str(params.get("endSelector"))
    backend.drag(
        _resolve_selector(state, backend, ref=start_ref, selector=start_selector),
        _resolve_selector(state, backend, ref=end_ref, selector=end_selector),
    )
    return f"Dragged {start_ref or start_selector} onto {end_ref or end_selector}."


def _browser_hover(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    ref = _optional_str(params.get("ref"))
    selector = _optional_str(params.get("selector"))
    resolved_selector = _resolve_selector(state, backend, ref=ref, selector=selector)
    backend.hover(resolved_selector)
    return f"Hovered over {ref or resolved_selector}."


def _browser_select_option(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    ref = _optional_str(params.get("ref"))
    selector = _optional_str(params.get("selector"))
    values = [str(value) for value in cast(list[object], params["values"])]
    resolved_selector = _resolve_selector(state, backend, ref=ref, selector=selector)
    backend.select_option(resolved_selector, values)
    return f"Selected {values} on {ref or resolved_selector}."


def _browser_generate_locator(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    return _resolve_selector(
        state,
        backend,
        ref=_optional_str(params.get("ref")),
        selector=_optional_str(params.get("selector")),
    )


def _browser_check(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    ref = _optional_str(params.get("ref"))
    selector = _optional_str(params.get("selector"))
    resolved_selector = _resolve_selector(state, backend, ref=ref, selector=selector)
    backend.set_checked(resolved_selector, checked=True)
    return f"Checked {ref or resolved_selector}."


def _browser_uncheck(
    params: dict[str, object],
    state: PlaywrightBrowserServerState,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None,
) -> str:
    backend = _get_backend(state, backend_factory)
    ref = _optional_str(params.get("ref"))
    selector = _optional_str(params.get("selector"))
    resolved_selector = _resolve_selector(state, backend, ref=ref, selector=selector)
    backend.set_checked(resolved_selector, checked=False)
    return f"Unchecked {ref or resolved_selector}."


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(cast(int, value))


def _optional_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    return [str(item) for item in cast(list[object], value)]
