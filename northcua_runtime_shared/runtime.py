"""Shared runtime adapter protocol for standalone data-plane packages."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from northcua_protocol.capabilities import Capability, ToolDescriptor
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_runtime_shared.tool_contracts.contracts import ToolContract


class RuntimeHealth(BaseModel):
    """Health snapshot for one runtime adapter.

    Attributes:
        healthy: Overall adapter readiness flag.
        message: Optional human-readable diagnostic detail.
        capability_health: Capability-level health projection.
    """

    healthy: bool = True
    message: str = ""
    capability_health: dict[Capability, bool] = Field(default_factory=dict)


class RuntimeAdapter(Protocol):
    """Pluggable executor for a family of device tools."""

    @property
    def capabilities(self) -> set[Capability]:
        """Capabilities exposed by this adapter.

        Returns:
            Coarse capabilities implemented by the adapter.
        """
        ...

    @property
    def tool_contracts(self) -> list[ToolContract]:
        """Tool contracts exposed by this adapter.

        Returns:
            Canonical contracts used for registration, validation, and routing.
        """
        ...

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Wire descriptor projection exposed by this adapter.

        Returns:
            Protocol tool descriptors advertised to the controller.
        """
        ...

    async def healthcheck(self) -> RuntimeHealth:
        """Return runtime health.

        Returns:
            Adapter readiness and capability-level health.
        """
        ...

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute one protocol command.

        Args:
            command: Controller-issued command request.

        Returns:
            Command result preserving command authority fields.
        """
        ...
