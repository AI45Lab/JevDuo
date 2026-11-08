"""Sticky session lease management."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from northcua.controller.control_plane.scheduler import Scheduler
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.payloads import SessionLeaseGranted, SessionLeaseReleased, TaskRequest
from northcua_protocol.state import SessionState

logger = logging.getLogger(__name__)


class LeaseManager:
    """Manage session-to-device sticky leases."""

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        scheduler: Scheduler,
        events: ControllerEventLog,
        timing_observer: Callable[[str, float], None] | None = None,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self.events = events
        self.timing_observer = timing_observer

    def acquire_for_task(
        self,
        request: TaskRequest,
        *,
        session_id: str,
        preferred_device_id: str = "",
        create_task: bool = True,
    ) -> SessionLeaseGranted:
        """Acquire a sticky lease for a task/session.

        Args:
            request: Accepted task request that needs a device lease.
            session_id: Controller session id associated with the task.
            preferred_device_id: Connection-affinity device preference.
            create_task: Whether to persist the task row before leasing.

        Returns:
            Lease payload that can be delivered to the selected device.

        Raises:
            RuntimeError: If no matching device has capacity.
        """

        total_started_at = time.perf_counter()
        select_started_at = time.perf_counter()
        device = self.scheduler.select_device(
            request.required_capabilities,
            preferred_device_id=preferred_device_id,
            target_device_id=request.target_device_id,
        )
        self._observe_elapsed("lease.select_device", select_started_at)
        lease_id = uuid.uuid4().hex
        if create_task:
            create_task_started_at = time.perf_counter()
            self.store.create_task(request, session_id=session_id, submitter_id="controller-internal")
            self._observe_elapsed("lease.create_task", create_task_started_at)
        create_lease_started_at = time.perf_counter()
        generation = self.store.create_lease_if_schedulable(
            lease_id=lease_id,
            session_id=session_id,
            task_id=request.task_id,
            device_id=device.descriptor.device_id,
            timing_observer=self.timing_observer,
        )
        self._observe_elapsed("lease.create_lease", create_lease_started_at)
        reserve_started_at = time.perf_counter()
        self.scheduler.registry.reserve_session(device.descriptor.device_id)
        self._observe_elapsed("lease.reserve_session", reserve_started_at)
        lease = SessionLeaseGranted(
            lease_id=lease_id,
            session_id=session_id,
            task_id=request.task_id,
            device_id=device.descriptor.device_id,
            trace_id=request.trace_id,
            lease_generation=generation,
            capabilities=request.required_capabilities,
            metadata=dict(request.metadata),
        )
        self.events.write(
            "lease_granted",
            trace_id=request.trace_id,
            entity_type="session",
            entity_id=session_id,
            lease_id=lease_id,
            device_id=device.descriptor.device_id,
        )
        self._observe_elapsed("lease.acquire_total", total_started_at)
        return lease

    async def acquire_for_task_async(
        self,
        request: TaskRequest,
        *,
        session_id: str,
        preferred_device_id: str = "",
        create_task: bool = True,
    ) -> SessionLeaseGranted:
        """Acquire a lease while moving SQLite writes off the event loop.

        Device selection and in-memory capacity reservation stay on the event
        loop so concurrent submitters cannot over-select the same device while
        the SQLite lease row is being created in the background thread.

        Args:
            request: Accepted task request that needs a device lease.
            session_id: Controller session id associated with the task.
            preferred_device_id: Connection-affinity device preference.
            create_task: Whether to persist the task row before leasing.

        Returns:
            Lease payload that can be delivered to the selected device.

        Raises:
            RuntimeError: If no matching device has capacity.
        """

        total_started_at = time.perf_counter()
        select_started_at = time.perf_counter()
        device = self.scheduler.select_device(
            request.required_capabilities,
            preferred_device_id=preferred_device_id,
            target_device_id=request.target_device_id,
        )
        self._observe_elapsed("lease.select_device", select_started_at)
        reserve_started_at = time.perf_counter()
        reserve_task = asyncio.create_task(self.scheduler.registry.reserve_session_async(device.descriptor.device_id))
        try:
            await asyncio.shield(reserve_task)
        except asyncio.CancelledError:
            try:
                await reserve_task
                await self.scheduler.registry.release_session_async(device.descriptor.device_id)
            except Exception:
                logger.warning(
                    "controller failed to reconcile cancelled lease reservation",
                    extra={"task_id": request.task_id, "device_id": device.descriptor.device_id},
                    exc_info=True,
                )
            raise
        self._observe_elapsed("lease.reserve_session", reserve_started_at)
        lease_id = uuid.uuid4().hex

        db_task = asyncio.create_task(
            controller_store_call(
                self.store.path,
                lambda store: _persist_lease(
                    store,
                    request=request,
                    session_id=session_id,
                    lease_id=lease_id,
                    device_id=device.descriptor.device_id,
                    create_task=create_task,
                    timing_observer=self.timing_observer,
                ),
                operation_name="lease_create",
                priority=ControllerStorePriority.CRITICAL,
            )
        )
        try:
            create_lease_started_at = time.perf_counter()
            generation = await asyncio.shield(db_task)
            self._observe_elapsed("lease.create_lease", create_lease_started_at)
        except asyncio.CancelledError:
            try:
                await db_task
                await self._rollback_persisted_lease_after_cancel(
                    lease_id=lease_id,
                    session_id=session_id,
                    task_id=request.task_id,
                    device_id=device.descriptor.device_id,
                    trace_id=request.trace_id,
                )
            except Exception:
                logger.warning(
                    "controller failed to reconcile cancelled lease acquire",
                    extra={
                        "task_id": request.task_id,
                        "session_id": session_id,
                        "device_id": device.descriptor.device_id,
                    },
                    exc_info=True,
                )
            raise
        except Exception:
            logger.warning(
                "controller failed to persist async lease; rolling back reservation",
                extra={
                    "task_id": request.task_id,
                    "session_id": session_id,
                    "device_id": device.descriptor.device_id,
                },
                exc_info=True,
            )
            await self.scheduler.registry.release_session_async(device.descriptor.device_id)
            raise

        lease = SessionLeaseGranted(
            lease_id=lease_id,
            session_id=session_id,
            task_id=request.task_id,
            device_id=device.descriptor.device_id,
            trace_id=request.trace_id,
            lease_generation=generation,
            capabilities=request.required_capabilities,
            metadata=dict(request.metadata),
        )
        self.events.write(
            "lease_granted",
            trace_id=request.trace_id,
            entity_type="session",
            entity_id=session_id,
            lease_id=lease_id,
            device_id=device.descriptor.device_id,
        )
        self._observe_elapsed("lease.acquire_total", total_started_at)
        return lease

    async def _rollback_persisted_lease_after_cancel(
        self,
        *,
        lease_id: str,
        session_id: str,
        task_id: str,
        device_id: str,
        trace_id: str,
    ) -> None:
        lease = SessionLeaseGranted(
            lease_id=lease_id,
            session_id=session_id,
            task_id=task_id,
            device_id=device_id,
            trace_id=trace_id,
        )
        should_release_registry, _task_requeued = await controller_store_call(
            self.store.path,
            lambda store: _persist_rollback_undelivered(store, lease),
            operation_name="lease_rollback_undelivered",
            priority=ControllerStorePriority.CRITICAL,
        )
        if should_release_registry:
            await self.scheduler.registry.release_session_async(device_id)

    def release(self, release: SessionLeaseReleased, *, trace_id: str) -> None:
        """Release a sticky lease and decrement device pressure.

        Args:
            release: Lease release payload from controller or device lifecycle.
            trace_id: Trace id used for controller event correlation.

        Raises:
            PermissionError: If the release does not match the active
                persisted lease/session owner.
        """

        lease_row = self.store.lease_row(release.lease_id)
        session_row = self.store.session_row(release.session_id)
        if lease_row is None or session_row is None:
            raise PermissionError("lease release references unknown lease or session")
        if str(lease_row["session_id"]) != release.session_id or str(lease_row["device_id"]) != release.device_id:
            raise PermissionError("lease release does not match persisted lease owner")
        if (
            str(session_row["lease_id"] or "") != release.lease_id
            or str(session_row["device_id"] or "") != release.device_id
        ):
            raise PermissionError("lease release does not match active session owner")
        if str(lease_row["status"]) != SessionState.LEASED.value:
            raise PermissionError("lease release references a non-active lease")
        if str(session_row["status"]) not in {SessionState.LEASED.value, SessionState.RUNNING.value}:
            raise PermissionError("lease release references a non-active session")
        self.store.update_lease_status(release.lease_id, SessionState.RELEASED)
        self.store.update_session_status(release.session_id, SessionState.RELEASED)
        self.scheduler.registry.release_session(release.device_id)
        self.events.write(
            "lease_released",
            trace_id=trace_id,
            entity_type="session",
            entity_id=release.session_id,
            lease_id=release.lease_id,
            device_id=release.device_id,
            reason=release.reason,
        )

    async def release_async(self, release: SessionLeaseReleased, *, trace_id: str) -> None:
        """Release a sticky lease without blocking the controller event loop.

        Args:
            release: Lease release payload from controller or device lifecycle.
            trace_id: Trace id used for controller event correlation.

        Raises:
            PermissionError: If the release does not match the active
                persisted lease/session owner.
        """

        release_task = asyncio.create_task(
            controller_store_call(
                self.store.path,
                lambda store: _persist_release(store, release),
                operation_name="lease_release",
                priority=ControllerStorePriority.CRITICAL,
            )
        )
        try:
            should_release_registry = await asyncio.shield(release_task)
        except asyncio.CancelledError:
            should_release_registry = await release_task
            if should_release_registry:
                await self._release_registry_after_persist(release.device_id)
            raise
        if should_release_registry:
            await self._release_registry_after_persist(release.device_id)
        self.events.write(
            "lease_released",
            trace_id=trace_id,
            entity_type="session",
            entity_id=release.session_id,
            lease_id=release.lease_id,
            device_id=release.device_id,
            reason=release.reason,
            idempotent=not should_release_registry,
        )

    async def _release_registry_after_persist(self, device_id: str) -> None:
        release_task = asyncio.create_task(self.scheduler.registry.release_session_async(device_id))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            await release_task
            raise

    async def _rollback_registry_after_persist(self, device_id: str, *, should_release_registry: bool) -> None:
        if not should_release_registry:
            return
        release_task = asyncio.create_task(self.scheduler.registry.release_session_async(device_id))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            await release_task
            raise

    def rollback_undelivered(self, lease: SessionLeaseGranted, *, trace_id: str, reason: str) -> None:
        """Undo a lease that could not be delivered to the device connection.

        Args:
            lease: Lease payload that failed websocket delivery.
            trace_id: Trace id used for controller event correlation.
            reason: Human-readable rollback reason stored in the event log.
        """

        lease_released, task_requeued = self.store.rollback_undelivered_lease_if_active(
            lease_id=lease.lease_id,
            session_id=lease.session_id,
            task_id=lease.task_id,
        )
        if lease_released:
            self.scheduler.registry.release_session(lease.device_id)
        self.events.write(
            "lease_delivery_rolled_back",
            trace_id=trace_id,
            entity_type="session",
            entity_id=lease.session_id,
            lease_id=lease.lease_id,
            device_id=lease.device_id,
            reason=reason,
            task_requeued=task_requeued,
            idempotent=not lease_released,
        )

    async def rollback_undelivered_async(self, lease: SessionLeaseGranted, *, trace_id: str, reason: str) -> bool:
        """Undo an undelivered lease without blocking the event loop.

        Args:
            lease: Lease payload that failed websocket delivery.
            trace_id: Trace id used for controller event correlation.
            reason: Human-readable rollback reason stored in the event log.

        Returns:
            ``True`` when the task/session were restored for retry.

        Raises:
            asyncio.CancelledError: Re-raised after any committed rollback also
                releases the in-memory registry capacity.
        """

        rollback_task = asyncio.create_task(
            controller_store_call(
                self.store.path,
                lambda store: _persist_rollback_undelivered(store, lease),
                operation_name="lease_rollback_undelivered",
                priority=ControllerStorePriority.CRITICAL,
            )
        )
        try:
            should_release_registry, task_requeued = await asyncio.shield(rollback_task)
        except asyncio.CancelledError:
            should_release_registry, task_requeued = await rollback_task
            await self._rollback_registry_after_persist(
                lease.device_id,
                should_release_registry=should_release_registry,
            )
            raise
        await self._rollback_registry_after_persist(
            lease.device_id,
            should_release_registry=should_release_registry,
        )
        self.events.write(
            "lease_delivery_rolled_back",
            trace_id=trace_id,
            entity_type="session",
            entity_id=lease.session_id,
            lease_id=lease.lease_id,
            device_id=lease.device_id,
            reason=reason,
            task_requeued=task_requeued,
            idempotent=not should_release_registry,
        )
        return task_requeued

    def _observe_elapsed(self, operation: str, started_at: float) -> None:
        if self.timing_observer is None:
            return
        self.timing_observer(operation, max(0.0, (time.perf_counter() - started_at) * 1000.0))


def _persist_lease(
    store: SQLiteControllerStore,
    *,
    request: TaskRequest,
    session_id: str,
    lease_id: str,
    device_id: str,
    create_task: bool,
    timing_observer: Callable[[str, float], None] | None,
) -> int:
    if create_task:
        store.create_task(request, session_id=session_id, submitter_id="controller-internal")
    return store.create_lease_if_schedulable(
        lease_id=lease_id,
        session_id=session_id,
        task_id=request.task_id,
        device_id=device_id,
        timing_observer=timing_observer,
    )


def _persist_release(store: SQLiteControllerStore, release: SessionLeaseReleased) -> bool:
    lease_row = store.lease_row(release.lease_id)
    session_row = store.session_row(release.session_id)
    if lease_row is None or session_row is None:
        raise PermissionError("lease release references unknown lease or session")
    if str(lease_row["session_id"]) != release.session_id or str(lease_row["device_id"]) != release.device_id:
        raise PermissionError("lease release does not match persisted lease owner")
    if (
        str(session_row["lease_id"] or "") != release.lease_id
        or str(session_row["device_id"] or "") != release.device_id
    ):
        raise PermissionError("lease release does not match active session owner")
    lease_status = str(lease_row["status"])
    session_status = str(session_row["status"])
    if lease_status == SessionState.RELEASED.value:
        if session_status in {SessionState.LEASED.value, SessionState.RUNNING.value}:
            store.update_session_status(release.session_id, SessionState.RELEASED)
        return False
    if lease_status != SessionState.LEASED.value:
        return False
    if session_status not in {SessionState.LEASED.value, SessionState.RUNNING.value}:
        return False
    store.update_lease_status(release.lease_id, SessionState.RELEASED)
    store.update_session_status(release.session_id, SessionState.RELEASED)
    return True


def _persist_rollback_undelivered(store: SQLiteControllerStore, lease: SessionLeaseGranted) -> tuple[bool, bool]:
    return store.rollback_undelivered_lease_if_active(
        lease_id=lease.lease_id,
        session_id=lease.session_id,
        task_id=lease.task_id,
    )
