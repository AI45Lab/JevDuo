"""Canonical standalone device command contracts shared by controller and worker."""

from __future__ import annotations

from northcua_protocol.capabilities import Capability, ToolRiskLevel

from northcua_runtime_shared.tool_contracts import (
    DeviceCommandBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
    ToolVisibility,
)

DEVICE_ECHO_CONTRACT = ToolContract(
    tool_id="device.echo",
    description="Echo command parameters for worker connectivity checks.",
    namespace="device.worker",
    kind=ToolContractKind.PROBE,
    audience=ToolAudience.RUNTIME,
    placement=ToolPlacement.DEVICE_COMMAND,
    visibility=ToolVisibility.RUNTIME_ONLY,
    capability=Capability.DESKTOP_SCREENSHOT,
    json_schema={"type": "object"},
    side_effect=False,
    readonly=True,
    risk_level=ToolRiskLevel.LOW,
    binding=DeviceCommandBinding(server_name="EchoRuntimeAdapter"),
    source="northcua_runtime_shared.device_shell_contracts",
)

SHELL_EXEC_CONTRACT = ToolContract(
    tool_id="shell.exec",
    description="Execute an explicitly allowlisted shell command on the standalone device worker.",
    namespace="device.worker.shell",
    kind=ToolContractKind.ACTION,
    audience=ToolAudience.RUNTIME,
    placement=ToolPlacement.DEVICE_COMMAND,
    visibility=ToolVisibility.RUNTIME_ONLY,
    capability=Capability.SHELL_EXEC,
    json_schema={"type": "object", "properties": {"cmd": {"type": "string"}}},
    timeout_seconds=30.0,
    side_effect=True,
    readonly=False,
    risk_level=ToolRiskLevel.HIGH,
    binding=DeviceCommandBinding(server_name="DeviceShellExecutor"),
    source="northcua_runtime_shared.device_shell_contracts",
)

SHELL_EXEC_READONLY_CONTRACT = ToolContract(
    tool_id="shell.exec_readonly",
    description="Run a tightly-scoped allowlisted read-only shell probe on the standalone device worker.",
    namespace="device.worker.shell",
    kind=ToolContractKind.PROBE,
    audience=ToolAudience.RUNTIME,
    placement=ToolPlacement.DEVICE_COMMAND,
    visibility=ToolVisibility.RUNTIME_ONLY,
    capability=Capability.SHELL_EXEC,
    json_schema={
        "type": "object",
        "properties": {"cmd": {"type": "string", "minLength": 1}},
        "required": ["cmd"],
        "additionalProperties": False,
    },
    timeout_seconds=30.0,
    side_effect=False,
    readonly=True,
    risk_level=ToolRiskLevel.MEDIUM,
    binding=DeviceCommandBinding(server_name="DeviceShellExecutor"),
    source="northcua_runtime_shared.device_shell_contracts",
)

__all__ = ["DEVICE_ECHO_CONTRACT", "SHELL_EXEC_CONTRACT", "SHELL_EXEC_READONLY_CONTRACT"]
