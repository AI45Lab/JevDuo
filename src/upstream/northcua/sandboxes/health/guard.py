"""Passive capacity guard for sandbox-backed batch runs."""

from __future__ import annotations

from pydantic import BaseModel

from northcua.sandboxes.contracts import SandboxCapacityProbeRequest, SandboxCapacityProbeResult
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher


class CapacityGuardDecision(BaseModel):
    """Capacity recommendation produced before a batch run.

    Attributes:
        requested_concurrency: User-requested concurrency.
        recommended_concurrency: Passive probe recommendation.
        effective_concurrency: Concurrency selected by the guard.
        auto_clamped: Whether the guard reduced the requested value.
        warnings: Operator-facing capacity warnings.
        probe: Full provider probe result for audit reports.
    """

    requested_concurrency: int
    recommended_concurrency: int
    effective_concurrency: int
    auto_clamped: bool = False
    warnings: list[str]
    probe: SandboxCapacityProbeResult


class CapacityGuard:
    """Run passive capacity probes and optionally clamp concurrency."""

    def __init__(self, registry: SandboxProviderRegistry | None = None) -> None:
        self._registry = registry or default_sandbox_provider_registry()

    async def decide(
        self,
        request: SandboxCapacityProbeRequest,
        *,
        auto_clamp: bool = False,
    ) -> CapacityGuardDecision:
        """Return a concurrency recommendation without creating sandboxes.

        Args:
            request: Passive capacity probe request.
            auto_clamp: Whether to reduce requested concurrency to the
                provider recommendation.

        Returns:
            Capacity decision for CLI output and experiment telemetry.
        """

        provider = self._registry.create(request.provider)
        probe = await SandboxProviderDispatcher(provider).capacity_probe(request)
        requested = request.requested_concurrency
        recommended = probe.recommended_concurrency or requested
        effective = min(requested, recommended) if auto_clamp and requested > 0 else requested
        return CapacityGuardDecision(
            requested_concurrency=requested,
            recommended_concurrency=recommended,
            effective_concurrency=effective,
            auto_clamped=auto_clamp and effective != requested,
            warnings=probe.warnings,
            probe=probe,
        )
