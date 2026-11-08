"""Plugin-style registries for runtime adapters, tool surfaces, and artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.capabilities import Capability, PlatformKind, ToolDescriptor
from northcua_runtime_shared.tool_contracts.contracts import ToolContract

from northcua_runtime_shared.runtime import RuntimeAdapter


@dataclass(slots=True, frozen=True)
class RuntimeAdapterRegistration:
    """Static registration metadata for one runtime adapter plugin.

    Attributes:
        name: Unique adapter registration name.
        platforms: Platform families supported by the adapter.
        factory: Callable that builds an adapter for an optional context.
    """

    name: str
    platforms: frozenset[PlatformKind]
    factory: Callable[[object | None], RuntimeAdapter]


class RuntimeAdapterRegistry:
    """Registry for runtime-adapter plugins keyed by platform family."""

    def __init__(self) -> None:
        self._registrations: list[RuntimeAdapterRegistration] = []

    def register(
        self,
        name: str,
        factory: Callable[[object | None], RuntimeAdapter],
        *,
        platforms: Iterable[PlatformKind],
    ) -> None:
        """Register one adapter factory.

        Args:
            name: Unique adapter registration name.
            factory: Callable that creates a runtime adapter.
            platforms: Platform families supported by the factory.
        """

        registration = RuntimeAdapterRegistration(name=name, platforms=frozenset(platforms), factory=factory)
        self._registrations = [item for item in self._registrations if item.name != name]
        self._registrations.append(registration)

    def register_decorator(
        self,
        name: str,
        *,
        platforms: Iterable[PlatformKind],
    ) -> Callable[[Callable[[object | None], RuntimeAdapter]], Callable[[object | None], RuntimeAdapter]]:
        """Return a decorator that registers one runtime adapter factory.

        Args:
            name: Unique adapter registration name.
            platforms: Platform families supported by the factory.

        Returns:
            Decorator that registers and returns the original factory.
        """

        def decorator(factory: Callable[[object | None], RuntimeAdapter]) -> Callable[[object | None], RuntimeAdapter]:
            """Register and return the decorated adapter factory.

            Args:
                factory: Runtime adapter factory being decorated.

            Returns:
                Original factory, preserving decorator transparency.
            """

            self.register(name, factory, platforms=platforms)
            return factory

        return decorator

    def build_for_platform(self, platform: PlatformKind, *, context: object | None = None) -> list[RuntimeAdapter]:
        """Instantiate all runtime adapters registered for one platform.

        Args:
            platform: Platform family requested by the worker.
            context: Optional runtime-specific context object.

        Returns:
            Adapters whose registrations include ``platform``.
        """

        return [
            registration.factory(context) for registration in self._registrations if platform in registration.platforms
        ]


class ToolSurfaceRegistry:
    """Registry of concrete tool contracts exposed by one execution surface."""

    def __init__(self, contracts: Iterable[ToolContract] = ()) -> None:
        self._contracts: dict[str, ToolContract] = {}
        for contract in contracts:
            self.register(contract)

    def register(self, contract: ToolContract) -> None:
        """Register or replace one tool contract.

        Args:
            contract: Tool contract keyed by its stable tool id.
        """

        self._contracts[contract.tool_id] = contract

    def contract(self, tool_id: str) -> ToolContract | None:
        """Return one contract by tool id.

        Args:
            tool_id: Protocol-facing tool id.

        Returns:
            Matching contract, or ``None`` when the tool is unknown.
        """

        return self._contracts.get(tool_id)

    def contracts(self) -> list[ToolContract]:
        """Return all registered contracts in stable tool-id order."""

        return [self._contracts[tool_id] for tool_id in sorted(self._contracts)]

    def descriptor(self, tool_id: str) -> ToolDescriptor | None:
        """Return one wire descriptor projection by tool id.

        Args:
            tool_id: Protocol-facing tool id.

        Returns:
            Matching descriptor, or ``None`` when the tool is unknown.
        """

        contract = self.contract(tool_id)
        return contract.to_descriptor() if contract is not None else None

    def descriptors(self) -> list[ToolDescriptor]:
        """Return all registered descriptor projections in stable tool-id order.

        Returns:
            Registered descriptors sorted by tool id for stable CLI/tests.
        """

        return [contract.to_descriptor() for contract in self.contracts()]

    def capabilities(self) -> set[Capability]:
        """Return the coarse capabilities advertised by registered descriptors.

        Returns:
            Capability set derived from all registered tool descriptors.
        """

        return {contract.capability for contract in self._contracts.values()}


@dataclass(slots=True, frozen=True)
class CapabilityDescriptor:
    """Execution hints associated with one coarse capability.

    Attributes:
        capability: Capability these hints describe.
        requires_focus: Whether foreground focus is required.
        supports_parallel_sessions: Whether sessions may share a device.
        health_key: Optional runtime health key for operator views.
    """

    capability: Capability
    requires_focus: bool = False
    supports_parallel_sessions: bool = True
    health_key: str = ""


class CapabilityDescriptorRegistry:
    """Registry of capability execution hints."""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor] = ()) -> None:
        self._descriptors: dict[Capability, CapabilityDescriptor] = {
            descriptor.capability: descriptor for descriptor in descriptors
        }

    def register(self, descriptor: CapabilityDescriptor) -> None:
        """Register or replace one capability descriptor.

        Args:
            descriptor: Capability execution hints.
        """

        self._descriptors[descriptor.capability] = descriptor

    def descriptor(self, capability: Capability) -> CapabilityDescriptor | None:
        """Return one capability descriptor.

        Args:
            capability: Capability to query.

        Returns:
            Matching descriptor, or ``None`` when not registered.
        """

        return self._descriptors.get(capability)


class ArtifactHandler(Protocol):
    """Materialize or transform one artifact reference."""

    def __call__(self, artifact: ArtifactRef) -> ArtifactRef:
        """Return the handled artifact reference."""
        ...


@dataclass(slots=True)
class ArtifactHandlerRegistry:
    """Registry for artifact storage/materialization handlers."""

    _handlers: dict[str, ArtifactHandler] = field(default_factory=dict)

    def register(self, storage_kind: str, handler: ArtifactHandler) -> None:
        """Register one artifact handler by storage kind.

        Args:
            storage_kind: Storage kind value, such as ``inline`` or
                ``controller_blob``.
            handler: Callable that materializes or transforms an artifact ref.
        """

        self._handlers[storage_kind] = handler

    def handle(self, artifact: ArtifactRef) -> ArtifactRef:
        """Materialize one artifact via its storage-kind handler when configured.

        Args:
            artifact: Artifact reference to handle.

        Returns:
            Transformed artifact when a handler exists, otherwise the original ref.
        """

        handler = self._handlers.get(artifact.storage_kind.value)
        if handler is None:
            return artifact
        return handler(artifact)
