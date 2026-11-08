"""Registry/factory for platform runtime implementations."""

from __future__ import annotations

from collections.abc import Callable

from northcua.platforms.protocol import PlatformKind, PlatformRuntime

PlatformRuntimeFactory = Callable[[], PlatformRuntime]


class PlatformRuntimeRegistry:
    """Typed in-process registry for platform runtime factories."""

    def __init__(self) -> None:
        self._factories: dict[PlatformKind, PlatformRuntimeFactory] = {}

    def register(self, kind: PlatformKind, factory: PlatformRuntimeFactory) -> None:
        """Register or replace one platform runtime factory.

        Args:
            kind: Platform kind handled by the factory.
            factory: Zero-argument runtime factory.
        """

        self._factories[kind] = factory

    def create(self, kind: PlatformKind) -> PlatformRuntime:
        """Create a runtime for a registered platform kind.

        Args:
            kind: Platform kind to instantiate.

        Returns:
            Platform runtime instance.

        Raises:
            KeyError: If no factory is registered for the platform kind.
        """

        return self._factories[kind]()

    def registered_kinds(self) -> tuple[PlatformKind, ...]:
        """Return registered platform kinds.

        Returns:
            Registered platform kinds in insertion order.
        """

        return tuple(self._factories)


def default_platform_runtime_registry() -> PlatformRuntimeRegistry:
    """Create a registry with built-in descriptor runtimes.

    Returns:
        Registry containing desktop and browser platform runtime factories.
    """

    from northcua.platforms.browser import BrowserPlatformRuntime
    from northcua.platforms.desktop import DesktopPlatformRuntime

    registry = PlatformRuntimeRegistry()
    registry.register(PlatformKind.DESKTOP, DesktopPlatformRuntime)
    registry.register(PlatformKind.BROWSER, BrowserPlatformRuntime)
    return registry
