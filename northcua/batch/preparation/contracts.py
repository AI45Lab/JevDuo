"""Typed contracts for single-use prepared sandbox reservations."""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from northcua_protocol.capabilities import JsonObject


class PreparedSandboxDepth(str, Enum):
    """How much per-case initialization happens before task claim."""

    FIXTURE_SETUP = "fixture_setup"
    FULL_INIT = "full_init"


class PreparedSandboxStatus(str, Enum):
    """SQLite lifecycle state for one prepared sandbox reservation."""

    RESERVED = "reserved"
    PREPARING = "preparing"
    PREPARED = "prepared"
    ASSIGNED = "assigned"
    CLEANUP_DEFERRED = "cleanup_deferred"
    RELEASED = "released"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELED = "canceled"
    CLEANUP_FAILED = "cleanup_failed"


class PreparedSandboxFallbackReason(str, Enum):
    """Why a claimed task fell back to normal sandbox acquisition."""

    DISABLED = "disabled"
    MISS = "miss"
    TIMEOUT = "timeout"
    EXPIRED = "expired"
    CLAIM_REJECTED = "claim_rejected"
    IN_MEMORY_LEASE_MISSING = "in_memory_lease_missing"
    INSUFFICIENT_REMAINING_TTL = "insufficient_remaining_ttl"
    PREPARE_FAILED = "prepare_failed"
    DEVICE_NOT_READY = "device_not_ready"


class PreparedSandboxFailureKind(str, Enum):
    """Why a background prepared sandbox attempt failed."""

    CREATION_FAILED = "creation_failed"
    CREATION_CANCELED = "creation_canceled"
    CLAIM_WAIT_TIMEOUT_CANCELLED_FUTURE = "claim_wait_timeout_cancelled_future"
    MANAGER_CLOSED_BEFORE_ASSIGNMENT = "manager_closed_before_assignment"
    PREPARATION_STOPPED_BY_CONTROL = "preparation_stopped_by_control"
    EXPIRED_BEFORE_ASSIGNMENT = "expired_before_assignment"


class PreparedSandboxEventMessage(str, Enum):
    """Stable prepared-sandbox event names persisted in telemetry."""

    RESERVED = "prepared_sandbox_reserved"
    READY = "prepared_sandbox_ready"
    ASSIGNED = "prepared_sandbox_assigned"
    RELEASED = "prepared_sandbox_released"
    FAILED = "prepared_sandbox_failed"
    EXPIRED = "prepared_sandbox_expired"
    FALLBACK = "prepared_sandbox_fallback"
    CLEANUP_FAILED = "prepared_sandbox_cleanup_failed"
    CLEANUP_DEFERRED = "prepared_sandbox_cleanup_deferred"
    LEASE_EXTENDED = "prepared_sandbox_lease_extended"
    MANAGER_CLOSING = "prepared_sandbox_manager_closing"
    INITIAL_WARMUP_STARTED = "prepared_sandbox_initial_warmup_started"
    INITIAL_WARMUP_FINISHED = "prepared_sandbox_initial_warmup_finished"
    DEFERRED_STARTUP_STARTED = "prepared_sandbox_deferred_startup_started"
    DEFERRED_STARTUP_FINISHED = "prepared_sandbox_deferred_startup_finished"


