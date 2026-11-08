"""Best-effort cleanup for NorthCUA-owned sandbox inventory."""

from __future__ import annotations

from pydantic import BaseModel, Field

from northcua.sandboxes.contracts import SandboxCapacityProbeRequest
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher


class SandboxJanitorReport(BaseModel):
    """Result of a passive janitor scan.

    Attributes:
        provider: Provider scanned by the janitor.
        matched: Number of NorthCUA-owned inventory rows matched.
        terminated: Number of sandboxes the janitor attempted to terminate.
        skipped: Sandbox ids skipped because v1 lacks a safe termination handle.
        errors: Cleanup errors by sandbox id.
    """

    provider: str
    matched: int = 0
    terminated: int = 0
    skipped: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class SandboxJanitor:
    """Scan provider inventory for NorthCUA-owned sandbox leftovers.

    The v1 janitor is conservative: it reports orphan candidates from passive
    inventory and only terminates when a future provider exposes a safe
    inventory-to-handle path. This avoids accidentally deleting external
    sandboxes while still producing actionable audit data.
    """

    def __init__(self, registry: SandboxProviderRegistry | None = None) -> None:
        self._registry = registry or default_sandbox_provider_registry()

    async def scan(self, request: SandboxCapacityProbeRequest) -> SandboxJanitorReport:
        """Return NorthCUA-owned orphan candidates without deleting them.

        Args:
            request: Passive inventory probe request. ``include_items`` is
                forced on so the janitor can inspect metadata.

        Returns:
            Conservative report listing candidate sandbox ids.
        """

        provider = self._registry.create(request.provider)
        probe = await SandboxProviderDispatcher(provider).capacity_probe(
            request.model_copy(update={"include_items": True})
        )
        candidates = [
            item
            for item in probe.items
            if str(item.metadata.get("northcua", "")).lower() in {"1", "true", "yes"}
            or "batch_id" in item.metadata
            or "experiment_id" in item.metadata
        ]
        return SandboxJanitorReport(
            provider=request.provider,
            matched=len(candidates),
            skipped=[item.sandbox_id for item in candidates],
        )
