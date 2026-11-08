"""Runtime-only metadata attached to provider Tool objects."""

from __future__ import annotations

from northcua_runtime_shared.tool_contracts.fingerprints import fingerprint_json

_SERVER_NAME_ATTR = "_northcua_route_server_name"
_CONTRACT_ID_ATTR = "_northcua_contract_id"
_CONTRACT_TOOL_ID_ATTR = "_northcua_contract_tool_id"
_CONTRACT_SOURCE_ATTR = "_northcua_contract_source"
_CONTRACT_PUBLIC_NAME_ATTR = "_northcua_contract_public_name"
_CONTRACT_DESCRIPTION_ATTR = "_northcua_contract_description"
_CONTRACT_PLACEMENT_ATTR = "_northcua_contract_placement"
_CONTRACT_PROVIDER_SCHEMA_HASH_ATTR = "_northcua_contract_provider_schema_hash"


def attach_tool_route_metadata(tool: object, *, server_name: str) -> object:
    """Attach MCP route metadata without changing tool execution parameters."""

    setattr(tool, _SERVER_NAME_ATTR, str(server_name or ""))
    return tool


def attach_tool_contract_projection_metadata(tool: object, *, contract: object) -> object:
    """Attach contract projection metadata to a provider Tool object."""

    setattr(tool, _CONTRACT_ID_ATTR, str(getattr(contract, "contract_id", "") or ""))
    setattr(tool, _CONTRACT_TOOL_ID_ATTR, str(getattr(contract, "tool_id", "") or ""))
    setattr(tool, _CONTRACT_SOURCE_ATTR, str(getattr(contract, "source", "") or ""))
    setattr(tool, _CONTRACT_PUBLIC_NAME_ATTR, str(getattr(contract, "provider_name", "") or ""))
    setattr(tool, _CONTRACT_DESCRIPTION_ATTR, str(getattr(contract, "description", "") or ""))
    placement = getattr(contract, "placement", "")
    setattr(tool, _CONTRACT_PLACEMENT_ATTR, str(getattr(placement, "value", placement)))
    setattr(
        tool,
        _CONTRACT_PROVIDER_SCHEMA_HASH_ATTR,
        fingerprint_json(getattr(contract, "provider_parameters_schema", {}) or {}),
    )
    return tool


def tool_route_server_name(tool: object) -> str:
    """Return explicit NorthCUA route metadata from a provider Tool object."""

    return str(getattr(tool, _SERVER_NAME_ATTR, "") or "")


def tool_contract_projection_id(tool: object) -> str:
    """Return the contract id that produced a provider Tool object."""

    return str(getattr(tool, _CONTRACT_ID_ATTR, "") or "")


def tool_contract_projection_tool_id(tool: object) -> str:
    """Return the canonical tool id that produced a provider Tool object."""

    return str(getattr(tool, _CONTRACT_TOOL_ID_ATTR, "") or "")


def tool_contract_projection_placement(tool: object) -> str:
    """Return the owning contract's explicit execution placement."""

    return str(getattr(tool, _CONTRACT_PLACEMENT_ATTR, "") or "")


def tool_is_contract_projection(tool: object) -> bool:
    """Return whether a provider Tool object came from a ToolContract projection."""

    return bool(tool_contract_projection_id(tool))


def tool_contract_projection_is_current(tool: object) -> bool:
    """Return whether a provider Tool still matches its contract projection metadata."""

    if not tool_is_contract_projection(tool):
        return False
    expected_name = str(getattr(tool, _CONTRACT_PUBLIC_NAME_ATTR, "") or "")
    expected_description = str(getattr(tool, _CONTRACT_DESCRIPTION_ATTR, "") or "")
    expected_schema_hash = str(getattr(tool, _CONTRACT_PROVIDER_SCHEMA_HASH_ATTR, "") or "")
    return (
        str(getattr(tool, "name", "") or "") == expected_name
        and str(getattr(tool, "description", "") or "") == expected_description
        and fingerprint_json(getattr(tool, "input_schema", {}) or {}) == expected_schema_hash
    )


__all__ = [
    "attach_tool_contract_projection_metadata",
    "attach_tool_route_metadata",
    "tool_contract_projection_id",
    "tool_contract_projection_is_current",
    "tool_contract_projection_tool_id",
    "tool_contract_projection_placement",
    "tool_is_contract_projection",
    "tool_route_server_name",
]
