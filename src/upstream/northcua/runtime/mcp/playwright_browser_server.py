"""Local Playwright MCP server aligned with the old PlaywrightBrowserExecutor."""

from __future__ import annotations

from typing import Callable

from fastmcp import FastMCP

from northcua.runtime.browser import (
    BrowserElementInfo,
    BrowserSnapshotResult,
    BrowserTabInfo,
    PlaywrightBrowserBackend,
)
from northcua.runtime.browser.contracts import PLAYWRIGHT_BROWSER_SERVER_NAME, PLAYWRIGHT_BROWSER_TOOL_CONTRACTS
from northcua.runtime.browser.mcp_tools import PlaywrightBrowserServerState, _register_playwright_browser_tools
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry

__all__ = [
    "BrowserElementInfo",
    "BrowserSnapshotResult",
    "BrowserTabInfo",
    "MCPFactoryContext",
    "PlaywrightBrowserBackend",
    "PlaywrightBrowserServerState",
    "create_playwright_browser_mcp_server",
]


@NorthCUAMCPRegistry.register_decorator(
    PLAYWRIGHT_BROWSER_SERVER_NAME,
    tool_contracts=PLAYWRIGHT_BROWSER_TOOL_CONTRACTS,
)
def create_playwright_browser_mcp_server(
    context: MCPFactoryContext,
    *,
    backend_factory: Callable[[], PlaywrightBrowserBackend] | None = None,
) -> FastMCP:
    """Create the local Playwright browser FastMCP server.

    Args:
        context: MCP factory context supplied by the registry.
        backend_factory: Optional backend factory for tests or custom runtimes.

    Returns:
        FastMCP server exposing browser automation tools.

    Raises:
        ToolError: Propagated by nested FastMCP tools when browser commands
            receive invalid refs, tabs, URLs, or actions.
    """

    del context
    state = PlaywrightBrowserServerState()
    mcp = FastMCP("NorthCUA Playwright Browser Executor")
    _register_playwright_browser_tools(mcp, state, backend_factory)
    return mcp
