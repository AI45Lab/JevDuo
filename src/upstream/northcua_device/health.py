"""Device worker health projections."""

from __future__ import annotations

from dataclasses import dataclass, field

from northcua_device.executor import CommandExecutor


@dataclass(slots=True, frozen=True)
class DeviceHealthReport:
    """Structured worker-health projection for CLI and ops checks.

    Attributes:
        healthy: Overall readiness flag.
        capability_health: Capability-level health keyed by capability value.
        messages: Human-readable details from runtime adapters.
    """

    healthy: bool
    capability_health: dict[str, bool] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


class DeviceHealthReporter:
    """Build readiness reports from runtime adapter health snapshots."""

    def __init__(self, executor: CommandExecutor) -> None:
        self.executor = executor

    async def report(self) -> DeviceHealthReport:
        """Return a health snapshot suitable for CLI/operator diagnostics.

        Returns:
            Aggregated runtime health report for doctor/readiness commands.
        """

        snapshots = await self.executor.runtime_health_snapshots()
        capability_health: dict[str, bool] = {}
        messages: list[str] = []
        healthy = True
        for snapshot in snapshots:
            healthy = healthy and snapshot.healthy
            capability_health.update(
                {capability.value: status for capability, status in snapshot.capability_health.items()}
            )
            if snapshot.message:
                messages.append(snapshot.message)
        return DeviceHealthReport(
            healthy=healthy,
            capability_health=capability_health,
            messages=messages,
        )


__all__ = ["DeviceHealthReport", "DeviceHealthReporter"]
