"""Registry for process-worker runner strategies."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import ProcessWorkerRunnerFactory
from .desktop import DesktopProcessWorkerRunnerFactory
from .web import WebProcessWorkerRunnerFactory


class ProcessWorkerRunnerRegistry:
    """Resolve process-safe runner factories by open agent-kind key."""

    def __init__(self, factories: Iterable[ProcessWorkerRunnerFactory] = ()) -> None:
        self._factories: dict[str, ProcessWorkerRunnerFactory] = {}
        for factory in factories:
            self.register(factory)

    @classmethod
    def default(cls) -> ProcessWorkerRunnerRegistry:
        """Return the built-in process runner registry."""

        return cls((DesktopProcessWorkerRunnerFactory(), WebProcessWorkerRunnerFactory()))

    def register(self, factory: ProcessWorkerRunnerFactory) -> None:
        """Register or replace one agent-kind runner strategy."""

        agent_kind = factory.agent_kind.strip()
        if not agent_kind:
            raise ValueError("process worker runner agent_kind must not be empty")
        self._factories[agent_kind] = factory

    def resolve(self, agent_kind: str) -> ProcessWorkerRunnerFactory | None:
        """Resolve one process runner strategy, if installed."""

        return self._factories.get(agent_kind)

    def supported_agent_kinds(self) -> tuple[str, ...]:
        """Return supported agent kinds in stable order."""

        return tuple(sorted(self._factories))
