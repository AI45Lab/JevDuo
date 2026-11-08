"""Helpers for grouping tool contracts into runtime profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from northcua_runtime_shared.tool_contracts.contracts import ToolAudience, ToolContract, ToolVisibility


class ToolSurfaceMode(str, Enum):
    """Transport/runtime mode used when resolving a profile."""

    LOCAL = "local"
    LOCAL_IN_MEMORY = "local_in_memory"
    REMOTE = "remote"
    REMOTE_EXTERNALIZED = "remote_externalized"


@dataclass(frozen=True, slots=True)
class ToolProfile:
    """Named visibility/capability slice over canonical contracts.

    A profile decides which contracts are included in a context. It never
    rewrites parameter schema, route metadata, or provider descriptions.
    """

    name: str
    contract_ids: frozenset[str] = frozenset()
    namespaces: frozenset[str] = frozenset()
    audiences: frozenset[ToolAudience] = frozenset()
    visibility: frozenset[ToolVisibility] = frozenset()
    include_imported: bool = False

    def includes(self, contract: ToolContract) -> bool:
        """Return whether this profile includes a contract."""

        if contract.imported and not self.include_imported:
            return False
        if self.contract_ids and contract.tool_id not in self.contract_ids:
            return False
        if self.namespaces and contract.namespace not in self.namespaces:
            return False
        if self.audiences and contract.audience not in self.audiences:
            return False
        if self.visibility and contract.visibility not in self.visibility:
            return False
        return True

    def select(self, contracts: tuple[ToolContract, ...]) -> tuple[ToolContract, ...]:
        """Return contracts selected by this profile in declaration order."""

        return tuple(contract for contract in contracts if self.includes(contract))


@dataclass(frozen=True, slots=True)
class ToolContractProfile:
    """Named collection of tool contracts with routing metadata."""

    name: str
    contracts: tuple[ToolContract, ...]

    @property
    def model_visible_contracts(self) -> tuple[ToolContract, ...]:
        """Return contracts that should be advertised to models."""

        return tuple(contract for contract in self.contracts if contract.model_visible)

    @property
    def server_by_tool_id(self) -> dict[str, str]:
        """Return MCP server routing by tool id."""

        return {contract.tool_id: contract.server_name for contract in self.contracts if contract.server_name}


ToolSurfaceProfile = ToolProfile


__all__ = ["ToolContractProfile", "ToolProfile", "ToolSurfaceMode", "ToolSurfaceProfile"]
