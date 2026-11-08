"""Provider-side sandbox cleanup helpers for controller residue repair."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from northcua.maintenance.cleanup_controller_models import CleanupCandidate, CleanupPlan
from northcua.sandboxes.capacity_contracts import SandboxInventoryItem, SandboxInventoryState

if TYPE_CHECKING:
    from northcua.batch.maintenance.sandbox_ops import SandboxMaintenanceClient

logger = logging.getLogger(__name__)

ACTIVE_DEVICE_STATUSES = {"connecting", "online", "draining", "unhealthy"}
SANDBOX_TERMINATE_CONCURRENCY = 8
SANDBOX_TERMINATE_TIMEOUT_SECONDS = 30.0


async def terminate_candidate_sandboxes(
    *,
    plan: CleanupPlan,
    inventory_by_sandbox: dict[str, SandboxInventoryItem],
    sandbox_client: "SandboxMaintenanceClient",
) -> None:
    """Terminate cleanup candidates that are present in provider inventory.

    Args:
        plan: Cleanup plan whose candidates are being applied.
        inventory_by_sandbox: Provider inventory keyed by sandbox id.
        sandbox_client: Provider maintenance client.
    """

    semaphore = asyncio.Semaphore(SANDBOX_TERMINATE_CONCURRENCY)

    async def _terminate_one(candidate: CleanupCandidate) -> None:
        if not candidate.sandbox_id or candidate.sandbox_id not in inventory_by_sandbox:
            return
        item = inventory_by_sandbox[candidate.sandbox_id]
        async with semaphore:
            try:
                await _terminate_with_timeout(
                    sandbox_client,
                    item=item,
                    batch_id=plan.batch_id,
                    include_connect_existing=True,
                )
                candidate.provider_status = "terminated"
            except Exception as exc:  # noqa: BLE001 - cleanup should keep moving and report partials.
                record_terminate_error(plan, candidate, exc, prefix="sandbox terminate failed")
                if candidate.provider_status != "provider_missing":
                    logger.warning("sandbox terminate failed during controller residue cleanup", exc_info=True)

    await asyncio.gather(*(_terminate_one(candidate) for candidate in plan.candidates))


async def terminate_live_db_known_sandboxes(
    *,
    plan: CleanupPlan,
    sandbox_client: "SandboxMaintenanceClient",
    provider: str,
) -> None:
    """Terminate DB-known candidate sandboxes that provider inventory omitted.

    Args:
        plan: Cleanup plan whose candidates are being applied.
        sandbox_client: Provider maintenance client.
        provider: Provider id used when candidate metadata lacks one.
    """

    semaphore = asyncio.Semaphore(SANDBOX_TERMINATE_CONCURRENCY)

    async def _terminate_one(candidate: CleanupCandidate) -> None:
        if not candidate.sandbox_id or candidate.provider_status:
            return
        item = SandboxInventoryItem(
            sandbox_id=candidate.sandbox_id,
            provider=str(candidate.metadata.get("provider") or provider),
            template=str(candidate.metadata.get("template") or "osworld_desktop"),
            state=SandboxInventoryState.RUNNING,
            metadata={
                "northcua": True,
                "batch_id": plan.batch_id,
                "task_id": candidate.case_id,
                "attempt": str(candidate.metadata.get("attempt") or "1"),
            },
        )
        async with semaphore:
            try:
                await _terminate_with_timeout(
                    sandbox_client,
                    item=item,
                    batch_id=plan.batch_id,
                    include_connect_existing=True,
                )
                candidate.provider_status = "terminated"
            except Exception as exc:  # noqa: BLE001 - direct cleanup should keep moving and report partials.
                record_terminate_error(plan, candidate, exc, prefix="direct sandbox terminate failed")
                if candidate.provider_status != "provider_missing":
                    logger.warning("direct sandbox terminate failed during controller residue cleanup", exc_info=True)

    await asyncio.gather(*(_terminate_one(candidate) for candidate in plan.candidates))


async def _terminate_with_timeout(
    sandbox_client: "SandboxMaintenanceClient",
    *,
    item: SandboxInventoryItem,
    batch_id: str,
    include_connect_existing: bool,
) -> None:
    try:
        await asyncio.wait_for(
            sandbox_client.terminate(
                item=item,
                batch_id=batch_id,
                include_connect_existing=include_connect_existing,
            ),
            timeout=SANDBOX_TERMINATE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise TimeoutError(f"sandbox terminate timed out after {SANDBOX_TERMINATE_TIMEOUT_SECONDS:.1f}s") from exc


def record_terminate_error(plan: CleanupPlan, candidate: CleanupCandidate, exc: Exception, *, prefix: str) -> None:
    """Record one provider termination error on the candidate and plan.

    Args:
        plan: Cleanup plan accumulating warnings.
        candidate: Candidate whose provider cleanup failed.
        exc: Provider exception raised during termination.
        prefix: Operator-facing warning prefix.
    """

    if is_sandbox_missing_error(exc, sandbox_id=candidate.sandbox_id):
        candidate.provider_status = "provider_missing"
        return
    candidate.provider_status = "terminate_failed"
    plan.warnings.append(f"{prefix}: {candidate.sandbox_id}: {type(exc).__name__}: {exc}")


def is_sandbox_missing_error(exc: Exception, *, sandbox_id: str = "") -> bool:
    """Return whether a provider error specifically means the sandbox is gone.

    Args:
        exc: Provider exception raised during connect/terminate.
        sandbox_id: Optional provider sandbox id that was being terminated.

    Returns:
        True only for sandbox-specific not-found errors. Generic 404 strings
        are not enough because provider APIs may use 404 for routes/accounts.
    """

    message = str(exc).lower()
    normalized_sandbox_id = sandbox_id.lower()
    return bool(
        normalized_sandbox_id and normalized_sandbox_id in message and ("not found" in message or "404" in message)
    )
