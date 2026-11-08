"""Compile runtime direct tools from canonical ToolContracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.tool import Tool

from northcua.agentkernel.tools import contract_to_nexau_tool
from northcua.core.errors import MCPRegistryError
from northcua.core.json_types import JsonObject
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.contracts import external_mcp_snapshot_contracts
from northcua.runtime.mcp.registry import NorthCUAMCPRegistry
from northcua.runtime.mcp.server_config import visible_runtime_mcp_server_configs
from northcua_runtime_shared.tool_contracts import (
    ToolContract,
    ToolProfile,
    ToolSurfaceMode,
    ToolSurfaceResolver,
    ToolVisibility,
    fingerprint_json,
)

RuntimeToolImplementation = Callable[..., Awaitable[JsonObject]]
RuntimeToolImplementationFactory = Callable[[str, str], RuntimeToolImplementation]


@dataclass(frozen=True, slots=True)
class CompiledRuntimeDirectToolSurface:
    """Direct tools compiled for one runtime mode."""

    tools: list[Tool]
    externalized_server_names: list[str]


@dataclass(frozen=True, slots=True)
class DerivedRemoteToolSurface:
    """Remote external tools derived from contract-backed server configs."""

    tools: list[Tool]
    externalized_server_names: list[str]


async def derive_remote_tool_surface(
    agent_config: AgentConfig,
    *,
    raw_server_configs: list[JsonObject] | None = None,
    implementation_factory: RuntimeToolImplementationFactory | None = None,
) -> DerivedRemoteToolSurface:
    """Derive remote external tools from registered contracts or imported snapshots."""

    ensure_builtin_mcp_servers_registered()
    visible_server_configs = [
        dict(server)
        for server in visible_runtime_mcp_server_configs(
            list(raw_server_configs) if raw_server_configs is not None else list(agent_config.mcp_servers)
        )
    ]
    surface = compile_runtime_direct_tool_surface(
        visible_server_configs,
        mode=ToolSurfaceMode.REMOTE_EXTERNALIZED,
        implementation_factory=implementation_factory,
    )
    return DerivedRemoteToolSurface(
        tools=surface.tools,
        externalized_server_names=surface.externalized_server_names,
    )


def compile_runtime_direct_tool_surface(
    raw_server_configs: Sequence[Mapping[str, object]],
    *,
    mode: ToolSurfaceMode,
    implementation_factory: RuntimeToolImplementationFactory | None = None,
) -> CompiledRuntimeDirectToolSurface:
    """Compile direct NexAU tools from registered contracts or external snapshots."""

    direct_tools: list[Tool] = []
    externalized_server_names: list[str] = []
    public_name_fingerprints: dict[str, str] = {}
    public_name_sources: dict[str, str] = {}
    public_name_imported: dict[str, bool] = {}
    for server_config in raw_server_configs:
        server_name = str(server_config.get("name") or "")
        if not server_name:
            continue
        contracts = _contracts_for_server_config(server_config)
        if contracts is None:
            raise MCPRegistryError(
                f"Model-visible external MCP server {server_name!r} requires imported snapshot tool metadata "
                "before exposure."
            )
        surface = ToolSurfaceResolver(contracts).resolve(
            ToolProfile(
                name=f"mcp.{server_name}.{mode.value}",
                visibility=frozenset({ToolVisibility.MODEL_VISIBLE}),
                include_imported=any(contract.imported for contract in contracts),
            ),
            mode=mode,
        )
        contracts_by_id = {contract.contract_id: contract for contract in surface.contracts}
        for projection in surface.provider_tools:
            contract = contracts_by_id[projection.contract_id]
            fingerprint = fingerprint_json(contract.provider_parameters_schema)
            existing_fingerprint = public_name_fingerprints.get(projection.name)
            if existing_fingerprint is not None:
                previous_source = public_name_sources.get(projection.name, "<unknown>")
                current_source = contract.server_name or server_name
                if existing_fingerprint != fingerprint or public_name_imported[projection.name] != contract.imported:
                    raise MCPRegistryError(
                        f"Runtime direct tool name {projection.name!r} collides with conflicting schema fingerprints: "
                        f"{previous_source} vs {current_source}"
                    )
                raise MCPRegistryError(
                    f"Runtime direct tool name {projection.name!r} collides across multiple routed sources: "
                    f"{previous_source} vs {current_source}"
                )
            public_name_fingerprints[projection.name] = fingerprint
            public_name_sources[projection.name] = contract.server_name or server_name
            public_name_imported[projection.name] = contract.imported
            direct_tools.append(
                contract_to_nexau_tool(
                    contract,
                    implementation=(
                        implementation_factory(contract.server_name or server_name, projection.name)
                        if implementation_factory is not None
                        else None
                    ),
                    source_name=contract.server_name or server_name,
                )
            )
        externalized_server_names.append(server_name)
    return CompiledRuntimeDirectToolSurface(
        tools=direct_tools,
        externalized_server_names=externalized_server_names,
    )


def _contracts_for_server_config(server_config: Mapping[str, object]) -> tuple[ToolContract, ...] | None:
    server_name = str(server_config.get("name") or "")
    if NorthCUAMCPRegistry.is_registered(server_name):
        contracts = tuple(NorthCUAMCPRegistry.tool_contracts(server_name))
        if not contracts:
            raise MCPRegistryError(
                f"Model-visible registered MCP server {server_name!r} is missing ToolContract metadata"
            )
        return contracts
    snapshots = _external_snapshot_contracts_from_config(server_config)
    return snapshots or None


def _external_snapshot_contracts_from_config(server_config: Mapping[str, object]) -> tuple[ToolContract, ...]:
    server_name = str(server_config.get("name") or "")
    raw_items = server_config.get("external_mcp_snapshots")
    if not isinstance(raw_items, list):
        return ()
    items: list[Mapping[str, object]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item.setdefault("server_name", server_name)
        items.append(item)
    if items:
        return external_mcp_snapshot_contracts(
            items,
            default_server_name=server_name,
            default_source=str(server_config.get("url") or server_config.get("command") or server_name),
        )
    return ()


__all__ = [
    "CompiledRuntimeDirectToolSurface",
    "DerivedRemoteToolSurface",
    "RuntimeToolImplementation",
    "RuntimeToolImplementationFactory",
    "compile_runtime_direct_tool_surface",
    "derive_remote_tool_surface",
]
