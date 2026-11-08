"""Runtime context envelopes for platform adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexau.archs.main_sub.config import AgentConfig

    from northcua.core.config import NorthCUAAppConfig


@dataclass(frozen=True, slots=True)
class PlatformRuntimeContext:
    """Small dependency envelope passed to platform runtimes.

    Attributes:
        session_id: Active NorthCUA session identifier.
        artifact_store: Session-scoped artifact store. Kept as ``object`` so
            platform contracts do not depend back on agentinfra.
        app_config: NorthCUA app config.
        agent_config: NexAU agent config.
    """

    session_id: str
    artifact_store: object
    app_config: "NorthCUAAppConfig"
    agent_config: "AgentConfig"
