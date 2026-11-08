"""Sandbox inventory and termination helpers for batch maintenance."""

from __future__ import annotations

from northcua.sandboxes.capacity_contracts import SandboxInventoryItem, SandboxInventoryState
from northcua.sandboxes.contracts import SandboxAllocationStrategy, SandboxProviderSpec
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry

_ACTIVE_STATES = {
    SandboxInventoryState.RUNNING,
    SandboxInventoryState.PAUSED,
    SandboxInventoryState.UNKNOWN,
}


class SandboxMaintenanceClient:
    """Provider-agnostic sandbox inventory and termination facade."""

    def __init__(self, registry: SandboxProviderRegistry | None = None) -> None:
        self._registry = registry or default_sandbox_provider_registry()

    async def inventory(
        self,
        *,
        provider_id: str,
        batch_id: str,
        case_id: str = "",
    ) -> list[SandboxInventoryItem]:
        """Return active NorthCUA-owned sandboxes matching a batch/case.

        Args:
            provider_id: Sandbox provider id.
            batch_id: Batch metadata value to match.
            case_id: Optional task/case metadata value to match.

        Returns:
            Active inventory rows tagged as NorthCUA-owned for this batch.
        """

        from northcua.sandboxes.capacity_contracts import SandboxCapacityProbeRequest

        provider = self._registry.create(provider_id)
        result = await provider.capacity_probe(
            SandboxCapacityProbeRequest(
                provider=provider_id,
                metadata_filter={"batch_id": batch_id},
                include_items=True,
            )
        )
        return [
            item
            for item in result.items
            if _matches_batch(
                item,
                batch_id=batch_id,
                case_id=case_id,
                include_connect_existing=True,
            )
        ]

    async def terminate(
        self,
        *,
        item: SandboxInventoryItem,
        batch_id: str,
        include_connect_existing: bool = False,
    ) -> None:
        """Connect to a matching sandbox inventory item and terminate it.

        Args:
            item: Provider inventory row to terminate.
            batch_id: Batch id that must match item metadata.
            include_connect_existing: Whether rows without task-attempt
                metadata are eligible.

        Raises:
            RuntimeError: If the item does not carry safe NorthCUA ownership
                metadata for this batch.
        """

        if not _matches_batch(
            item,
            batch_id=batch_id,
            case_id="",
            include_connect_existing=include_connect_existing,
        ):
            raise RuntimeError(f"refusing to terminate sandbox without NorthCUA batch ownership: {item.sandbox_id}")
        provider = self._registry.create(item.provider)
        handle = await provider.connect(
            SandboxProviderSpec(
                provider=item.provider,
                template=item.template or item.template_id,
                sandbox_id=item.sandbox_id,
                allocation_strategy=SandboxAllocationStrategy.CONNECT_EXISTING,
                desktop=True,
            )
        )
        await provider.terminate(handle)


def is_terminatable_batch_sandbox(
    item: SandboxInventoryItem,
    *,
    batch_id: str,
    case_id: str = "",
    include_connect_existing: bool = False,
) -> bool:
    """Return whether an inventory item is safe to terminate for a batch.

    Args:
        item: Provider inventory row.
        batch_id: Batch id that must match metadata.
        case_id: Optional task/case id that must match metadata.
        include_connect_existing: Whether rows without task-attempt metadata
            are eligible.

    Returns:
        ``True`` only for active, NorthCUA-owned inventory rows in scope.
    """

    return _matches_batch(
        item,
        batch_id=batch_id,
        case_id=case_id,
        include_connect_existing=include_connect_existing,
    )


def _matches_batch(
    item: SandboxInventoryItem,
    *,
    batch_id: str,
    case_id: str,
    include_connect_existing: bool = False,
) -> bool:
    metadata = item.metadata
    if item.state not in _ACTIVE_STATES:
        return False
    if str(metadata.get("batch_id", "")) != batch_id:
        return False
    if not _truthy(metadata.get("northcua")):
        return False
    if case_id and str(metadata.get("task_id", "")) != case_id:
        return False
    if _is_legacy_reusable_environment(metadata):
        return False
    if include_connect_existing:
        return True
    return bool(str(metadata.get("task_id", ""))) and bool(str(metadata.get("attempt", "")))


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _is_legacy_reusable_environment(metadata: dict[str, object]) -> bool:
    task_id = str(metadata.get("task_id", ""))
    return (
        _truthy(metadata.get("warm_pool"))
        or bool(str(metadata.get("warm_pool_slot", "")))
        or task_id.startswith("warm-pool-")
    )
