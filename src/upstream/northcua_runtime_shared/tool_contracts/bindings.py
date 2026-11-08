"""Execution binding metadata for typed NorthCUA tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ToolBindingKind(str, Enum):
    """Supported execution binding families for a tool contract."""

    MCP = "mcp"
    DIRECT_HANDLER = "direct_handler"
    REMOTE_COMMAND = "remote_command"
    DEVICE_COMMAND = "device_command"
    REMOTE_PROXY = "remote_proxy"
    EVALUATOR_ARTIFACT = "evaluator_artifact"
    EVALUATOR_CLIENT = "evaluator_client"
    SANDBOX_PROVIDER = "sandbox_provider"
    EXTERNAL_MCP_SNAPSHOT = "external_mcp_snapshot"


@dataclass(frozen=True, slots=True)
class McpBinding:
    """Binding for tools executed by an MCP server."""

    server_name: str
    registration_hook: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.MCP


@dataclass(frozen=True, slots=True)
class DirectHandlerBinding:
    """Binding for in-process AgentKit tools."""

    handler_path: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.DIRECT_HANDLER


@dataclass(frozen=True, slots=True)
class RemoteCommandBinding:
    """Binding for direct tools proxied through the remote command plane."""

    server_name: str
    registration_hook: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.REMOTE_COMMAND


@dataclass(frozen=True, slots=True)
class DeviceCommandBinding:
    """Binding for tools dispatched through the device command plane."""

    server_name: str
    registration_hook: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.DEVICE_COMMAND


@dataclass(frozen=True, slots=True)
class RemoteProxyBinding:
    """Binding for tools executed through a remote client proxy."""

    server_name: str
    registration_hook: str = ""
    proxy_name: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.REMOTE_PROXY


@dataclass(frozen=True, slots=True)
class EvaluatorArtifactBinding:
    """Binding for evaluator artifact inspection tools."""

    handler_path: str = ""
    artifact_kind: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.EVALUATOR_ARTIFACT


@dataclass(frozen=True, slots=True)
class EvaluatorClientBinding:
    """Binding for evaluator tools dispatched against a client/runtime."""

    dispatch_name: str
    server_name: str = ""
    placement: str = ""
    target_tool_id: str = ""
    target_schema_fingerprint: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.EVALUATOR_CLIENT


@dataclass(frozen=True, slots=True)
class SandboxProviderBinding:
    """Binding for sandbox/provider control-plane operations."""

    provider_method: str
    phase: str = ""
    capability: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.SANDBOX_PROVIDER


@dataclass(frozen=True, slots=True)
class ExternalMcpSnapshotBinding:
    """Binding for an imported non-owned MCP descriptor snapshot."""

    server_name: str
    source: str = ""
    schema_fingerprint: str = ""

    @property
    def kind(self) -> ToolBindingKind:
        """Return the binding kind."""

        return ToolBindingKind.EXTERNAL_MCP_SNAPSHOT


ToolBinding = (
    McpBinding
    | DirectHandlerBinding
    | RemoteCommandBinding
    | DeviceCommandBinding
    | RemoteProxyBinding
    | EvaluatorArtifactBinding
    | EvaluatorClientBinding
    | SandboxProviderBinding
    | ExternalMcpSnapshotBinding
)


def binding_summary(binding: ToolBinding) -> dict[str, str]:
    """Return stable JSON-compatible binding metadata."""

    summary: dict[str, str] = {"kind": binding.kind.value}
    for attr in (
        "server_name",
        "registration_hook",
        "handler_path",
        "dispatch_name",
        "placement",
        "target_tool_id",
        "target_schema_fingerprint",
        "provider_method",
        "phase",
        "capability",
        "proxy_name",
        "artifact_kind",
        "source",
        "schema_fingerprint",
    ):
        value = getattr(binding, attr, None)
        if value:
            summary[attr] = str(value)
    return summary


__all__ = [
    "DirectHandlerBinding",
    "DeviceCommandBinding",
    "EvaluatorArtifactBinding",
    "EvaluatorClientBinding",
    "ExternalMcpSnapshotBinding",
    "McpBinding",
    "RemoteCommandBinding",
    "RemoteProxyBinding",
    "SandboxProviderBinding",
    "ToolBinding",
    "ToolBindingKind",
    "binding_summary",
]
