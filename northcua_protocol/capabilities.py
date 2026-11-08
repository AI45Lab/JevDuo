"""Capability and tool descriptors used by controller scheduling and workers."""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import ConfigDict, Field

from northcua_protocol.base import ProtocolModel


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list[object] | dict[str, object]
JsonObject = dict[str, object]


class PlatformKind(str, Enum):
    """Supported device/platform families for scheduling and worker runtime selection."""

    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"
    BROWSER = "browser"
    ANDROID = "android"
    IOS = "ios"
    VM = "vm"


class Capability(str, Enum):
    """Coarse runtime capabilities exposed by a device worker."""

    DESKTOP_SCREENSHOT = "desktop.screenshot"
    DESKTOP_INPUT = "desktop.input"
    DESKTOP_BACKGROUND_INPUT = "desktop.background_input"
    DESKTOP_MONITOR = "desktop.monitor"
    DESKTOP_WINDOW = "desktop.window"
    A11Y_TREE = "a11y.tree"
    SHELL_EXEC = "shell.exec"
    BROWSER_PAGE = "browser.page"


class ToolRiskLevel(str, Enum):
    """Operational risk level for a device-executable tool."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolDescriptor(ProtocolModel):
    """Concrete device tool exposed through a capability.

    Attributes:
        tool_id: Stable protocol-facing tool identifier.
        capability: Capability required to execute the tool.
        parameters_schema: JSON-schema-like parameter schema serialized as
            ``schema`` on the wire for protocol readability.
        timeout_seconds: Default command timeout.
        side_effect: Whether executing the tool changes device state.
        requires_focus: Whether the tool requires foreground GUI focus.
        risk_level: Safety risk classification.
        max_payload_bytes: Maximum accepted parameter payload size.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    tool_id: str
    capability: Capability
    parameters_schema: JsonObject = Field(default_factory=dict, alias="schema")
    timeout_seconds: float = 10.0
    side_effect: bool = True
    requires_focus: bool = False
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW
    max_payload_bytes: int = 1_000_000


class DeviceDescriptor(ProtocolModel):
    """Device registration descriptor sent during ``DEVICE_HELLO``.

    Attributes:
        device_id: Stable worker id used for strict targeting and leases.
        display_name: Optional operator-facing name.
        platform: Device platform family.
        capabilities: Coarse capabilities available through the worker.
        tools: Concrete protocol tool descriptors advertised by the worker.
        max_sessions: Maximum active sessions allowed on the device.
        max_gui_sessions: Maximum GUI sessions allowed when foreground focus matters.
        gui_resource_id: Shared GUI resource group for devices that operate the
            same physical desktop or sandbox display.
        max_concurrent_commands: Maximum simultaneous command executions.
        supports_parallel_sessions: Whether the scheduler may colocate sessions.
        requires_foreground_focus: Whether GUI focus makes concurrent sessions unsafe.
        metadata: Small, untrusted device metadata for operator inspection.
    """

    device_id: str
    display_name: str = ""
    platform: PlatformKind
    capabilities: set[Capability] = Field(default_factory=set)
    tools: list[ToolDescriptor] = Field(default_factory=list)
    max_sessions: int = 1
    max_gui_sessions: int = 1
    gui_resource_id: str = ""
    max_concurrent_commands: int = 1
    supports_parallel_sessions: bool = False
    requires_foreground_focus: bool = True
    metadata: JsonObject = Field(default_factory=dict)

    def has_capabilities(self, required: set[Capability]) -> bool:
        """Return whether the device exposes all required capabilities.

        Args:
            required: Capabilities requested by a task or scheduler filter.

        Returns:
            ``True`` when every required capability is present.
        """

        return required.issubset(self.capabilities)
