"""Runtime adapters for NorthCUA device workers."""

from northcua_device.runtime.agentkit import AgentKitRuntimeAdapter
from northcua_device.runtime.browser import BrowserRuntimeAdapter
from northcua_device.runtime.desktop import DesktopRuntimeAdapter
from northcua_device.runtime.registry import device_runtime_registry
from northcua_device.runtime.shell import EchoRuntimeAdapter, ShellRuntimeAdapter
from northcua_runtime_shared import RuntimeAdapter, RuntimeHealth

__all__ = [
    "BrowserRuntimeAdapter",
    "AgentKitRuntimeAdapter",
    "DesktopRuntimeAdapter",
    "EchoRuntimeAdapter",
    "RuntimeAdapter",
    "RuntimeHealth",
    "ShellRuntimeAdapter",
    "device_runtime_registry",
]
