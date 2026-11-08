"""Authority checks for controller command dispatch."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.state import SessionState


@dataclass(frozen=True, slots=True)
class CommandClaimOutcome:
    """Atomic authority-check and idempotency-claim result."""

    claim: Literal["new", "cached", "in_flight"]
    cached: CommandResult | None
    validation_exec_ms: float
    claim_exec_ms: float


def validate_active_lease(store: SQLiteControllerStore, request: CommandRequest) -> None:
    """Reject commands that no longer belong to an active lease/session.

    Args:
        store: Controller persistence store containing sessions and leases.
        request: Command request whose session, lease, device, and generation
            must match the active controller record.

    Raises:
        PermissionError: If the command is stale, targets the wrong device, or
            references an inactive session/lease.
    """

    session_row = store.session_row(request.session_id)
    if session_row is None:
        raise PermissionError("command session does not exist")
    if str(session_row["lease_id"] or "") != request.lease_id:
        raise PermissionError("command lease does not match the active session lease")
    if str(session_row["status"]) not in {SessionState.LEASED.value, SessionState.RUNNING.value}:
        raise PermissionError("command session is not active")
    lease_row = store.lease_row(request.lease_id)
    if lease_row is None:
        raise PermissionError("command lease does not exist")
    if str(lease_row["device_id"]) != request.device_id:
        raise PermissionError("command device does not own the lease")
    if int(lease_row["generation"]) != request.lease_generation:
        raise PermissionError("command lease_generation is stale")
    if str(lease_row["status"]) != SessionState.LEASED.value:
        raise PermissionError("command lease is not active")


def validate_and_claim_active_command(
    store: SQLiteControllerStore,
    request: CommandRequest,
) -> CommandClaimOutcome:
    """Atomically validate command authority and claim its idempotency key."""

    store.connection.execute("BEGIN IMMEDIATE")
    try:
        validation_started_at = time.perf_counter()
        validate_active_lease(store, request)
        validation_finished_at = time.perf_counter()
        claim, cached = store.claim_command_in_transaction(request)
        claim_finished_at = time.perf_counter()
        store.connection.commit()
    except BaseException:
        store.connection.rollback()
        raise
    return CommandClaimOutcome(
        claim=claim,
        cached=cached,
        validation_exec_ms=round((validation_finished_at - validation_started_at) * 1000.0, 3),
        claim_exec_ms=round((claim_finished_at - validation_finished_at) * 1000.0, 3),
    )


__all__ = ["CommandClaimOutcome", "validate_active_lease", "validate_and_claim_active_command"]