class PreparedSandboxPolicy(BaseModel):
    """Runtime policy for the prepared sandbox queue.

    The queue is intentionally disabled by default. When enabled, each
    prepared sandbox belongs to exactly one task attempt and is destroyed by
    the existing task release path after that attempt completes.

    Attributes:
        enabled: Whether the prepared queue should run.
        depth: Amount of per-case initialization to execute before task claim.
        buffer: Desired number of active prepared reservations; zero follows
            batch run concurrency.
        prepare_concurrency: Concurrent background prepare operations; zero
            follows sandbox create concurrency.
        initial_warmup_wait_seconds: Maximum batch-start wait for the first
            prepared sandbox to become ready. This is intentionally separate
            from ``claim_wait_seconds`` so prepared warmup cannot become a
            global batch-start barrier.
        claim_pending_wait_seconds: Maximum foreground wait when a task claim
            sees an in-flight prepared future but no ready assignment. Zero
            makes prepared fail open immediately to normal sandbox creation.
        ttl_seconds: Maximum lifetime for an unassigned prepared sandbox.
        claim_wait_seconds: How long a claimed task waits for an in-flight
            prepare before falling back to normal sandbox acquisition.
        min_assignment_ttl_seconds: Minimum remaining prepared lease lifetime
            required before assigning a prepared sandbox; zero derives this
            from the task timeout plus ``assignment_ttl_margin_seconds``.
        assignment_ttl_margin_seconds: Extra teardown/clock-skew margin added
            to the task timeout when deriving the claim-time TTL guard.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    enabled: bool = False
    depth: PreparedSandboxDepth = PreparedSandboxDepth.FULL_INIT
    buffer: int = Field(default=0, ge=0)
    prepare_concurrency: int = Field(default=0, ge=0)
    initial_warmup_wait_seconds: float = Field(default=5.0, ge=0)
    claim_pending_wait_seconds: float = Field(default=0.0, ge=0)
    ttl_seconds: float = Field(default=600.0, gt=0)
    claim_wait_seconds: float = Field(default=3600.0, ge=0)
    min_assignment_ttl_seconds: float = Field(default=0.0, ge=0)
    assignment_ttl_margin_seconds: float = Field(default=300.0, ge=0)

    @property
    def mode(self) -> str:
        """Return a compact operator-facing mode string.

        Returns:
            ``"enabled"`` when preparation is active, otherwise ``"disabled"``.
        """

        return "enabled" if self.enabled else "disabled"

    def effective_buffer(self, *, run_concurrency: int) -> int:
        """Resolve zero-as-default buffer values against run concurrency.

        Args:
            run_concurrency: Effective task concurrency for the batch run.

        Returns:
            Concrete target number of active prepared reservations.
        """

        if not self.enabled:
            return 0
        if self.buffer > 0:
            return self.buffer
        return max(run_concurrency, 1)

    def effective_prepare_concurrency(self, *, create_concurrency: int) -> int:
        """Resolve zero-as-default prepare concurrency values.

        Args:
            create_concurrency: Effective sandbox create concurrency.

        Returns:
            Concrete background prepare concurrency.
        """

        if not self.enabled:
            return 0
        if self.prepare_concurrency > 0:
            return self.prepare_concurrency
        if create_concurrency > 0:
            return create_concurrency
        return 1


class PreparedSandboxRecord(BaseModel):
    """Persisted reservation row for one task attempt.

    Attributes:
        task_id: Batch task id this sandbox is reserved for.
        attempt: One-based task attempt prepared by this row.
        status: Reservation lifecycle status.
        runner_id: Runner process that owns the reservation.
        depth: Initialization depth used while preparing.
        provider: Sandbox provider id.
        template: Provider template alias.
        sandbox_id: Provider sandbox id once capacity exists.
        device_id: Device worker id once ready.
        lease_id: Internal sandbox lease id.
        reservation_token: Stable compare-and-swap token for row updates.
        reserved_at: Unix timestamp for reservation creation.
        updated_at: Unix timestamp for the last row mutation.
        expires_at: Unix timestamp after which unassigned rows can expire.
        prepare_started_at: Unix timestamp for background prepare start.
        prepared_at: Unix timestamp for ready state.
        assigned_at: Unix timestamp for runner assignment.
        released_at: Unix timestamp for normal release.
        prepare_seconds: Wall-clock seconds spent preparing the sandbox.
        error: Last prepare/expiry/cancel error text.
        metadata: JSON-safe operator metadata.
    """

    task_id: str
    attempt: int = 1
    status: PreparedSandboxStatus
    runner_id: str
    depth: PreparedSandboxDepth = PreparedSandboxDepth.FULL_INIT
    provider: str = ""
    template: str = ""
    sandbox_id: str = ""
    device_id: str = ""
    lease_id: str = ""
    reservation_token: str = ""
    reserved_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    prepare_started_at: float = 0.0
    prepared_at: float = 0.0
    assigned_at: float = 0.0
    released_at: float = 0.0
    prepare_seconds: float = 0.0
    error: str = ""
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, int]:
        """Return the natural task-attempt key.

        Returns:
            ``(task_id, attempt)`` tuple used by manager caches.
        """

        return self.task_id, self.attempt
