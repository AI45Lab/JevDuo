"""Heartbeat guard for local batch task leases."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from northcua.batch.contracts import BatchTaskStatus
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.state import BatchTaskLease, LeasePolicy
from northcua.batch.state.async_repository import repository_call

logger = logging.getLogger(__name__)
_CANCEL_POLL_SECONDS = 1.0


class BatchLeaseLostError(RuntimeError):
    """Raised when a runner loses ownership of a task lease mid-attempt."""


@dataclass(slots=True)
class BatchLeaseHeartbeat:
    """Background heartbeat handle for one claimed batch task.

    Attributes:
        task: Asyncio task that refreshes the repository lease.
        lost: Event set when the repository rejects the heartbeat, usually
            because another runner reclaimed an expired lease.
    """

    task: asyncio.Task[None]
    lost: asyncio.Event

    async def close(self) -> None:
        """Stop the heartbeat task and wait for cancellation cleanup."""

        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass


def start_batch_lease_heartbeat(
    *,
    events: BatchEventWriter,
    claim: BatchTaskLease,
    policy: LeasePolicy,
) -> BatchLeaseHeartbeat:
    """Start a heartbeat that can signal ownership loss to the attempt.

    Args:
        events: Task-scoped event writer carrying the authoritative repository.
        claim: Local task claim acquired from the repository.
        policy: Lease timeout and heartbeat interval.

    Returns:
        Heartbeat handle whose ``lost`` event should cancel external side
        effects if ownership is stolen by another runner.
    """

    lost = asyncio.Event()
    task = asyncio.create_task(
        _heartbeat_batch_lease(events=events, claim=claim, policy=policy, lost=lost),
        name=f"northcua-batch-heartbeat:{claim.task_id}",
    )
    return BatchLeaseHeartbeat(task=task, lost=lost)


async def _heartbeat_batch_lease(
    *,
    events: BatchEventWriter,
    claim: BatchTaskLease,
    policy: LeasePolicy,
    lost: asyncio.Event,
) -> None:
    """Extend a local batch lease until ownership is lost or task completes.

    Args:
        events: Event writer whose repository owns the lease row.
        claim: Claim being refreshed.
        policy: Lease timing policy.
        lost: Event set when the heartbeat is rejected.
    """

    next_heartbeat_at = time.monotonic()
    while True:
        state = await repository_call(lambda: events.repository.get(claim.task_id))
        if state is not None and state.status == BatchTaskStatus.CANCELED:
            cancel_requested = await repository_call(lambda: events.repository.cancel_requested(claim.task_id))
            if cancel_requested:
                logger.warning(
                    "batch task lease canceled by operator",
                    extra={"task_id": claim.task_id, "lease_owner": claim.lease_owner},
                )
                lost.set()
                return
        now = time.monotonic()
        if now >= next_heartbeat_at:
            heartbeat_ok = await repository_call(lambda: events.repository.heartbeat(claim, policy=policy))
            if not heartbeat_ok:
                logger.warning(
                    "batch task lease heartbeat lost ownership",
                    extra={"task_id": claim.task_id, "lease_owner": claim.lease_owner},
                )
                lost.set()
                return
            next_heartbeat_at = now + policy.heartbeat_interval_seconds
        await asyncio.sleep(min(_CANCEL_POLL_SECONDS, policy.heartbeat_interval_seconds))
