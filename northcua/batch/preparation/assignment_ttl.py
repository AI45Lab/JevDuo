"""TTL guard helpers for prepared sandbox assignment."""

from __future__ import annotations

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.contracts import PreparedSandboxPolicy


def task_by_id(config: BatchRunConfig, task_id: str) -> BatchTaskSpec | None:
    """Return a manifest task by id.

    Args:
        config: Batch run configuration that owns the manifest.
        task_id: Manifest task id to resolve.

    Returns:
        Matching task spec, or ``None`` when the manifest no longer contains
        the id.
    """

    return next((task for task in config.manifest.tasks if task.task_id == task_id), None)


def task_timeout_seconds(config: BatchRunConfig, task: BatchTaskSpec) -> float:
    """Return the effective timeout budget for one task.

    Args:
        config: Batch run configuration carrying manifest defaults.
        task: Task whose timeout should be resolved.

    Returns:
        Non-negative task timeout in seconds.
    """

    timeout_seconds = task.timeout_seconds or config.manifest.defaults.timeout_seconds
    return max(float(timeout_seconds or 0.0), 0.0)


def assignment_ttl_guard_seconds(
    *,
    config: BatchRunConfig,
    policy: PreparedSandboxPolicy,
    task: BatchTaskSpec,
) -> float:
    """Return the minimum provider TTL required before assignment.

    Args:
        config: Batch run configuration carrying task defaults.
        policy: Prepared sandbox policy with explicit TTL margin.
        task: Task being assigned to a prepared sandbox.

    Returns:
        Required provider TTL in seconds. This is at least the configured
        policy minimum and, for timed tasks, task timeout plus assignment margin.
    """

    timeout_seconds = task_timeout_seconds(config, task)
    derived_guard = policy.min_assignment_ttl_seconds
    if timeout_seconds > 0:
        derived_guard = max(derived_guard, timeout_seconds + policy.assignment_ttl_margin_seconds)
    return derived_guard


def estimated_provider_remaining_ttl_seconds(
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    assignment: PreparedSandboxAssignment,
    now: float,
) -> float:
    """Estimate provider-side TTL remaining for an assignment.

    Args:
        config: Batch run configuration with sandbox defaults.
        task: Task that will use the prepared sandbox.
        assignment: Prepared sandbox assignment candidate.
        now: Current wall-clock timestamp in seconds.

    Returns:
        Estimated provider TTL remaining in seconds. When no provider timeout
        is known, this falls back to the persisted prepared record TTL.
    """

    provider_timeout_seconds = _provider_timeout_seconds_for_assignment(
        config=config,
        task=task,
        assignment=assignment,
    )
    if provider_timeout_seconds <= 0:
        return assignment.remaining_ttl_seconds(now=now)
    anchor = assignment.record.prepare_started_at or assignment.record.reserved_at
    if anchor <= 0:
        anchor = assignment.record.prepared_at
    if anchor <= 0:
        return assignment.remaining_ttl_seconds(now=now)
    return anchor + provider_timeout_seconds - now


def prepared_assignment_ttl_details(
    assignment: PreparedSandboxAssignment,
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    now: float,
    guard_seconds: float,
    task_timeout_seconds_value: float,
) -> dict[str, object]:
    """Build prepared assignment TTL telemetry.

    Args:
        assignment: Prepared sandbox assignment being considered.
        config: Batch run configuration with sandbox defaults.
        task: Task that will use the prepared sandbox.
        now: Current wall-clock timestamp in seconds.
        guard_seconds: Minimum required provider TTL for assignment.
        task_timeout_seconds_value: Effective task timeout already resolved by
            the caller.

    Returns:
        JSON-safe details describing persisted TTL, estimated provider TTL, and
        whether the assignment guard passed.
    """

    record = assignment.record
    expired_before_assignment = bool(record.expires_at > 0 and record.expires_at <= (record.assigned_at or now))
    provider_timeout_seconds = _provider_timeout_seconds_for_assignment(
        config=config,
        task=task,
        assignment=assignment,
    )
    estimated_provider_remaining_ttl = estimated_provider_remaining_ttl_seconds(
        config=config,
        task=task,
        assignment=assignment,
        now=now,
    )
    return {
        "prepared_sandbox_ttl_seconds": _round_seconds(assignment.prepared_ttl_seconds()),
        "provider_timeout_seconds": _round_seconds(provider_timeout_seconds),
        "estimated_provider_remaining_ttl_seconds_at_assignment": _round_seconds(estimated_provider_remaining_ttl),
        "prepared_required_provider_timeout_seconds": _metadata_float(
            record.metadata,
            "prepared_required_provider_timeout_seconds",
        ),
        "prepared_original_provider_timeout_seconds": _metadata_float(
            record.metadata,
            "prepared_original_provider_timeout_seconds",
        ),
        "prepared_sandbox_reserved_at": record.reserved_at,
        "prepared_sandbox_prepared_at": record.prepared_at,
        "prepared_sandbox_assigned_at": record.assigned_at,
        "prepared_sandbox_expires_at": record.expires_at,
        "remaining_ttl_seconds_at_assignment": _round_seconds(assignment.remaining_ttl_seconds(now=now)),
        "prepared_age_seconds_at_assignment": _round_seconds(assignment.prepared_age_seconds(now=now)),
        "prepared_to_assigned_seconds": _round_seconds(assignment.prepared_to_assigned_seconds()),
        "expired_before_assignment": expired_before_assignment,
        "expired_after_assignment": assignment.expired_after_assignment(now=now),
        "case_timeout_seconds": _round_seconds(task_timeout_seconds_value),
        "assignment_ttl_guard_seconds": _round_seconds(guard_seconds),
        "assignment_ttl_guard_passed": guard_seconds <= 0 or estimated_provider_remaining_ttl >= guard_seconds,
    }


def _round_seconds(value: float) -> float:
    return round(float(value), 3)


def _metadata_float(metadata: dict[str, object], key: str) -> float | None:
    raw = metadata.get(key)
    if raw is None:
        return None
    if not isinstance(raw, (int, float, str)):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _provider_timeout_seconds_for_assignment(
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    assignment: PreparedSandboxAssignment,
) -> float:
    metadata_timeout_seconds = _metadata_float(assignment.record.metadata, "prepared_provider_timeout_seconds")
    if metadata_timeout_seconds is not None and metadata_timeout_seconds > 0:
        return metadata_timeout_seconds
    sandbox_spec = task.sandbox or config.manifest.defaults.sandbox
    return max(float(sandbox_spec.resources.timeout_seconds or 0.0), 0.0)
