"""AgentKit registry and built-in discovery."""

from __future__ import annotations

from importlib.metadata import entry_points
from threading import RLock

from .spec import AgentSpec


class AgentKitRegistry:
    """Process-local registry of declarative CUA agent specs."""

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        """Register one agent spec.

        Args:
            spec: Fully declared AgentKit spec to make discoverable.

        Raises:
            ValueError: If the spec has an empty kind or conflicts with a
                different already-registered spec.
        """

        kind = spec.kind.strip()
        if not kind:
            raise ValueError("Agent kind must not be empty.")
        existing = self._specs.get(kind)
        if existing is not None and existing is not spec:
            raise ValueError(f"Agent kind already registered: {kind}")
        self._specs[kind] = spec

    def get(self, kind: str) -> AgentSpec:
        """Return a registered spec.

        Args:
            kind: Agent kind to resolve.

        Returns:
            Registered spec for ``kind``.
        """

        return self._specs[kind]

    def maybe_get(self, kind: str) -> AgentSpec | None:
        """Return a registered spec, if present.

        Args:
            kind: Agent kind to resolve.

        Returns:
            Registered spec, or ``None`` when the kind is unknown.
        """

        return self._specs.get(kind)

    def all(self) -> tuple[AgentSpec, ...]:
        """Return all registered specs in stable order.

        Returns:
            Specs sorted by kind for deterministic CLI and tests.
        """

        return tuple(self._specs[key] for key in sorted(self._specs))

    def enabled(self) -> tuple[AgentSpec, ...]:
        """Return enabled specs in stable order.

        Returns:
            Enabled specs sorted by kind for deterministic generated surfaces.
        """

        return tuple(spec for spec in self.all() if spec.enabled)

    def clear(self) -> None:
        """Clear the registry for tests."""

        self._specs.clear()


_LOCK = RLock()
_REGISTRY = AgentKitRegistry()
_BUILTINS_LOADED = False
_ENTRY_POINTS_LOADED = False


def get_agentkit_registry() -> AgentKitRegistry:
    """Return the process-global AgentKit registry.

    Returns:
        Mutable registry used by built-ins, entry points, and tests.
    """

    return _REGISTRY


def ensure_builtin_agent_specs_registered() -> None:
    """Load built-in desktop/web specs once."""

    global _BUILTINS_LOADED
    with _LOCK:
        if _BUILTINS_LOADED:
            return
        from .builtins import builtin_agent_specs

        for spec in builtin_agent_specs():
            _REGISTRY.register(spec)
        _BUILTINS_LOADED = True


def load_agentkit_entry_points() -> None:
    """Load third-party AgentKit specs from entry points once."""

    global _ENTRY_POINTS_LOADED
    with _LOCK:
        if _ENTRY_POINTS_LOADED:
            return
        for entry_point in entry_points(group="northcua.agentkit.agents"):
            loaded = entry_point.load()
            spec = loaded() if callable(loaded) else loaded
            if isinstance(spec, AgentSpec):
                _REGISTRY.register(spec)
        _ENTRY_POINTS_LOADED = True


def load_all_agent_specs() -> AgentKitRegistry:
    """Load built-ins and entry points, then return the registry.

    Returns:
        Process-global registry after built-in and entry-point discovery.
    """

    ensure_builtin_agent_specs_registered()
    load_agentkit_entry_points()
    return _REGISTRY


def reset_agentkit_registry_for_tests() -> None:
    """Reset registry state for focused unit tests."""

    global _BUILTINS_LOADED, _ENTRY_POINTS_LOADED
    with _LOCK:
        _REGISTRY.clear()
        _BUILTINS_LOADED = False
        _ENTRY_POINTS_LOADED = False
