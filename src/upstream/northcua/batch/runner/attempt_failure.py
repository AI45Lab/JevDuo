"""Failure classification helpers for batch attempts."""

from __future__ import annotations

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.batch.runner.events import BatchEventWriter
from northcua.reliability.contracts import FailureAction
from northcua.telemetry.contracts import FailureCategory
from northcua.telemetry.failure_categories import failure_category_from_message


def _failure_category_for_attempt(
    *,
    state: BatchTaskState,
    status: BatchTaskStatus,
    error: Exception,
) -> FailureCategory:
    explicit_category = getattr(error, "failure_category", None)
    if isinstance(explicit_category, FailureCategory):
        return explicit_category
    if isinstance(explicit_category, str):
        try:
            return FailureCategory(explicit_category)
        except ValueError:
            pass
    message = str(error).lower()
    if status == BatchTaskStatus.TIMED_OUT:
        return FailureCategory.TIMEOUT
    if "readiness" in message or "did not become ready" in message or "not ready" in message:
        return FailureCategory.READINESS_TIMEOUT
    message_category = failure_category_from_message(message)
    if message_category is not None:
        return message_category
    if state.status == BatchTaskStatus.STARTING_SANDBOX:
        return FailureCategory.SANDBOX_CREATE
    if state.status == BatchTaskStatus.BOOTSTRAPPING_DEVICE or "bootstrap" in message:
        return FailureCategory.DEVICE_BOOTSTRAP
    if state.status == BatchTaskStatus.WAITING_DEVICE or "ready" in message:
        return FailureCategory.READINESS_TIMEOUT
    if state.controller_task_id or state.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
        return FailureCategory.MAIN_AGENT
    return FailureCategory.UNKNOWN


def _recovery_action_from_claim(events: BatchEventWriter) -> FailureAction | None:
    if events.lease is None:
        return None
    return events.lease.state.recovery_action


def _retry_budget_from_claim(events: BatchEventWriter) -> int:
    if events.lease is None:
        return 0
    return events.lease.state.retry_budget_remaining
