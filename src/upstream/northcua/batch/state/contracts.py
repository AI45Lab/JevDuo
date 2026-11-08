"""Typed contracts for batch-local task ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from northcua.batch.contracts import BatchManifest, BatchTaskState
from northcua.batch.selected_retry import SelectedRetryPlan


@dataclass(frozen=True)
class LeasePolicy:
    """Timing policy for batch task leases.

    Attributes:
        lease_timeout_seconds: Duration after which a silent runner may be
            replaced by another local runner.
        heartbeat_interval_seconds: Interval used by active runners to extend
            their ownership before the lease expires.
    """

    lease_timeout_seconds: float = 120.0
    heartbeat_interval_seconds: float = 40.0

    @classmethod
    def from_timeout(cls, lease_timeout_seconds: float) -> "LeasePolicy":
        """Create a lease policy using the default one-third heartbeat cadence.

        Args:
            lease_timeout_seconds: Positive lease timeout in seconds.

        Returns:
            Lease policy with heartbeat interval capped to at least one second.

        Raises:
            ValueError: If the timeout is not positive.
        """

        if lease_timeout_seconds <= 0:
            raise ValueError("lease_timeout_seconds must be positive")
        return cls(
            lease_timeout_seconds=lease_timeout_seconds,
            heartbeat_interval_seconds=max(1.0, lease_timeout_seconds / 3.0),
        )


@dataclass(frozen=True)
class BatchTaskLease:
    """Ownership claim over one manifest task.

    Attributes:
        task_id: Manifest task id owned by this lease.
        lease_owner: Runner id that acquired the task.
        lease_token: Opaque token required for state writes.
        generation: Monotonic claim generation for stale-owner detection.
        state: Latest task state at the time of claim.
    """

    task_id: str
    lease_owner: str
    lease_token: str
    generation: int
    state: BatchTaskState


class BatchStateRepository(Protocol):
    """Authoritative batch task state and local lease repository."""

    def initialize_manifest(self, manifest: BatchManifest, *, legacy_jsonl_path: str = "") -> None:
        """Seed repository rows for manifest tasks.

        Args:
            manifest: Manifest whose task ids should exist in the store.
            legacy_jsonl_path: Optional one-time migration source for
                pre-SQLite batch runs. Runners may pass this only when the
                SQLite state file does not yet exist; JSONL audit logs are not
                consulted again after SQLite has been initialized.
        """
        ...

    def reset_selected_for_retry(
        self,
        plan: SelectedRetryPlan,
        *,
        actor: str = "batch resume",
    ) -> dict[str, int | bool]:
        """Atomically requeue exactly the terminal tasks named by a retry plan."""

        ...

    def claim_next(
        self,
        *,
        lease_owner: str,
        policy: LeasePolicy,
        max_attempts_by_task: dict[str, int] | None = None,
        respect_prepared_reservations: bool = False,
        prepared_reservation_stale_after_seconds: float = 0.0,
        prepared_ready_task_attempts: set[tuple[str, int]] | None = None,
    ) -> BatchTaskLease | None:
        """Atomically claim the next runnable task.

        Args:
            lease_owner: Stable id for the current runner process.
            policy: Lease timing policy.
            max_attempts_by_task: Optional retry budget keyed by manifest task
                id. Failed or timed-out tasks are only claimable while their
                recorded attempts are below this total-attempt budget.
            respect_prepared_reservations: Whether active prepared-sandbox
                rows owned by other runners should block claiming. This stays
                disabled for normal/default-off runs so stale prepared rows do
                not affect the control-plane source of truth.
            prepared_reservation_stale_after_seconds: Optional liveness grace
                used when respecting prepared reservations. Rows not refreshed
                inside this window are treated as crashed-runner leftovers.
            prepared_ready_task_attempts: Optional allowlist of task attempts
                with in-memory ready prepared assignments. When provided, the
                claim skips all other rows so pressure-mode scheduling cannot
                claim in-flight prepared rows and fall back to direct create.

        Returns:
            Lease for one task, or ``None`` when no task is currently runnable.
        """
        ...

    def next_claim_after(
        self,
        *,
        lease_owner: str,
        max_attempts_by_task: dict[str, int] | None = None,
    ) -> float | None:
        """Return the earliest future timestamp for delayed retry claims.

        Args:
            lease_owner: Stable id for the current runner process.
            max_attempts_by_task: Optional retry budget keyed by manifest task id.

        Returns:
            Unix timestamp for the next delayed retry, or ``None`` when no
            retryable delayed row exists.
        """
        ...

    def heartbeat(self, lease: BatchTaskLease, *, policy: LeasePolicy) -> bool:
        """Extend an active lease.

        Args:
            lease: Lease returned by ``claim_next``.
            policy: Lease timing policy.

        Returns:
            ``True`` when the lease is still owned by this runner.
        """
        ...

    def upsert_state(self, state: BatchTaskState, *, lease_token: str = "") -> bool:
        """Persist a latest task state.

        Args:
            state: State snapshot to store.
            lease_token: Active lease token required for owned writes.

        Returns:
            ``True`` when the write was accepted.
        """
        ...

    def release_claim(self, lease: BatchTaskLease, *, terminal_state: BatchTaskState | None = None) -> bool:
        """Release task ownership after an attempt completes.

        Args:
            lease: Lease being released.
            terminal_state: Optional final state to persist before release.

        Returns:
            ``True`` when the release matched the current owner.
        """
        ...

    def mark_cancel_requested(self) -> None:
        """Mark unclaimed nonterminal tasks as canceled."""
        ...

    def cancel_requested(self, task_id: str) -> bool:
        """Return whether a task has an explicit operator cancel request.

        Args:
            task_id: Manifest task id to inspect.

        Returns:
            ``True`` only when an explicit batch cancel marker was recorded.
        """
        ...

    def set_control_flag(self, name: str, enabled: bool, *, actor: str = "", reason: str = "") -> None:
        """Persist a batch-level scheduler control flag.

        Args:
            name: Stable flag name such as ``paused`` or ``drain_requested``.
            enabled: Whether the flag should be active.
            actor: Operator or subsystem changing the flag.
            reason: Human-readable reason for audit.
        """
        ...

    def control_flags(self) -> dict[str, bool]:
        """Return current batch-level scheduler control flags.

        Returns:
            Mapping of flag name to boolean value.
        """
        ...

    def get(self, task_id: str) -> BatchTaskState | None:
        """Return the latest state for a task.

        Args:
            task_id: Manifest task id.

        Returns:
            Latest task state, or ``None`` when unknown.
        """
        ...

    def all(self) -> list[BatchTaskState]:
        """Return latest states for all tasks in manifest order.

        Returns:
            Latest task states ordered as the original manifest declared them.
        """
        ...
