"""Registry for sandbox providers."""

from __future__ import annotations

from collections.abc import Callable

from northcua.sandboxes.provider.base import SandboxProvider

ProviderFactory = Callable[[], SandboxProvider]


class SandboxProviderRegistry:
    """Small factory registry for sandbox provider adapters."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider_id: str, factory: ProviderFactory) -> None:
        """Register a provider factory.

        Args:
            provider_id: Stable provider id used in manifests.
            factory: Callable that builds an adapter instance.

        Raises:
            ValueError: If the provider id is empty or already registered.
        """

        if not provider_id:
            raise ValueError("sandbox provider id cannot be empty")
        if provider_id in self._factories:
            raise ValueError(f"sandbox provider already registered: {provider_id}")
        self._factories[provider_id] = factory

    def create(self, provider_id: str) -> SandboxProvider:
        """Create a provider adapter by id.

        Args:
            provider_id: Registered provider id.

        Returns:
            Provider adapter instance.

        Raises:
            ValueError: If no provider factory is registered for the id.
        """

        try:
            return self._factories[provider_id]()
        except KeyError as exc:
            raise ValueError(f"Unknown sandbox provider: {provider_id}") from exc

    def provider_ids(self) -> tuple[str, ...]:
        """Return registered provider ids in deterministic order.

        Returns:
            Sorted provider ids currently registered.
        """

        return tuple(sorted(self._factories))


def default_sandbox_provider_registry() -> SandboxProviderRegistry:
    """Build the default provider registry with lazy optional providers.

    Returns:
        Registry containing fake, local-process and E2B provider factories.
    """

    from northcua.sandboxes.provider.e2b import E2BSandboxProvider
    from northcua.sandboxes.provider.fake import FakeSandboxProvider
    from northcua.sandboxes.provider.local_process import LocalProcessSandboxProvider

    registry = SandboxProviderRegistry()
    registry.register("fake", FakeSandboxProvider)
    registry.register("e2b", E2BSandboxProvider)
    registry.register("local-process", LocalProcessSandboxProvider)
    return registry
