"""Browser runtime adapter backed by AgentKit tool specs when available."""

from __future__ import annotations

from northcua_protocol.capabilities import PlatformKind

from northcua_device.runtime.agentkit import AgentKitRuntimeAdapter


class BrowserRuntimeAdapter(AgentKitRuntimeAdapter):
    """Browser data-plane adapter for web AgentKit tools."""

    def __init__(self, *, agent_kinds: set[str] | None = None) -> None:
        super().__init__(
            agent_kinds=agent_kinds if agent_kinds is not None else {"web"},
            platforms={PlatformKind.BROWSER},
            artifact_root="logs/device-browser",
        )
