"""Shared infra retry backoff helpers for batch runners."""

from __future__ import annotations

import hashlib
import time

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState
from northcua.telemetry.contracts import CasePhase, FailureCategory

INFRA_BACKOFF_CATEGORIES = {
    FailureCategory.CONTROLLER_SUBMIT,
    FailureCategory.READINESS_TIMEOUT,
    FailureCategory.DEVICE_TRANSPORT,
    FailureCategory.DEVICE_COMMAND_TIMEOUT,
    FailureCategory.SANDBOX_CREATE,
    FailureCategory.DEVICE_BOOTSTRAP,
    FailureCategory.TIMEOUT,
}


def with_infra_retry_backoff(
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    state: BatchTaskState,
    max_retries: int,
) -> BatchTaskState:
    """Delay retryable infra failures so bursts cool down before re-claim.

    Args:
        config: Batch run configuration containing retry cooldown policy.
        task: Manifest task whose retry is being scheduled.
        state: Latest task state.
        max_retries: Maximum configured task retry count.

    Returns:
        Updated task state with ``next_attempt_after`` when backoff applies.
    """

    if not config.infra_retry_backoff_seconds:
        return state
    if state.last_failure_category not in INFRA_BACKOFF_CATEGORIES:
        return state
    if state.retry_budget_remaining <= 0 or state.attempts >= max_retries + 1:
        return state
    base_delay = backoff_delay_seconds(config.infra_retry_backoff_seconds, attempt=state.attempts)
    jitter = stable_retry_jitter_seconds(task.task_id, state.attempts, base_delay)
    return state.model_copy(update={"next_attempt_after": time.time() + base_delay + jitter})


def backoff_delay_seconds(backoffs: list[float], *, attempt: int) -> float:
    """Return the configured backoff for a one-based attempt number.

    Args:
        backoffs: Ordered delay schedule in seconds.
        attempt: One-based task attempt number.

    Returns:
        Sanitized delay for the attempt, or the last configured delay.
    """

    sanitized = [max(float(item), 0.0) for item in backoffs if float(item) >= 0.0]
    if not sanitized:
        return 0.0
    index = min(max(attempt - 1, 0), len(sanitized) - 1)
    return sanitized[index]


def stable_retry_jitter_seconds(task_id: str, attempt: int, base_delay: float) -> float:
    """Return deterministic jitter so retryable failures do not herd.

    Args:
        task_id: Stable manifest task id.
        attempt: One-based task attempt number.
        base_delay: Base retry delay in seconds.

    Returns:
        Deterministic jitter in seconds.
    """

    if base_delay <= 0:
        return 0.0
    digest = hashlib.sha256(f"{task_id}:{attempt}:infra-retry-backoff".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return fraction * min(base_delay * 0.2, 30.0)


def phase_for_backoff(failure_category: FailureCategory | None) -> CasePhase:
    """Map infra failure categories to the phase where cooldown was scheduled.

    Args:
        failure_category: Last failure category recorded on a task.

    Returns:
        Case phase most closely associated with the retry cooldown.
    """

    if failure_category == FailureCategory.CONTROLLER_SUBMIT:
        return CasePhase.CONTROLLER_SUBMIT
    if failure_category == FailureCategory.READINESS_TIMEOUT:
        return CasePhase.WORKER_READY
    if failure_category in {FailureCategory.SANDBOX_CREATE, FailureCategory.DEVICE_BOOTSTRAP}:
        return CasePhase.SANDBOX_CREATE
    return CasePhase.MAIN_AGENT
