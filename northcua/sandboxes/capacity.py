"""Shared helpers for passive sandbox capacity probes."""

from __future__ import annotations

from northcua.sandboxes.contracts import (
    SandboxCapacityProbeRequest,
    SandboxCapacityProbeResult,
    SandboxInventoryItem,
    SandboxInventoryState,
)

_ACTIVE_STATES = {SandboxInventoryState.RUNNING, SandboxInventoryState.PAUSED, SandboxInventoryState.UNKNOWN}


def build_capacity_probe_result(
    *,
    request: SandboxCapacityProbeRequest,
    source: str,
    items: list[SandboxInventoryItem],
    warnings: list[str] | None = None,
) -> SandboxCapacityProbeResult:
    """Build a normalized capacity result from passive inventory rows.

    Args:
        request: Operator request and configured capacity knobs.
        source: Provider inventory source, for example ``e2b_api``.
        items: Passive inventory rows; callers must not create sandboxes.
        warnings: Optional caveats to include in the result.

    Returns:
        Capacity summary with clamped task/create recommendations.
    """

    active = [item for item in items if item.state in _ACTIVE_STATES]
    running = sum(1 for item in active if item.state == SandboxInventoryState.RUNNING)
    paused = sum(1 for item in active if item.state == SandboxInventoryState.PAUSED)
    matching_template = sum(1 for item in active if _matches_template(item, request.template))
    matching_metadata = sum(1 for item in active if _matches_metadata(item, request.metadata_filter))
    northcua_active = sum(1 for item in active if _is_northcua_owned(item))
    available = _available_capacity(
        max_active=request.max_active_sandboxes,
        active_count=len(active),
        reserve=request.reserve_sandboxes,
    )
    recommended = _recommended_concurrency(
        requested=request.requested_concurrency,
        available=available,
        max_active=request.max_active_sandboxes,
    )
    recommended_create = (
        min(recommended or request.requested_concurrency, request.create_rate_per_second)
        if request.create_rate_per_second > 0
        else 0
    )
    result_warnings = list(warnings or [])
    if request.max_active_sandboxes <= 0:
        result_warnings.append("max_active_sandboxes is unknown; task concurrency was not hard-clamped")
    return SandboxCapacityProbeResult(
        provider=request.provider,
        source=source,
        template=request.template,
        total_active=len(active),
        running=running,
        paused=paused,
        northcua_active=northcua_active,
        matching_template_active=matching_template,
        matching_metadata_active=matching_metadata,
        max_active_sandboxes=request.max_active_sandboxes,
        reserve_sandboxes=request.reserve_sandboxes,
        available_sandboxes=available,
        requested_concurrency=request.requested_concurrency,
        recommended_concurrency=recommended,
        create_rate_per_second=request.create_rate_per_second,
        recommended_create_concurrency=recommended_create,
        warnings=result_warnings,
        items=items if request.include_items else [],
    )


def _available_capacity(*, max_active: int, active_count: int, reserve: int) -> int:
    if max_active <= 0:
        return 0
    return max(max_active - active_count - reserve, 0)


def _recommended_concurrency(*, requested: int, available: int, max_active: int) -> int:
    if requested <= 0:
        return available
    if max_active <= 0:
        return requested
    return min(requested, available)


def _matches_template(item: SandboxInventoryItem, template: str) -> bool:
    if not template:
        return False
    return template in {item.template, item.template_id}


def _matches_metadata(item: SandboxInventoryItem, metadata_filter: dict[str, object]) -> bool:
    if not metadata_filter:
        return False
    return all(str(item.metadata.get(key, "")) == str(value) for key, value in metadata_filter.items())


def _is_northcua_owned(item: SandboxInventoryItem) -> bool:
    value = str(item.metadata.get("northcua", "")).lower()
    return value in {"1", "true", "yes"} or "batch_id" in item.metadata or "experiment_id" in item.metadata
