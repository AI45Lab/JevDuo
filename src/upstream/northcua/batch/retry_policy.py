"""Shared retry policy helpers for batch control-plane state."""

from __future__ import annotations

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.reliability import FailureAction
from northcua.telemetry.contracts import FailureCategory

_INFRA_RETRYABLE_FAILURES = {
    FailureCategory.SANDBOX_CREATE,
    FailureCategory.UPLOAD,
    FailureCategory.STARTUP_COMMAND,
    FailureCategory.DEVICE_BOOTSTRAP,
    FailureCategory.DEVICE_COMMAND_TIMEOUT,
    FailureCategory.DEVICE_TRANSPORT,
    FailureCategory.READINESS_TIMEOUT,
    FailureCategory.CONTROLLER_SUBMIT,
    FailureCategory.TIMEOUT,
}


def is_infra_retryable_failure(category: FailureCategory | str | None) -> bool:
    """Return whether a terminal-looking failure should still consume retry budget.

    Args:
        category: Structured or serialized failure category from task state.

    Returns:
        ``True`` for transient device connectivity failures that may carry
        non-retryable terminal metadata from the interrupted agent.
    """

    if category is None:
        return False
    try:
        normalized = category if isinstance(category, FailureCategory) else FailureCategory(str(category))
    except ValueError:
        return False
    return normalized in _INFRA_RETRYABLE_FAILURES


def terminal_decision_blocks_retry(
    *,
    status: BatchTaskStatus | str,
    terminal_decision: object,
    last_failure_category: FailureCategory | str | None,
    recovery_action: FailureAction | str | None = None,
) -> bool:
    """Return whether persisted task state explicitly forbids a retry.

    Args:
        status: Latest local batch task status.
        terminal_decision: Structured controller terminal decision payload.
        last_failure_category: Latest normalized failure category, if known.
        recovery_action: Persisted recovery action chosen by resume/control plane.

    Returns:
        ``True`` only when business-level terminal metadata should stop whole
        case retries. Transient device failures are allowed to consume the
        configured retry budget.
    """

    status_value = status.value if isinstance(status, BatchTaskStatus) else str(status)
    recovery_value = recovery_action.value if isinstance(recovery_action, FailureAction) else recovery_action
    if is_infra_retryable_failure(last_failure_category):
        return False
    if status_value == BatchTaskStatus.EVALUATION_PENDING.value:
        return True
    if recovery_value == FailureAction.FAIL_FAST.value:
        return True
    if status_value != BatchTaskStatus.FAILED.value:
        return False
    return isinstance(terminal_decision, dict) and terminal_decision.get("retryable") is False


def task_state_blocks_retry(state: BatchTaskState) -> bool:
    """Return whether a task state should stop whole-case retries.

    Args:
        state: Persisted task state from runner, resume, or SQLite.

    Returns:
        ``True`` when the shared retry policy says scheduling should stop.
    """

    return terminal_decision_blocks_retry(
        status=state.status,
        terminal_decision=state.terminal_decision,
        last_failure_category=state.last_failure_category,
        recovery_action=state.recovery_action,
    )
