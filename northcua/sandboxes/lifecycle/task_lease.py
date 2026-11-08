"""Task-scoped sandbox lease orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from northcua.sandboxes.contracts import (
    DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS,
    SandboxAllocationStrategy,
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxDeviceBinding,
    SandboxFailureReason,
    SandboxFileUploadSpec,
    SandboxHandle,
    SandboxLease,
    SandboxLeaseState,
    SandboxLifecycleEvent,
    SandboxLifecycleMessage,
    SandboxLifecycleState,
    SandboxProviderFeature,
    SandboxProviderSpec,
    SandboxReadinessState,
)
from northcua.sandboxes.device.bootstrap import SandboxDeviceBootstrapper
from northcua.sandboxes.device.readiness import DeviceReadinessProbe, NoopDeviceReadinessProbe, SandboxReadinessResult
from northcua.sandboxes.lifecycle.errors import SandboxAcquireCleanupError
from northcua.sandboxes.lifecycle.manager import SandboxLifecycleManager, _sandbox_command_failure_message
from northcua.sandboxes.provider.base import SandboxProvider
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher

EventSink = Callable[[SandboxLifecycleEvent], None]
logger = logging.getLogger(__name__)


class SandboxTaskLeaseManager:
    """Acquire provider capacity, bootstrap a worker, and release logs/cleanup."""

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        event_sink: EventSink | None = None,
        readiness_probe: DeviceReadinessProbe | None = None,
    ) -> None:
        self._provider = provider
        self._provider_dispatcher = SandboxProviderDispatcher(provider)
        self._lifecycle = SandboxLifecycleManager(provider)
        self._bootstrapper = SandboxDeviceBootstrapper(provider)
        self._event_sink = event_sink
        self._readiness_probe = readiness_probe or NoopDeviceReadinessProbe()

    async def aclose(self) -> None:
        """Close optional provider-owned background work."""

        close = getattr(self._provider, "aclose", None)
        if callable(close):
            await cast(Callable[[], Awaitable[None]], close)()

    async def acquire(
        self,
        *,
        batch_id: str,
        task_id: str,
        spec: SandboxProviderSpec,
        binding: SandboxDeviceBinding,
        startup_commands: list[SandboxCommandSpec],
        start_worker: bool = True,
        readiness_timeout_seconds: float = DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS,
        bootstrap_log_dir: Path | None = None,
        reusable: bool = False,
        create_semaphore: asyncio.Semaphore | None = None,
        on_acquired: Callable[[SandboxLease], None] | None = None,
    ) -> SandboxLease:
        """Acquire a sandbox lease and optionally start the device worker.

        Args:
            batch_id: Batch namespace used in lifecycle events.
            task_id: Manifest task or warm-pool slot currently acquiring capacity.
            spec: Provider allocation request.
            binding: Device worker bootstrap binding for the sandbox.
            startup_commands: Commands that prepare the sandbox before worker launch.
            start_worker: Whether to bootstrap a NorthCUA device worker.
            readiness_timeout_seconds: Maximum time to wait for worker readiness.
            bootstrap_log_dir: Optional host directory for worker logs when
                bootstrap fails before a normal batch release path exists.
            reusable: Whether the resulting lease may return to a warm pool.
            create_semaphore: Optional semaphore guarding only provider create/connect.

        Returns:
            Lease that owns the provider handle and device id.

        Raises:
            RuntimeError: If worker startup or readiness probing fails.
        """

        self._emit(
            batch_id,
            task_id,
            "",
            spec.provider,
            SandboxLifecycleState.CREATED,
            SandboxLifecycleMessage.SANDBOX_ACQUIRE_STARTED,
        )
        stage_sink = self._stage_sink(batch_id=batch_id, task_id=task_id, provider=spec.provider)
        lease: SandboxLease | None = None

        def record_handle(handle: SandboxHandle) -> None:
            nonlocal lease
            lease = SandboxLease(
                lease_id=uuid.uuid4().hex,
                handle=handle,
                device_id=binding.device_id,
                reusable=reusable,
                readiness=SandboxReadinessState.UNKNOWN,
            )
            if on_acquired is not None:
                on_acquired(lease)

        handle = await self._lifecycle.acquire(
            spec,
            startup_commands=startup_commands,
            create_semaphore=create_semaphore,
            stage_sink=stage_sink,
            on_handle_acquired=record_handle,
        )
        self._emit(
            batch_id,
            task_id,
            handle.sandbox_id,
            spec.provider,
            handle.state,
            SandboxLifecycleMessage.SANDBOX_ACQUIRED,
        )
        if lease is None or lease.handle != handle:
            raise RuntimeError("sandbox lifecycle did not publish the acquired handle")
        if start_worker:
            lease = await self._bootstrap_worker_for_lease(
                lease=lease,
                handle=handle,
                spec=spec,
                binding=binding,
                batch_id=batch_id,
                task_id=task_id,
                readiness_timeout_seconds=readiness_timeout_seconds,
                bootstrap_log_dir=bootstrap_log_dir,
                stage_sink=stage_sink,
            )
        if on_acquired is not None:
            on_acquired(lease)
        return lease

    async def reconnect(
        self,
        lease: SandboxLease,
        *,
        spec: SandboxProviderSpec,
    ) -> SandboxLease:
        """Reconnect a durably recorded lease without rerunning bootstrap commands."""

        if lease.handle.provider != spec.provider or lease.handle.template != spec.template:
            raise ValueError("durable sandbox lease differs from its provider specification")
        if not self._provider.capabilities().supports(SandboxProviderFeature.CONNECT_EXISTING):
            raise RuntimeError(f"sandbox provider {spec.provider!r} cannot reconnect durable leases")
        reconnect_spec = spec.model_copy(
            update={
                "allocation_strategy": SandboxAllocationStrategy.CONNECT_EXISTING,
                "sandbox_id": lease.handle.sandbox_id,
                "file_uploads": [],
            }
        )
        handle = await self._lifecycle.acquire(reconnect_spec, startup_commands=[])
        if (
            handle.sandbox_id,
            handle.provider,
            handle.template,
        ) != (
            lease.handle.sandbox_id,
            lease.handle.provider,
            lease.handle.template,
        ):
            raise RuntimeError("sandbox provider reconnected a different durable lease")
        return lease.model_copy(update={"handle": handle})

    async def _bootstrap_worker_for_lease(
        self,
        *,
        lease: SandboxLease,
        handle: SandboxHandle,
        spec: SandboxProviderSpec,
        binding: SandboxDeviceBinding,
        batch_id: str,
        task_id: str,
        readiness_timeout_seconds: float,
        bootstrap_log_dir: Path | None,
        stage_sink: Callable[[str, dict[str, object]], None],
    ) -> SandboxLease:
        try:
            lease = await self._start_worker_and_wait_ready(
                lease=lease,
                handle=handle,
                spec=spec,
                binding=binding,
                batch_id=batch_id,
                task_id=task_id,
                readiness_timeout_seconds=readiness_timeout_seconds,
                stage_sink=stage_sink,
            )
        except asyncio.CancelledError:
            await self._cleanup_failed_bootstrap(
                lease=lease,
                handle=handle,
                spec=spec,
                batch_id=batch_id,
                task_id=task_id,
                bootstrap_log_dir=bootstrap_log_dir,
                canceled=True,
            )
            raise
        except Exception:
            logger.debug("sandbox worker bootstrap flow failed before cleanup", exc_info=True)
            await self._cleanup_failed_bootstrap(
                lease=lease,
                handle=handle,
                spec=spec,
                batch_id=batch_id,
                task_id=task_id,
                bootstrap_log_dir=bootstrap_log_dir,
                canceled=False,
            )
            raise
        return lease

    async def _start_worker_and_wait_ready(
        self,
        *,
        lease: SandboxLease,
        handle: SandboxHandle,
        spec: SandboxProviderSpec,
        binding: SandboxDeviceBinding,
        batch_id: str,
        task_id: str,
        readiness_timeout_seconds: float,
        stage_sink: Callable[[str, dict[str, object]], None],
    ) -> SandboxLease:
        worker_started = time.monotonic()
        try:
            await self._bootstrapper.start_worker(handle, binding)
        except Exception as exc:
            logger.debug("sandbox device worker bootstrap failed", exc_info=True)
            stage_sink(
                "device_worker_start",
                {
                    "stage": "device_worker_start",
                    "status": "failed",
                    "duration_ms": max(0, int((time.monotonic() - worker_started) * 1000)),
                    "sandbox_id": handle.sandbox_id,
                    "provider": handle.provider,
                    "device_id": binding.device_id,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                },
            )
            raise
        worker_start_seconds = time.monotonic() - worker_started
        lease = lease.model_copy(update={"readiness": SandboxReadinessState.WAITING})
        self._emit(
            batch_id,
            task_id,
            handle.sandbox_id,
            spec.provider,
            SandboxLifecycleState.READY,
            SandboxLifecycleMessage.DEVICE_WORKER_STARTED,
            {"device_id": binding.device_id, "worker_start_seconds": round(worker_start_seconds, 3)},
        )
        stage_sink(
            "device_worker_start",
            {
                "stage": "device_worker_start",
                "status": "succeeded",
                "duration_ms": max(0, int(worker_start_seconds * 1000)),
                "sandbox_id": handle.sandbox_id,
                "provider": handle.provider,
                "device_id": binding.device_id,
            },
        )
        return await self._wait_for_device_readiness(
            lease=lease,
            handle=handle,
            spec=spec,
            binding=binding,
            batch_id=batch_id,
            task_id=task_id,
            readiness_timeout_seconds=readiness_timeout_seconds,
            stage_sink=stage_sink,
        )

    async def _wait_for_device_readiness(
        self,
        *,
        lease: SandboxLease,
        handle: SandboxHandle,
        spec: SandboxProviderSpec,
        binding: SandboxDeviceBinding,
        batch_id: str,
        task_id: str,
        readiness_timeout_seconds: float,
        stage_sink: Callable[[str, dict[str, object]], None],
    ) -> SandboxLease:
        readiness_started = time.monotonic()
        self._emit(
            batch_id,
            task_id,
            handle.sandbox_id,
            spec.provider,
            SandboxLifecycleState.READY,
            SandboxLifecycleMessage.DEVICE_READINESS_WAIT_STARTED,
            {"device_id": binding.device_id, "timeout_seconds": readiness_timeout_seconds},
        )
        try:
            readiness = await self._readiness_probe.wait_for_ready(
                binding.device_id,
                timeout_seconds=readiness_timeout_seconds,
            )
        except Exception as exc:
            logger.debug("sandbox device readiness wait failed", exc_info=True)
            self._emit_readiness_stage(
                stage_sink,
                handle=handle,
                binding=binding,
                readiness_seconds=time.monotonic() - readiness_started,
                timeout_seconds=readiness_timeout_seconds,
                readiness="failed",
                status="failed",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            raise
        readiness_seconds = time.monotonic() - readiness_started
        self._emit(
            batch_id,
            task_id,
            handle.sandbox_id,
            spec.provider,
            SandboxLifecycleState.READY,
            SandboxLifecycleMessage.DEVICE_READINESS_WAIT_FINISHED,
            {
                "device_id": binding.device_id,
                "timeout_seconds": readiness_timeout_seconds,
                "elapsed_seconds": round(readiness_seconds, 3),
                "readiness": readiness.value,
                "probe_count": getattr(self._readiness_probe, "last_probe_count", 0),
                "last_probe_error": getattr(self._readiness_probe, "last_error", ""),
                "last_probe_status": getattr(self._readiness_probe, "last_status", ""),
            },
        )
        self._emit_readiness_stage(
            stage_sink,
            handle=handle,
            binding=binding,
            readiness_seconds=readiness_seconds,
            timeout_seconds=readiness_timeout_seconds,
            readiness=readiness.value,
            status="succeeded" if readiness == SandboxReadinessState.READY else "failed",
        )
        lease = lease.model_copy(update={"readiness": readiness})
        if readiness != SandboxReadinessState.READY:
            raise RuntimeError(f"sandbox device did not become ready: {readiness.value}")
        self._emit(
            batch_id,
            task_id,
            handle.sandbox_id,
            spec.provider,
            SandboxLifecycleState.READY,
            SandboxLifecycleMessage.DEVICE_READY,
        )
        return lease

    def _emit_readiness_stage(
        self,
        stage_sink: Callable[[str, dict[str, object]], None],
        *,
        handle: SandboxHandle,
        binding: SandboxDeviceBinding,
        readiness_seconds: float,
        timeout_seconds: float,
        readiness: str,
        status: str,
        error: str = "",
    ) -> None:
        payload = {
            "stage": "device_readiness_wait",
            "status": status,
            "duration_ms": max(0, int(readiness_seconds * 1000)),
            "sandbox_id": handle.sandbox_id,
            "provider": handle.provider,
            "device_id": binding.device_id,
            "timeout_seconds": timeout_seconds,
            "readiness": readiness,
            "probe_count": getattr(self._readiness_probe, "last_probe_count", 0),
            "last_probe_error": getattr(self._readiness_probe, "last_error", ""),
            "last_probe_status": getattr(self._readiness_probe, "last_status", ""),
        }
        if error:
            payload["error"] = error
        stage_sink("device_readiness_wait", payload)

    async def _cleanup_failed_bootstrap(
        self,
        *,
        lease: SandboxLease,
        handle: SandboxHandle,
        spec: SandboxProviderSpec,
        batch_id: str,
        task_id: str,
        bootstrap_log_dir: Path | None,
        canceled: bool,
    ) -> None:
        logger.warning(
            "sandbox device bootstrap canceled" if canceled else "sandbox device bootstrap failed",
            extra={"sandbox_id": handle.sandbox_id, "device_id": lease.device_id},
            exc_info=True,
        )
        try:
            release_coro = self.release(
                lease,
                batch_id=batch_id,
                task_id=task_id,
                strategy=spec.allocation_strategy,
                log_dir=bootstrap_log_dir,
                failure_reason=SandboxFailureReason.DEVICE_BOOTSTRAP_FAILED,
            )
            if canceled:
                await asyncio.shield(release_coro)
            else:
                await release_coro
        except Exception as cleanup_exc:
            raise SandboxAcquireCleanupError(
                sandbox_id=handle.sandbox_id,
                provider=handle.provider,
                message="sandbox device bootstrap canceled" if canceled else "sandbox device bootstrap failed",
                cleanup_error=cleanup_exc,
            ) from cleanup_exc

    async def release(
        self,
        lease: SandboxLease,
        *,
        batch_id: str,
        task_id: str,
        strategy: SandboxAllocationStrategy,
        teardown_commands: list[SandboxCommandSpec] | None = None,
        log_dir: Path | None = None,
        failure_reason: SandboxFailureReason | None = None,
        event_sink_override: EventSink | None = None,
    ) -> SandboxLease:
        """Collect logs and release provider capacity for a lease.

        Args:
            lease: Lease to drain and release.
            batch_id: Batch namespace used in lifecycle events.
            task_id: Manifest task id associated with the release.
            strategy: Allocation strategy that determines terminate vs detach behavior.
            teardown_commands: Best-effort commands run before release.
            log_dir: Optional host directory for collected sandbox logs.
            failure_reason: Optional reason stored on failed leases.
            event_sink_override: Optional sink for the terminal lifecycle event.

        Returns:
            Updated lease carrying the final release state.
        """

        updated = lease.model_copy(
            update={
                "state": SandboxLeaseState.DRAINING,
                "failure_reason": failure_reason,
            }
        )
        if log_dir is not None:
            try:
                log_refs = await self._provider_dispatcher.collect_logs(lease.handle, log_dir)
                logger.info(
                    "sandbox logs collected",
                    extra={
                        "batch_id": batch_id,
                        "task_id": task_id,
                        "sandbox_id": lease.handle.sandbox_id,
                        "log_dir": str(log_dir),
                        "log_count": len(log_refs),
                        "log_bytes": sum(ref.size_bytes for ref in log_refs),
                        "failure_reason": failure_reason.value if failure_reason else "",
                    },
                )
                if failure_reason is not None and not log_refs:
                    logger.warning(
                        "no sandbox logs collected for failed lease",
                        extra={
                            "batch_id": batch_id,
                            "task_id": task_id,
                            "sandbox_id": lease.handle.sandbox_id,
                            "log_dir": str(log_dir),
                            "failure_reason": failure_reason.value,
                        },
                    )
            except Exception:
                logger.warning(
                    "failed to collect sandbox logs",
                    extra={
                        "batch_id": batch_id,
                        "task_id": task_id,
                        "sandbox_id": lease.handle.sandbox_id,
                        "log_dir": str(log_dir),
                        "failure_reason": failure_reason.value if failure_reason else "",
                    },
                    exc_info=True,
                )
        await self._lifecycle.release(lease.handle, strategy=strategy, teardown_commands=teardown_commands or [])
        final_state = SandboxLeaseState.FAILED if failure_reason else SandboxLeaseState.RELEASED
        updated = updated.model_copy(update={"state": final_state})
        self._emit(
            batch_id,
            task_id,
            lease.handle.sandbox_id,
            lease.handle.provider,
            SandboxLifecycleState.TERMINATED,
            SandboxLifecycleMessage.SANDBOX_RELEASED,
            {"failure_reason": failure_reason.value if failure_reason else ""},
            event_sink=event_sink_override,
        )
        return updated

    async def run_startup_commands(
        self,
        lease: SandboxLease,
        commands: list[SandboxCommandSpec],
    ) -> None:
        """Run additional startup commands against an already-ready lease.

        Args:
            lease: Lease whose sandbox should receive the commands.
            commands: Startup/init commands deferred until task assignment.

        Raises:
            RuntimeError: If a required command fails.
        """

        for command in commands:
            result = await self._provider_dispatcher.run_command(lease.handle, command)
            if not result.ok and not command.allow_failure:
                raise RuntimeError(
                    _sandbox_command_failure_message("sandbox deferred startup command failed", command, result)
                )

    async def run_command(
        self,
        lease: SandboxLease,
        command: SandboxCommandSpec,
    ) -> SandboxCommandResult:
        """Run a foreground command against an already-acquired lease.

        Args:
            lease: Lease whose sandbox should receive the command.
            command: Command to execute inside the sandbox.

        Returns:
            Provider-normalized command result.
        """

        return await self._provider_dispatcher.run_command(lease.handle, command)

    def supports_timeout_extension(self) -> bool:
        """Return whether this lease manager can extend provider-side lifetime.

        Returns:
            True when the configured sandbox provider advertises timeout
            extension support for acquired leases.
        """

        return self._provider_dispatcher.capabilities().supports(SandboxProviderFeature.EXTEND_TIMEOUT)

    async def extend_timeout(self, lease: SandboxLease, *, timeout_seconds: int) -> None:
        """Extend provider-side lifetime for an already-acquired lease.

        Args:
            lease: Lease whose sandbox provider timeout should be extended.
            timeout_seconds: New provider timeout measured from the extension
                call, using provider-specific semantics.
        """

        await self._provider_dispatcher.extend_timeout(lease.handle, timeout_seconds)

    async def download_file(self, lease: SandboxLease, *, remote: str, local: Path) -> None:
        """Download one file from an already-acquired lease.

        Args:
            lease: Lease whose sandbox owns the file.
            remote: Absolute sandbox path to download.
            local: Local destination path.
        """

        local.parent.mkdir(parents=True, exist_ok=True)
        await self._provider_dispatcher.download(lease.handle, remote, local)

    async def stage_evaluator_files(
        self,
        lease: SandboxLease,
        uploads: list[SandboxFileUploadSpec],
    ) -> None:
        """Upload evaluator-only files after the main agent has terminated."""

        if any(upload.visibility != "evaluator" for upload in uploads):
            raise ValueError("post-run staging accepts only evaluator-visible files")
        await self._lifecycle.upload_files(lease.handle, uploads)

    async def wait_for_ready(
        self,
        device_id: str,
        *,
        timeout_seconds: float,
        batch_id: str = "",
        task_id: str = "",
        sandbox_id: str = "",
        provider: str = "",
        reason: str = "",
    ) -> SandboxReadinessState:
        """Re-probe whether a device id is still ready.

        Args:
            device_id: Controller device id to check.
            timeout_seconds: Maximum time to wait.
            batch_id: Optional batch namespace for lifecycle telemetry.
            task_id: Optional task id for lifecycle telemetry.
            sandbox_id: Optional provider sandbox id for lifecycle telemetry.
            provider: Optional sandbox provider for lifecycle telemetry.
            reason: Optional caller reason, such as prepared claim or deferred startup.

        Returns:
            Readiness state reported by the configured probe.
        """

        emit_events = bool(batch_id and task_id)
        if emit_events:
            self._emit(
                batch_id,
                task_id,
                sandbox_id,
                provider,
                SandboxLifecycleState.READY,
                SandboxLifecycleMessage.DEVICE_READINESS_WAIT_STARTED,
                {
                    "device_id": device_id,
                    "timeout_seconds": timeout_seconds,
                    "reason": reason,
                    "reprobe": True,
                },
            )
        started = time.monotonic()
        readiness = await self._readiness_probe.wait_for_ready(device_id, timeout_seconds=timeout_seconds)
        if emit_events:
            self._emit(
                batch_id,
                task_id,
                sandbox_id,
                provider,
                SandboxLifecycleState.READY,
                SandboxLifecycleMessage.DEVICE_READINESS_WAIT_FINISHED,
                {
                    "device_id": device_id,
                    "timeout_seconds": timeout_seconds,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "readiness": readiness.value,
                    "probe_count": getattr(self._readiness_probe, "last_probe_count", 0),
                    "last_probe_error": getattr(self._readiness_probe, "last_error", ""),
                    "last_probe_status": getattr(self._readiness_probe, "last_status", ""),
                    "reason": reason,
                    "reprobe": True,
                },
            )
        if isinstance(readiness, SandboxReadinessResult):
            return readiness.state
        return readiness

    def _stage_sink(self, *, batch_id: str, task_id: str, provider: str) -> Callable[[str, dict[str, object]], None]:
        def _emit_stage(stage: str, metadata: dict[str, object]) -> None:
            try:
                sandbox_id = str(metadata.get("sandbox_id") or "")
                state = (
                    SandboxLifecycleState.FAILED if metadata.get("status") == "failed" else SandboxLifecycleState.READY
                )
                self._emit(
                    batch_id,
                    task_id,
                    sandbox_id,
                    str(metadata.get("provider") or provider),
                    state,
                    "sandbox_stage_timing",
                    metadata,
                )
            except Exception:
                logger.warning(
                    "failed to write sandbox stage telemetry",
                    extra={"batch_id": batch_id, "task_id": task_id, "stage": stage},
                    exc_info=True,
                )

        return _emit_stage

    def _emit(
        self,
        batch_id: str,
        task_id: str,
        sandbox_id: str,
        provider: str,
        state: SandboxLifecycleState,
        message: SandboxLifecycleMessage | str,
        metadata: dict[str, object] | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        sink = event_sink or self._event_sink
        if sink is None:
            return
        sink(
            SandboxLifecycleEvent(
                batch_id=batch_id,
                task_id=task_id,
                sandbox_id=sandbox_id,
                provider=provider,
                state=state,
                message=message.value if isinstance(message, SandboxLifecycleMessage) else message,
                metadata=metadata or {},
            )
        )
