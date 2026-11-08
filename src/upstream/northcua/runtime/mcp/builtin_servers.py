"""Registration helpers for NorthCUA's built-in local MCP servers."""

from __future__ import annotations


def ensure_builtin_mcp_servers_registered() -> None:
    """Import all built-in MCP server modules so registry decorators run."""

    from . import accessibility_server as _accessibility_server  # noqa: F401
    from . import cli_server as _cli_server  # noqa: F401
    from . import desktop_native_server as _desktop_native_server  # noqa: F401
    from . import device_shell_server as _device_shell_server  # noqa: F401
    from . import playwright_browser_server as _playwright_browser_server  # noqa: F401
