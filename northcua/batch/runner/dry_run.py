"""Dry-run planning helper for batch attempts."""

from __future__ import annotations

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.planner import BatchTaskPlanner
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.batch.runner.events import BatchEventWriter
from northcua.sandboxes.contracts import SandboxProviderSpec


def run_dry_attempt(
    *,
    binding_factory: SandboxDeviceBindingFactory,
    planner: BatchTaskPlanner,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    sandbox_spec: SandboxProviderSpec,
    attempt: int,
    events: BatchEventWriter,
) -> BatchTaskState:
    """Plan a dry-run attempt without creating a sandbox.

    Args:
        binding_factory: Device binding helper used to derive deterministic ids.
        planner: Batch task planner that validates controller projection.
        config: Active batch runtime configuration.
        task: Manifest task being dry-run.
        sandbox_spec: Provider spec used to derive target metadata.
        attempt: One-based attempt count.
        events: Batch event writer used to persist the dry terminal state.

    Returns:
        Successful dry-run task state.
    """

    manifest = config.manifest
    device_id = binding_factory.device_id_for_task(manifest.batch_id, task, sandbox_spec, attempt=attempt)
    planner.plan(
        manifest,
        task,
        device_id=device_id,
        sandbox_id="dry-run",
        attempt=attempt,
    )
    state = BatchTaskState(
        task_id=task.task_id,
        attempts=attempt,
        device_id=device_id,
        status=BatchTaskStatus.SUCCEEDED,
        recovery_action=events.lease.state.recovery_action if events.lease is not None else None,
        retry_budget_remaining=events.lease.state.retry_budget_remaining if events.lease is not None else 0,
    )
    events.upsert_task(state)
    return state
