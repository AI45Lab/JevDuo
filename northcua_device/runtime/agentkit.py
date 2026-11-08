"""Lazy AgentKit runtime adapter for standalone device workers."""

from __future__ import annotations

from pathlib import Path

from northcua_protocol.capabilities import Capability, PlatformKind, ToolDescriptor
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.state import CommandState
from northcua_runtime_shared import RuntimeAdapter, RuntimeHealth
from northcua_runtime_shared.tool_contracts import ToolContract


class AgentKitRuntimeAdapter:
    """Device-side bridge to AgentKit when the main NorthCUA package is installed."""

    def __init__(
        self,
        *,
        agent_kinds: set[str] | None = None,
        platforms: set[PlatformKind] | None = None,
        artifact_root: str | Path = "logs/device-agentkit",
    ) -> None:
        self.agent_kinds = set(agent_kinds) if agent_kinds is not None else None
        self.platforms = set(platforms) if platforms is not None else None
        self.artifact_root = artifact_root
        self._delegate: RuntimeAdapter | None = None
        self._load_error: str = ""

    @property
    def capabilities(self) -> set[Capability]:
        """Return capabilities exposed by the optional AgentKit delegate.

        Returns:
            Delegate capabilities, or an empty set when AgentKit is not
            installed in this device package environment.
        """

        delegate = self._load_delegate()
        return delegate.capabilities if delegate is not None else set()

    @property
    def tool_contracts(self) -> list[ToolContract]:
        """Return contracts exposed by the optional AgentKit delegate."""

        delegate = self._load_delegate()
        return delegate.tool_contracts if delegate is not None else []

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Return tool descriptors exposed by the optional AgentKit delegate.

        Returns:
            Delegate tool descriptors, or an empty list when unavailable.
        """

        delegate = self._load_delegate()
        return delegate.tools if delegate is not None else []

    async def healthcheck(self) -> RuntimeHealth:
        """Check whether the optional AgentKit delegate can serve commands.

        Returns:
            Delegate health when available, otherwise an unhealthy status with
            the import/load error.
        """

        delegate = self._load_delegate()
        if delegate is None:
            return RuntimeHealth(healthy=False, message=self._load_error)
        return await delegate.healthcheck()

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute one command through the optional AgentKit delegate.

        Args:
            command: Protocol command received by the standalone device worker.

        Returns:
            Delegate command result, or a failed result when AgentKit is absent.
        """

        delegate = self._load_delegate()
        if delegate is None:
            return CommandResult(
                command_id=command.command_id,
                session_id=command.session_id,
                lease_id=command.lease_id,
                device_id=command.device_id,
                lease_generation=command.lease_generation,
                status=CommandState.FAILED,
                error=self._load_error or "AgentKit runtime adapter unavailable",
            )
        return await delegate.execute(command)

    def _load_delegate(self) -> RuntimeAdapter | None:
        if self._delegate is not None:
            return self._delegate
        try:
            from northcua.agentkit.device_adapter import AgentKitMCPRuntimeAdapter
        except ModuleNotFoundError as exc:  # pragma: no cover - standalone package boundary
            self._load_error = f"AgentKit runtime adapter unavailable: {exc}"
            return None
        self._delegate = AgentKitMCPRuntimeAdapter(
            agent_kinds=self.agent_kinds,
            platforms=self.platforms,
            artifact_root=self.artifact_root,
        )
        return self._delegate
