"""Runtime-adapter plugin registry for standalone device workers."""

from __future__ import annotations

from northcua_protocol.capabilities import PlatformKind
from northcua_runtime_shared import RuntimeAdapterRegistry
from northcua_runtime_shared.desktop_surface.profiles import (
    DesktopToolProfile,
    default_desktop_tool_profile,
)

from northcua_device.runtime.agentkit import AgentKitRuntimeAdapter
from northcua_device.runtime.browser import BrowserRuntimeAdapter
from northcua_device.runtime.desktop import DesktopRuntimeAdapter
from northcua_device.runtime.shell import ShellRuntimeAdapter

device_runtime_registry = RuntimeAdapterRegistry()


@device_runtime_registry.register_decorator(
    "desktop",
    platforms=(PlatformKind.MACOS, PlatformKind.WINDOWS, PlatformKind.LINUX),
)
def build_desktop_runtime_adapter(context: object | None) -> DesktopRuntimeAdapter:
    """Build the desktop runtime adapter.

    Args:
        context: Optional worker context carrying platform and CUA enablement
            flags. A missing context falls back to macOS without CUA capability.

    Returns:
        Adapter backed by the registered desktop MCP tool surface.
    """

    return DesktopRuntimeAdapter(tool_profile=default_desktop_tool_profile())


@device_runtime_registry.register_decorator("browser", platforms=(PlatformKind.BROWSER,))
def build_browser_runtime_adapter(context: object | None) -> BrowserRuntimeAdapter:
    """Build the browser runtime adapter.

    Args:
        context: Optional worker context containing AgentKit agent filters.

    Returns:
        Browser adapter backed by AgentKit when the main NorthCUA package is installed.
    """

    agent_kinds = getattr(context, "agentkit_agent_kinds", set()) if context is not None else set()
    return BrowserRuntimeAdapter(agent_kinds=set(agent_kinds))


@device_runtime_registry.register_decorator(
    "agentkit",
    platforms=(
        PlatformKind.MACOS,
        PlatformKind.WINDOWS,
        PlatformKind.LINUX,
        PlatformKind.ANDROID,
        PlatformKind.IOS,
        PlatformKind.VM,
    ),
)
def build_agentkit_runtime_adapter(context: object | None) -> AgentKitRuntimeAdapter:
    """Build the generic AgentKit runtime adapter for low-code custom agents.

    Args:
        context: Optional worker context containing platform and agent filters.

    Returns:
        Lazy adapter that delegates to the main package when installed.
    """

    platform = getattr(context, "platform", None)
    agent_kinds = getattr(context, "agentkit_agent_kinds", set()) if context is not None else set()
    platforms = {platform} if isinstance(platform, PlatformKind) else None
    return AgentKitRuntimeAdapter(agent_kinds=set(agent_kinds), platforms=platforms)


@device_runtime_registry.register_decorator(
    "shell",
    platforms=(
        PlatformKind.MACOS,
        PlatformKind.WINDOWS,
        PlatformKind.LINUX,
        PlatformKind.BROWSER,
        PlatformKind.ANDROID,
        PlatformKind.IOS,
        PlatformKind.VM,
    ),
)
def build_shell_runtime_adapter(_: object | None) -> ShellRuntimeAdapter:
    """Build the optional shell runtime adapter.

    Returns:
        Shell adapter; worker config decides whether its tool is allowlisted.
    """

    return ShellRuntimeAdapter()
