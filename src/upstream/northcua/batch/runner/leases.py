"""Batch-scoped sandbox lease coordination."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.batch.runner.events import BatchEventWriter
from northcua.sandboxes.contracts import (
    SandboxCommandSpec,
    SandboxFailureReason,
    SandboxLease,
    SandboxLifecycleEvent,
    SandboxLifecycleMessage,
    SandboxProviderSpec,
)
from northcua.sandboxes.device.readiness import DeviceReadinessProbe
from northcua.sandboxes.lifecycle.task_lease import SandboxTaskLeaseManager
from northcua.sandboxes.provider.registry import SandboxProviderRegistry
from northcua.telemetry.contracts import CasePhase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchTaskLeaseContext:
    """Provider lease plus the manager that owns its cleanup path.

    Attributes:
        lease: Sandbox lease returned by the lifecycle manager.
        manager: Per-provider manager that must release the lease.
    """

    lease: SandboxLease
    manager: SandboxTaskLeaseManager


class BatchTaskLeaseCoordinator:
    """Acquire, bind, and release task-scoped sandbox leases."""

    def __init__(
        self,
        *,
        provider_registry: SandboxProviderRegistry,
        binding_factory: SandboxDeviceBindingFactory,
        readiness_probe: DeviceReadinessProbe | None = None,
    ) -> None:
        self._providers = provider_registry
        self._binding_factory = binding_factory
        self._readiness_probe = readiness_probe

    @property
    def binding_factory(self) -> SandboxDeviceBindingFactory:
        """Return the device binding factory used by this coordinator.

        Returns:
            Factory used to derive stable device ids for task sandboxes.
        """

        return self._binding_factory

    async def acquire(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        sandbox_spec: SandboxProviderSpec,
        controller_url: str,
        token: str,
        events: BatchEventWriter,
        output_dir: Path,
        attempt: int,
        create_semaphore: asyncio.Semaphore | None = None,
        startup_commands_override: list[SandboxCommandSpec] | None = None,
        sandbox_phase_override: CasePhase | None = None,
        bootstrap_log_dir_override: Path | None = None,
        device_id_override: str = "",
        lifecycle_event_observer: Callable[[SandboxLifecycleEvent], None] | None = None,
    ) -> BatchTaskLeaseContext:
        """Create a sandbox, start a device worker, and wait for readiness.

        Args:
            config: Active batch runtime configuration.
            task: Manifest task that owns the sandbox lease.
            sandbox_spec: Provider allocation spec for the lease.
            controller_url: Controller websocket URL passed to the worker.
            token: Device/submitter token passed to the worker.
            events: Writer for sandbox lifecycle events.
            output_dir: Concrete batch output directory for collected logs.
            attempt: One-based attempt number used in log paths.
            create_semaphore: Optional semaphore guarding only provider create/connect.
            startup_commands_override: Optional fully resolved startup command
                list. Prepared queue uses this for fixture-only depth.
            sandbox_phase_override: Optional telemetry phase override for
                background prepared-sandbox acquisition.
            bootstrap_log_dir_override: Optional log directory for bootstrap
                failures outside the normal claim-time attempt path.
            device_id_override: Optional concrete device id. Prepared sandbox
                queue uses this to isolate timed-out background workers from
                normal fallback workers for the same task attempt.
            lifecycle_event_observer: Optional in-process observer used to
                aggregate phase timing without writing additional event rows.

        Returns:
            Lease context pairing the lease with the manager that created it.
        """

        provider = self._providers.create(sandbox_spec.provider)
        sandbox_spec = sandbox_spec.model_copy(
            update={
                "metadata": {
                    **sandbox_spec.metadata,
                    "northcua": "true",
                    "batch_id": config.manifest.batch_id,
                    "task_id": task.task_id,
                    "agent_kind": task.agent_kind,
                    "template": sandbox_spec.template,
                    "attempt": str(attempt),
                    "create_token": uuid.uuid4().hex,
                    **(
                        {"local_batch_output_dir": str(output_dir.resolve())}
                        if sandbox_spec.provider == "local-process"
                        else {}
                    ),
                }
            }
        )

        def _event_sink(event: SandboxLifecycleEvent) -> None:
            if lifecycle_event_observer is not None:
                try:
                    lifecycle_event_observer(event)
                except Exception:
                    logger.debug(
                        "sandbox lifecycle observer failed",
                        extra={"batch_id": config.manifest.batch_id, "task_id": task.task_id},
                        exc_info=True,
                    )
            events.sandbox_event(
                event,
                phase_override=(
                    sandbox_phase_override
                    if event.message
                    in {
                        SandboxLifecycleMessage.SANDBOX_ACQUIRE_STARTED.value,
                        SandboxLifecycleMessage.SANDBOX_ACQUIRED.value,
                        SandboxLifecycleMessage.DEVICE_WORKER_STARTED.value,
                        SandboxLifecycleMessage.DEVICE_READINESS_WAIT_STARTED.value,
                        SandboxLifecycleMessage.DEVICE_READINESS_WAIT_FINISHED.value,
                        SandboxLifecycleMessage.DEVICE_READY.value,
                    }
                    else None
                ),
            )

        manager = SandboxTaskLeaseManager(
            provider,
            event_sink=_event_sink,
            readiness_probe=self._readiness_probe,
        )
        device_id = device_id_override or self._binding_factory.device_id_for_task(
            config.manifest.batch_id,
            task,
            sandbox_spec,
            attempt=attempt,
        )
        logger.info(
            "batch task sandbox device binding prepared",
            extra={
                "batch_id": config.manifest.batch_id,
                "task_id": task.task_id,
                "attempt": attempt,
                "device_id": device_id,
                "sandbox_template": sandbox_spec.template,
            },
        )
        binding = self._binding_factory.for_task(
            config,
            task,
            sandbox_spec=sandbox_spec,
            device_id=device_id,
            controller_url=controller_url,
            token=token,
        )
        try:
            lease = await manager.acquire(
                batch_id=config.manifest.batch_id,
                task_id=task.task_id,
                spec=sandbox_spec,
                binding=binding,
                startup_commands=(
                    startup_commands_override
                    if startup_commands_override is not None
                    else [*config.manifest.defaults.startup_commands, *task.startup_commands]
                ),
                bootstrap_log_dir=bootstrap_log_dir_override
                or output_dir / "sandbox-logs" / task.task_id / f"attempt-{attempt}",
                readiness_timeout_seconds=config.device_readiness_timeout_seconds,
                create_semaphore=create_semaphore,
            )
        except BaseException:
            await asyncio.shield(manager.aclose())
            raise
        return BatchTaskLeaseContext(lease=lease, manager=manager)

    async def release(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        state: BatchTaskState,
        context: BatchTaskLeaseContext,
        output_dir: Path,
        attempt: int,
        events: BatchEventWriter | None = None,
        log_dir_override: Path | None = None,
        sandbox_phase_override: CasePhase | None = None,
    ) -> None:
        """Collect sandbox logs and release a task-scoped lease.

        Args:
            config: Active batch runtime configuration.
            task: Manifest task that owns the lease.
            state: Latest task state used to categorize release failures.
            context: Lease and manager returned by ``acquire``.
            output_dir: Concrete batch output directory for collected logs.
            attempt: One-based attempt number used in log paths.
            events: Optional event writer used when overriding release phase.
            log_dir_override: Optional cleanup log path for discarded prepared
                leases so they do not overwrite the real attempt logs.
            sandbox_phase_override: Optional telemetry phase for discarded
                prepared-sandbox release lifecycle events.
        """

        defaults = config.manifest.defaults
        failure_reason = (
            SandboxFailureReason.CONTROLLER_TASK_FAILED
            if state.status not in {BatchTaskStatus.SUCCEEDED, BatchTaskStatus.CANCELED}
            else None
        )
        try:
            await context.manager.release(
                context.lease,
                batch_id=config.manifest.batch_id,
                task_id=task.task_id,
                strategy=(task.sandbox or defaults.sandbox).allocation_strategy,
                teardown_commands=[*defaults.teardown_commands, *task.teardown_commands],
                log_dir=log_dir_override or output_dir / "sandbox-logs" / task.task_id / f"attempt-{attempt}",
                failure_reason=failure_reason,
                event_sink_override=(
                    (lambda event: events.sandbox_event(event, phase_override=sandbox_phase_override))
                    if sandbox_phase_override is not None and events is not None
                    else None
                ),
            )
        finally:
            await asyncio.shield(context.manager.aclose())
