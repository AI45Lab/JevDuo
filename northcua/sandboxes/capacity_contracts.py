"""Sandbox capacity probe contracts."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from northcua_protocol.capabilities import JsonObject


class SandboxInventoryState(str, Enum):
    """Provider-reported sandbox state normalized for capacity probes."""

    RUNNING = "running"
    PAUSED = "paused"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


class SandboxInventoryItem(BaseModel):
    """One sandbox returned by a passive provider inventory probe.

    Attributes:
        sandbox_id: Provider sandbox id.
        provider: Provider id that returned the item.
        template: Human-facing template alias when the provider exposes one.
        template_id: Provider template id when distinct from the alias.
        state: Normalized lifecycle state.
        metadata: Provider metadata attached to the sandbox.
        cpu_count: Reported CPU count when available.
        memory_mb: Reported memory in MiB when available.
        disk_mb: Reported disk in MiB when available.
        started_at: Provider start timestamp when available.
        desktop_url: Optional GUI stream URL for operator inspection.
    """

    sandbox_id: str
    provider: str
    template: str = ""
    template_id: str = ""
    state: SandboxInventoryState = SandboxInventoryState.UNKNOWN
    metadata: JsonObject = Field(default_factory=dict)
    cpu_count: int = 0
    memory_mb: int = 0
    disk_mb: int = 0
    started_at: str = ""
    desktop_url: str = ""


class SandboxCapacityProbeRequest(BaseModel):
    """Passive sandbox capacity probe input.

    Attributes:
        provider: Provider id to inspect.
        template: Optional template alias/id used for matching counts.
        requested_concurrency: User-requested task concurrency to clamp.
        max_active_sandboxes: Configured account/template upper bound. ``0``
            means unknown and disables hard clamping.
        reserve_sandboxes: Capacity to leave unused for manual debugging or
            other workloads.
        create_rate_per_second: Configured sandbox create rate. ``0`` means
            unknown and disables create-concurrency recommendation.
        metadata_filter: Metadata key/value filters used for operator-focused
            counts. Filtering is client-side and never creates sandboxes.
        include_items: Whether to include inventory rows in CLI/API output.
    """

    provider: str = "e2b"
    template: str = ""
    requested_concurrency: int = 0
    max_active_sandboxes: int = 0
    reserve_sandboxes: int = 0
    create_rate_per_second: int = 0
    metadata_filter: JsonObject = Field(default_factory=dict)
    include_items: bool = False

    @model_validator(mode="after")
    def _validate_non_negative(self) -> "SandboxCapacityProbeRequest":
        """Ensure numeric capacity knobs are non-negative."""

        for field_name in (
            "requested_concurrency",
            "max_active_sandboxes",
            "reserve_sandboxes",
            "create_rate_per_second",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        return self


class SandboxCapacityProbeResult(BaseModel):
    """Passive sandbox capacity probe output.

    Attributes:
        provider: Provider id that produced the result.
        source: Where provider inventory came from.
        template: Template alias/id used for matching counts.
        total_active: Total non-terminal sandboxes visible to the provider.
        running: Running sandbox count.
        paused: Paused sandbox count.
        northcua_active: Active sandboxes tagged as NorthCUA-owned.
        matching_template_active: Active sandboxes matching ``template``.
        matching_metadata_active: Active sandboxes matching ``metadata_filter``.
        max_active_sandboxes: Configured active sandbox limit. ``0`` means
            unknown.
        reserve_sandboxes: Reserved capacity excluded from recommendations.
        available_sandboxes: Capacity remaining after active and reserve.
        requested_concurrency: Requested task concurrency.
        recommended_concurrency: Safe task concurrency after passive clamping.
        create_rate_per_second: Configured create rate.
        recommended_create_concurrency: Suggested concurrent sandbox creates.
        warnings: Non-fatal caveats for operators.
        items: Optional inventory rows when requested.
    """

    provider: str
    source: str = "unknown"
    template: str = ""
    total_active: int = 0
    running: int = 0
    paused: int = 0
    northcua_active: int = 0
    matching_template_active: int = 0
    matching_metadata_active: int = 0
    max_active_sandboxes: int = 0
    reserve_sandboxes: int = 0
    available_sandboxes: int = 0
    requested_concurrency: int = 0
    recommended_concurrency: int = 0
    create_rate_per_second: int = 0
    recommended_create_concurrency: int = 0
    warnings: list[str] = Field(default_factory=list)
    items: list[SandboxInventoryItem] = Field(default_factory=list)
