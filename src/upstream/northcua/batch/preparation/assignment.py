"""Prepared sandbox assignment objects used by attempt execution."""

from __future__ import annotations

import time
from dataclasses import dataclass

from northcua.batch.preparation.contracts import PreparedSandboxRecord
from northcua.batch.runner.leases import BatchTaskLeaseContext
from northcua.sandboxes.contracts import SandboxLease


@dataclass(frozen=True)
class PreparedSandboxAssignment:
    """A ready single-use sandbox lease bound to a claimed task attempt.

    Attributes:
        record: SQLite reservation row after assignment.
        lease: Sandbox lease submitted to the controller.
        lease_context: Full lease context retained for the existing release
            path so cleanup behavior stays identical to non-prepared tasks.
    """

    record: PreparedSandboxRecord
    lease: SandboxLease
    lease_context: BatchTaskLeaseContext

    @property
    def key(self) -> tuple[str, int]:
        """Return the assigned task-attempt key.

        Returns:
            ``(task_id, attempt)`` tuple for cache and lifecycle lookups.
        """

        return self.record.key

    def prepared_ttl_seconds(self) -> float:
        """Return the originally reserved prepared-sandbox TTL in seconds.

        Returns:
            Difference between reservation expiry and reservation time, or zero
            when the record does not carry both timestamps.
        """

        if self.record.expires_at <= 0 or self.record.reserved_at <= 0:
            return 0.0
        return max(self.record.expires_at - self.record.reserved_at, 0.0)

    def prepared_age_seconds(self, *, now: float | None = None) -> float:
        """Return seconds since the prepared sandbox became ready.

        Args:
            now: Optional monotonic wall-clock sample used by callers that need
                consistent telemetry across multiple fields.

        Returns:
            Age of the prepared sandbox, anchored at ready/start/reserve time.
        """

        timestamp = time.time() if now is None else now
        anchor = self.record.prepared_at or self.record.prepare_started_at or self.record.reserved_at
        if anchor <= 0:
            return 0.0
        return max(timestamp - anchor, 0.0)

    def remaining_ttl_seconds(self, *, now: float | None = None) -> float:
        """Return seconds until the original prepared TTL cliff.

        Args:
            now: Optional wall-clock timestamp for consistent telemetry.

        Returns:
            Positive seconds until expiry, or a negative value after expiry.
        """

        timestamp = time.time() if now is None else now
        if self.record.expires_at <= 0:
            return 0.0
        return self.record.expires_at - timestamp

    def prepared_to_assigned_seconds(self) -> float:
        """Return seconds the prepared sandbox waited before assignment.

        Returns:
            Delay between ready/start/reserve time and assignment time.
        """

        anchor = self.record.prepared_at or self.record.prepare_started_at or self.record.reserved_at
        if self.record.assigned_at <= 0 or anchor <= 0:
            return 0.0
        return max(self.record.assigned_at - anchor, 0.0)

    def assigned_runtime_seconds(self, *, now: float | None = None) -> float:
        """Return seconds elapsed since assignment.

        Args:
            now: Optional wall-clock timestamp for consistent telemetry.

        Returns:
            Runtime age after assignment, or zero before assignment.
        """

        timestamp = time.time() if now is None else now
        if self.record.assigned_at <= 0:
            return 0.0
        return max(timestamp - self.record.assigned_at, 0.0)

    def expired_before_assignment(self) -> bool:
        """Return whether the original prepared TTL had elapsed before assignment.

        Returns:
            True when assignment happened at or after the recorded expiry time.
        """

        return bool(
            self.record.expires_at > 0
            and self.record.assigned_at > 0
            and self.record.expires_at <= self.record.assigned_at
        )

    def expired_after_assignment(self, *, now: float | None = None) -> bool:
        """Return whether the original prepared TTL elapsed during the assignment.

        Args:
            now: Optional wall-clock timestamp for consistent telemetry.

        Returns:
            True when the sandbox was assigned before expiry but is now past
            the original prepared TTL.
        """

        timestamp = time.time() if now is None else now
        return bool(
            self.record.expires_at > 0
            and self.record.assigned_at > 0
            and self.record.expires_at > self.record.assigned_at
            and self.record.expires_at <= timestamp
        )
