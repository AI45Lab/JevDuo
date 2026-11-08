"""Registry for local/registerable FastMCP servers."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

from fastmcp import FastMCP

from northcua.core.errors import MCPRegistryError
from northcua_protocol.capabilities import ToolDescriptor
from northcua_runtime_shared.tool_contracts import ToolContract


@dataclass(slots=True)
class MCPFactoryContext:
    """Context passed to registered MCP server factories.

    Attributes:
        process_name: Human-readable process/session label for diagnostics.
        extra: Small string metadata used by specialized factory adapters.
    """

    process_name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


MCPFactory = Callable[[MCPFactoryContext], FastMCP]


@dataclass(slots=True)
class RegisteredMCPServer:
    """Registered local MCP server metadata.

    Attributes:
        name: Registry key used by NexAU and NorthCUA configs.
        factory: Callable that constructs the FastMCP server instance.
        wire_tool_descriptors: Wire projection generated from the tool contracts.
    """

    name: str
    factory: MCPFactory
    wire_tool_descriptors: tuple[ToolDescriptor, ...] = ()
    tool_contracts: tuple[ToolContract, ...] = ()


class NorthCUAMCPRegistry:
    """In-process registry of registerable MCP server factories."""

    _registry: dict[str, RegisteredMCPServer] = {}

    @classmethod
    def register(
        cls,
        name: str,
        factory: MCPFactory,
        *,
        tool_contracts: Iterable[ToolContract] = (),
    ) -> None:
        """Register or replace a local server factory.

        Args:
            name: Registry name for the MCP server.
            factory: Factory that creates the FastMCP server.
            tool_contracts: Source-of-truth contracts for the server.
        """

        contracts = tuple(tool_contracts)
        if not contracts:
            raise MCPRegistryError(
                f"MCP server {name!r} missing ToolContract metadata; owned servers must register tool_contracts"
            )
        descriptors = tuple(contract.to_descriptor() for contract in contracts)
        cls._registry[name] = RegisteredMCPServer(
            name=name,
            factory=factory,
            wire_tool_descriptors=descriptors,
            tool_contracts=contracts,
        )

    @classmethod
    def register_decorator(
        cls,
        name: str,
        *,
        tool_contracts: Iterable[ToolContract] = (),
    ) -> Callable[[MCPFactory], MCPFactory]:
        """Return a decorator that registers a local server factory.

        Args:
            name: Registry name for the MCP server.
            tool_contracts: Source-of-truth contracts for the server.

        Returns:
            Decorator that registers and returns the original factory.
        """

        def decorator(factory: MCPFactory) -> MCPFactory:
            """Register the decorated MCP factory.

            Args:
                factory: MCP server factory to register.

            Returns:
                The original factory, unchanged.
            """

            cls.register(
                name,
                factory,
                tool_contracts=tool_contracts,
            )
            return factory

        return decorator

    @classmethod
    def build(cls, name: str, context: MCPFactoryContext | None = None) -> FastMCP:
        """Instantiate a registered server.

        Args:
            name: Registered MCP server name.
            context: Optional factory context.

        Returns:
            FastMCP server instance.

        Raises:
            MCPRegistryError: If no factory is registered for the name.
        """

        registered = cls._registry.get(name)
        if registered is None:
            raise MCPRegistryError(f"Unknown local MCP server: {name}")
        return registered.factory(context or MCPFactoryContext())

    @classmethod
    def is_registered(cls, name: str) -> bool:
        """Check whether a factory exists.

        Args:
            name: MCP server registry name.

        Returns:
            True when the name is registered.
        """

        return name in cls._registry

    @classmethod
    def names(cls) -> list[str]:
        """List registered names.

        Returns:
            Sorted registered MCP server names.
        """

        return sorted(cls._registry)

    @classmethod
    def wire_tool_descriptors(cls, name: str) -> list[ToolDescriptor]:
        """Return protocol wire descriptor projections for one registered server.

        Args:
            name: Registered MCP server name.

        Returns:
            Deep-copied protocol descriptors in server declaration order.

        Raises:
            MCPRegistryError: If no server is registered for the name.
        """

        registered = cls._registry.get(name)
        if registered is None:
            raise MCPRegistryError(f"Unknown local MCP server: {name}")
        return [descriptor.model_copy(deep=True) for descriptor in registered.wire_tool_descriptors]

    @classmethod
    def tool_contracts(cls, name: str) -> list[ToolContract]:
        """Return tool contracts for one registered server when available."""

        registered = cls._registry.get(name)
        if registered is None:
            raise MCPRegistryError(f"Unknown local MCP server: {name}")
        return list(registered.tool_contracts)

    @classmethod
    def all_wire_tool_descriptors(cls, names: Iterable[str] | None = None) -> list[ToolDescriptor]:
        """Return protocol wire descriptor projections across registered MCP servers.

        Args:
            names: Optional server names to include. When omitted, all
                registered servers are scanned in sorted registry order.

        Returns:
            Deep-copied descriptors in deterministic server/tool order.
        """

        selected_names = list(names) if names is not None else cls.names()
        descriptors: list[ToolDescriptor] = []
        for name in selected_names:
            descriptors.extend(cls.wire_tool_descriptors(name))
        return descriptors

    @classmethod
    def server_name_for_tool(cls, tool_id: str) -> str | None:
        """Return the registered MCP server that owns one protocol tool id.

        Args:
            tool_id: Protocol-facing tool id.

        Returns:
            Matching MCP server name, or ``None`` when no descriptor matches.
        """

        for name in cls.names():
            for descriptor in cls._registry[name].wire_tool_descriptors:
                if descriptor.tool_id == tool_id:
                    return name
        return None
