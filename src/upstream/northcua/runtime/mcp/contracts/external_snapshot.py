"""Imported snapshot contracts for non-owned MCP tools."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from northcua_protocol.capabilities import Capability, JsonObject, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import (
    ExternalMcpSnapshotBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
)
from northcua_runtime_shared.tool_contracts.fingerprints import fingerprint_json


def external_mcp_snapshot_contract(
    *,
    server_name: str,
    tool_name: str,
    description: str = "",
    input_schema: Mapping[str, object] | None = None,
    source: str = "",
    schema_fingerprint: str = "",
) -> ToolContract:
    """Build an imported contract snapshot for one non-owned MCP tool."""

    if input_schema is None:
        raise ValueError(f"External MCP snapshot {server_name}.{tool_name} requires input_schema")
    if not schema_fingerprint:
        raise ValueError(f"External MCP snapshot {server_name}.{tool_name} requires schema_fingerprint")
    if not source.strip():
        raise ValueError(f"External MCP snapshot {server_name}.{tool_name} requires source")
    schema: JsonObject = dict(input_schema)
    computed_fingerprint = fingerprint_json(schema)
    if schema_fingerprint != computed_fingerprint:
        raise ValueError(
            f"External MCP snapshot schema fingerprint mismatch for {server_name}.{tool_name}: "
            f"expected {schema_fingerprint}, computed {computed_fingerprint}"
        )
    safe_source = _sanitize_source(source) or server_name
    return ToolContract(
        tool_id=tool_name,
        public_name=tool_name,
        description=description or f"Imported MCP tool {tool_name} from {server_name}.",
        namespace=f"external.mcp.{server_name}",
        kind=ToolContractKind.EXTERNAL,
        audience=ToolAudience.EXTERNAL,
        placement=ToolPlacement.EXTERNAL_MCP,
        capability=Capability.DESKTOP_INPUT,
        json_schema=schema,
        side_effect=True,
        readonly=False,
        risk_level=ToolRiskLevel.MEDIUM,
        binding=ExternalMcpSnapshotBinding(
            server_name=server_name,
            source=safe_source,
            schema_fingerprint=computed_fingerprint,
        ),
        imported=True,
        source=safe_source,
    )


def external_mcp_snapshot_contracts(
    tools: Iterable[Mapping[str, object]],
    *,
    default_server_name: str = "",
    default_source: str = "",
) -> tuple[ToolContract, ...]:
    """Build imported contracts from discovered MCP tool metadata mappings."""

    contracts: list[ToolContract] = []
    for item in tools:
        server_name = str(item.get("server_name") or default_server_name)
        tool_name = str(item.get("name") or item.get("tool_name") or "")
        if not server_name or not tool_name:
            raise ValueError("External MCP snapshot metadata requires server_name and tool name")
        contracts.append(
            external_mcp_snapshot_contract(
                server_name=server_name,
                tool_name=tool_name,
                description=str(item.get("description") or ""),
                input_schema=_input_schema_from_item(item),
                source=str(item.get("source") or default_source),
                schema_fingerprint=str(item.get("schema_fingerprint") or ""),
            )
        )
    return tuple(contracts)


def _input_schema_from_item(item: Mapping[str, object]) -> Mapping[str, object] | None:
    if "parameters" in item:
        raise ValueError("External MCP snapshot metadata must use input_schema, not parameters")
    input_schema = item.get("input_schema")
    if isinstance(input_schema, Mapping):
        return input_schema
    return None


def _sanitize_source(source: str) -> str:
    raw = source.strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    parts = raw.split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} <args redacted>"


__all__ = ["external_mcp_snapshot_contract", "external_mcp_snapshot_contracts"]
