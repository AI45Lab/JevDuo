"""Helpers for NorthCUA-specific MCP server metadata layered on NexAU configs.

Raw MCP server dictionaries may include NorthCUA-only keys such as
``model_visible`` and ``external_mcp_snapshots``. Helpers in this module
preserve those keys for internal runtime routing and strip them before handing
configs to NexAU surfaces that expect the original MCP config schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from northcua.core.errors import MCPRegistryError
from northcua.runtime.mcp.registry import NorthCUAMCPRegistry
from northcua.runtime.mcp.types import MCPServerConfigPayload

_EXTERNAL_SNAPSHOT_KEY = "external_mcp_snapshots"
_NORTHCUA_MCP_METADATA_KEYS = {"model_visible", _EXTERNAL_SNAPSHOT_KEY}
_DEFAULT_MODEL_INVISIBLE_SERVER_NAMES = {
    "DesktopNativeDataExecutor",
    "DesktopNativeA11yExecutor",
    "WebAgentBackendExecutor",
    "WebAgentProbeExecutor",
}
_FORCED_MODEL_INVISIBLE_SERVER_NAMES = frozenset(_DEFAULT_MODEL_INVISIBLE_SERVER_NAMES)
_IN_MEMORY_MCP_TYPE = "in-memory"


@dataclass(frozen=True, slots=True)
class NorthCUAMCPServerConfig:
    """Normalized MCP server config with NorthCUA presentation metadata.

    Attributes:
        raw: Original JSON-style MCP server config. Runtime-only metadata is
            retained here so in-memory and invisible server routing can inspect
            it later.
        model_visible: Whether this server should be exposed to model-visible
            MCP discovery/tool surfaces.
    """

    raw: MCPServerConfigPayload
    model_visible: bool = True

    @property
    def name(self) -> str:
        """Return the configured server name.

        Returns:
            MCP server name, or an empty string when missing.
        """

        return str(self.raw.get("name") or "")

    @property
    def type(self) -> str:
        """Return the configured transport type.

        Returns:
            MCP transport type, or an empty string when missing.
        """

        return str(self.raw.get("type") or "")

    @property
    def is_in_memory(self) -> bool:
        """Return whether the server should be bridged in-process.

        Returns:
            True when the MCP server type is in-memory.
        """

        return self.type == _IN_MEMORY_MCP_TYPE

    def to_nexau_config(self) -> MCPServerConfigPayload:
        """Return a config dict stripped of NorthCUA-only metadata keys.

        Returns:
            NexAU-compatible MCP server config dictionary. The returned wire
            shape is identical to ``raw`` except keys such as ``model_visible``
            are omitted.
        """

        return {key: value for key, value in self.raw.items() if key not in _NORTHCUA_MCP_METADATA_KEYS}

    def to_runtime_config(self) -> MCPServerConfigPayload:
        """Return a runtime-visible config retaining routing metadata.

        Returns:
            MCP config dictionary with model-visibility metadata stripped, but
            with imported snapshot metadata retained for the ToolContract
            direct-tool compiler.
        """

        return {key: value for key, value in self.raw.items() if key != "model_visible"}


def parse_mcp_server_config(raw: Mapping[str, object]) -> NorthCUAMCPServerConfig:
    """Parse one raw MCP config dictionary.

    Args:
        raw: Raw MCP server config, possibly containing NorthCUA metadata.

    Returns:
        Normalized MCP server config wrapper. If ``model_visible`` is omitted,
        desktop-native data and accessibility servers default to invisible
        because they are runtime observation helpers rather than model action
        surfaces.
    """

    name = str(raw.get("name") or "")
    if name in _FORCED_MODEL_INVISIBLE_SERVER_NAMES:
        model_visible = False
    elif "model_visible" in raw:
        model_visible = bool(raw.get("model_visible"))
    else:
        model_visible = name not in _DEFAULT_MODEL_INVISIBLE_SERVER_NAMES
    return NorthCUAMCPServerConfig(
        raw=dict(raw),
        model_visible=model_visible,
    )


def visible_runtime_mcp_server_configs(raw_configs: Sequence[Mapping[str, object]]) -> list[MCPServerConfigPayload]:
    """Return only model-visible server configs for NorthCUA runtime routing.

    Args:
        raw_configs: Raw MCP server configs.

    Returns:
        Model-visible configs with ``model_visible`` stripped and runtime
        routing metadata such as imported snapshots retained.
    """

    return [
        config.to_runtime_config()
        for config in (parse_mcp_server_config(raw) for raw in raw_configs)
        if _model_visible_config_is_allowed(config)
    ]


def visible_nexau_mcp_server_configs(raw_configs: Sequence[Mapping[str, object]]) -> list[MCPServerConfigPayload]:
    """Return model-visible MCP server configs NexAU can consume directly.

    Args:
        raw_configs: Raw MCP server configs.

    Returns:
        Always empty. NorthCUA-owned and imported external tool schemas must
        be projected from ``ToolContract``; native NexAU MCP discovery is not a
        production model-visible source of truth.
    """

    for config in (parse_mcp_server_config(raw) for raw in raw_configs):
        _model_visible_config_is_allowed(config)
    return []


def visible_in_memory_mcp_server_configs(raw_configs: Sequence[Mapping[str, object]]) -> list[MCPServerConfigPayload]:
    """Return model-visible in-memory MCP server configs.

    Args:
        raw_configs: Raw MCP server configs.

    Returns:
        Model-visible in-memory server configs with NorthCUA metadata intact so
        the local bridge can decide how to expose direct tools.
    """

    return [
        dict(config.raw)
        for config in (parse_mcp_server_config(raw) for raw in raw_configs)
        if _model_visible_config_is_allowed(config) and config.is_in_memory
    ]


def visible_contract_direct_mcp_server_configs(
    raw_configs: Sequence[Mapping[str, object]],
) -> list[MCPServerConfigPayload]:
    """Return model-visible registered servers that must use contract direct tools.

    Args:
        raw_configs: Raw MCP server configs.

    Returns:
        Registered contract-backed servers with model-visible contracts. These
        servers are intentionally not handed to NexAU native MCP discovery, so
        local/remote prompt schemas come from the same ``ToolContract``
        provider projection.
    """

    return [
        dict(config.raw)
        for config in (parse_mcp_server_config(raw) for raw in raw_configs)
        if _model_visible_config_is_allowed(config) and _is_contract_backed_server(config.name)
    ]


def visible_external_snapshot_mcp_server_configs(
    raw_configs: Sequence[Mapping[str, object]],
) -> list[MCPServerConfigPayload]:
    """Return model-visible unmanaged MCP configs that carry imported tool snapshots."""

    return [
        dict(config.raw)
        for config in (parse_mcp_server_config(raw) for raw in raw_configs)
        if _model_visible_config_is_allowed(config)
        and not _is_contract_backed_server(config.name)
        and _has_external_mcp_snapshots(config.raw)
    ]


def invisible_mcp_server_names(raw_configs: Sequence[Mapping[str, object]]) -> list[str]:
    """Return the names of runtime-only model-invisible MCP servers.

    Args:
        raw_configs: Raw MCP server configs.

    Returns:
        Names of configs hidden from model-visible MCP surfaces.
    """

    return [
        config.name
        for config in (parse_mcp_server_config(raw) for raw in raw_configs)
        if not config.model_visible and config.name
    ]


def _is_contract_backed_server(server_name: str) -> bool:
    return bool(
        server_name
        and NorthCUAMCPRegistry.is_registered(server_name)
        and NorthCUAMCPRegistry.tool_contracts(server_name)
    )


def _has_external_mcp_snapshots(raw: Mapping[str, object]) -> bool:
    value = raw.get(_EXTERNAL_SNAPSHOT_KEY)
    return isinstance(value, list) and bool(value)


def _model_visible_config_is_allowed(config: NorthCUAMCPServerConfig) -> bool:
    if not config.model_visible:
        return False
    if (
        config.name
        and NorthCUAMCPRegistry.is_registered(config.name)
        and not NorthCUAMCPRegistry.tool_contracts(config.name)
    ):
        raise MCPRegistryError(f"Model-visible registered MCP server {config.name!r} is missing ToolContract metadata")
    if (
        config.name
        and not NorthCUAMCPRegistry.is_registered(config.name)
        and not _has_external_mcp_snapshots(config.raw)
    ):
        raise MCPRegistryError(
            f"Model-visible external MCP server {config.name!r} requires imported snapshot tool metadata."
        )
    return True
