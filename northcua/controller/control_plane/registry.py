"""Device registry for the controller control plane."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call
from northcua.controller.storage.device_heartbeat_batcher import DeviceHeartbeatBatcher
from northcua.controller.storage.identifiers import utc_now_iso
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.capabilities import Capability, DeviceDescriptor, ToolDescriptor
from northcua_protocol.payloads import DeviceHeartbeat
from northcua_protocol.state import DeviceState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RegisteredDevice:
    """In-memory device state.

    Attributes:
        descriptor: Latest descriptor advertised by the device worker.
        state: Controller-visible lifecycle state.
        active_sessions: Number of sessions currently reserved on the device.
        active_commands: Number of commands currently in flight.
        load: Device-reported load metric used as a scheduler tiebreaker.
        last_heartbeat_at: Timestamp of the most recent heartbeat.
        runtime_health: Device runtime health payload from heartbeat.
        capability_health: Per-capability health booleans from heartbeat.
        first_heartbeat_observed: Whether first-heartbeat stage telemetry has been emitted.
        capability_ready_observed: Whether first all-healthy capability stage telemetry has been emitted.
    """

    descriptor: DeviceDescriptor
    state: DeviceState
    active_sessions: int = 0
    active_commands: int = 0
    load: float = 0.0
    last_heartbeat_at: datetime | None = None
    runtime_health: dict[str, object] = field(default_factory=dict)
    capability_health: dict[Capability, bool] = field(default_factory=dict)
    first_heartbeat_observed: bool = False
    capability_ready_observed: bool = False


class DeviceRegistry:
    """Track controller-visible devices and their latest capabilities.

    The registry is the controller's in-memory view of device availability.
    SQLite remains the inspection store, while this object feeds scheduling
    and backpressure decisions during the single-process v1 controller run.
    """

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        events: ControllerEventLog,
        heartbeat_persist_interval_seconds: float = 15.0,
    ) -> None:
        self.store = store
        self.events = events
        self.heartbeat_persist_interval_seconds = max(0.0, heartbeat_persist_interval_seconds)
        self._devices: dict[str, RegisteredDevice] = {}
        self._gui_resource_owner_by_id: dict[str, str] = {}
        self._last_heartbeat_persist_at: dict[str, datetime] = {}
        self._retry_after_capacity_release_device_ids: set[str] = set()
        self._heartbeat_batcher = DeviceHeartbeatBatcher(self.store.path)

    def register(self, descriptor: DeviceDescriptor, *, trace_id: str) -> RegisteredDevice:
        """Register or replace a device descriptor.

        Args:
            descriptor: Device descriptor from DEVICE_HELLO.
            trace_id: Trace id used for controller event correlation.

        Returns:
            In-memory registered device state.
        """

        previous = self._devices.get(descriptor.device_id)
        now = datetime.now(timezone.utc)
        device = RegisteredDevice(
            descriptor=descriptor,
            state=DeviceState.ONLINE,
            active_sessions=previous.active_sessions if previous is not None else 0,
            active_commands=previous.active_commands if previous is not None else 0,
            last_heartbeat_at=now,
            first_heartbeat_observed=previous.first_heartbeat_observed if previous is not None else False,
            capability_ready_observed=previous.capability_ready_observed if previous is not None else False,
        )
        self._devices[descriptor.device_id] = device
        self._last_heartbeat_persist_at[descriptor.device_id] = now
        commit_started = time.perf_counter()
        self.store.upsert_device(descriptor, status=DeviceState.ONLINE)
        db_commit_ms = _elapsed_ms(commit_started)
        self.events.write("device_registered", trace_id=trace_id, entity_type="device", entity_id=descriptor.device_id)
        self.record_readiness_stage(
            trace_id=trace_id,
            device_id=descriptor.device_id,
            stage="device_hello",
            descriptor_capability_count=len(descriptor.capabilities),
            descriptor_tool_count=len(descriptor.tools),
            db_commit_ms=db_commit_ms,
        )
        return device

    async def register_async(self, descriptor: DeviceDescriptor, *, trace_id: str) -> RegisteredDevice:
        """Register a device descriptor without blocking the event loop.

        Args:
            descriptor: Device descriptor from DEVICE_HELLO.
            trace_id: Trace id used for controller event correlation.

        Returns:
            In-memory registered device state.
        """

        previous = self._devices.get(descriptor.device_id)
        now = datetime.now(timezone.utc)
        device = RegisteredDevice(
            descriptor=descriptor,
            state=DeviceState.ONLINE,
            active_sessions=previous.active_sessions if previous is not None else 0,
            active_commands=previous.active_commands if previous is not None else 0,
            last_heartbeat_at=now,
            first_heartbeat_observed=previous.first_heartbeat_observed if previous is not None else False,
            capability_ready_observed=previous.capability_ready_observed if previous is not None else False,
        )
        self._devices[descriptor.device_id] = device
        self._last_heartbeat_persist_at[descriptor.device_id] = now
        commit_started = time.perf_counter()
        await self._upsert_device_async(
            descriptor,
            status=DeviceState.ONLINE,
            operation_name="device_register",
            priority=ControllerStorePriority.CRITICAL,
        )
        db_commit_ms = _elapsed_ms(commit_started)
        self.events.write("device_registered", trace_id=trace_id, entity_type="device", entity_id=descriptor.device_id)
        self.record_readiness_stage(
            trace_id=trace_id,
            device_id=descriptor.device_id,
            stage="device_hello",
            descriptor_capability_count=len(descriptor.capabilities),
            descriptor_tool_count=len(descriptor.tools),
            db_commit_ms=db_commit_ms,
        )
        return device

    def heartbeat(self, heartbeat: DeviceHeartbeat, *, trace_id: str) -> bool:
        """Record heartbeat load for a device.

        Args:
            heartbeat: Device heartbeat payload.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Whether this heartbeat was sampled into durable storage. The
            controller uses sampled heartbeats as the cadence for heavier
            queued-task retry work; lease release and device registration still
            retry immediately.
        """

        device = self._devices.get(heartbeat.device_id)
        if device is None or device.state != DeviceState.ONLINE:
            return False
        first_heartbeat = not device.first_heartbeat_observed
        device.first_heartbeat_observed = True
        device.last_heartbeat_at = heartbeat.timestamp
        # Heartbeat counters are device-reported telemetry, while the
        # controller may already have reserved a lease that the worker has not
        # observed yet. Keep the safer higher pressure so a stale/low heartbeat
        # cannot overbook a foreground GUI sandbox.
        device.active_sessions = max(device.active_sessions, heartbeat.active_sessions)
        device.active_commands = max(device.active_commands, heartbeat.active_commands)
        device.load = heartbeat.load
        device.runtime_health = dict(heartbeat.runtime_health)
        device.capability_health = dict(heartbeat.capability_health)
        capability_ready = _heartbeat_capabilities_ready(heartbeat)
        first_capability_ready = capability_ready and not device.capability_ready_observed
        if first_capability_ready:
            device.capability_ready_observed = True
        should_retry_queued = heartbeat.device_id in self._retry_after_capacity_release_device_ids
        self._retry_after_capacity_release_device_ids.discard(heartbeat.device_id)
        should_persist = self._should_persist_heartbeat(heartbeat.device_id)
        db_commit_ms = -1
        if not should_persist:
            self._emit_readiness_heartbeat_stages(
                heartbeat,
                trace_id=trace_id,
                first_heartbeat=first_heartbeat,
                first_capability_ready=first_capability_ready,
                heartbeat_persisted=False,
                db_commit_ms=db_commit_ms,
            )
            return should_retry_queued
        commit_started = time.perf_counter()
        persisted = self.store.heartbeat_devices_batch(
            [
                (
                    utc_now_iso(),
                    json.dumps(
                        {
                            "load": heartbeat.load,
                            "active_commands": device.active_commands,
                            "runtime_health": heartbeat.runtime_health,
                            "capability_health": {
                                key.value: value for key, value in heartbeat.capability_health.items()
                            },
                        },
                        ensure_ascii=False,
                    ),
                    heartbeat.device_id,
                )
            ]
        )
        if persisted != 1:
            return False
        db_commit_ms = _elapsed_ms(commit_started)
        self.events.write(
            "device_heartbeat",
            trace_id=trace_id,
            entity_type="device",
            entity_id=heartbeat.device_id,
            active_sessions=device.active_sessions,
            load=heartbeat.load,
            active_commands=device.active_commands,
            sample_interval_seconds=self.heartbeat_persist_interval_seconds,
        )
        self._emit_readiness_heartbeat_stages(
            heartbeat,
            trace_id=trace_id,
            first_heartbeat=first_heartbeat,
            first_capability_ready=first_capability_ready,
            heartbeat_persisted=True,
            db_commit_ms=db_commit_ms,
        )
        return True

    async def heartbeat_async(self, heartbeat: DeviceHeartbeat, *, trace_id: str) -> bool:
        """Record heartbeat load without blocking websocket ingress on SQLite.

        Args:
            heartbeat: Device heartbeat payload.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Whether this heartbeat should trigger queued-task retry work.
        """

        device = self._devices.get(heartbeat.device_id)
        if device is None or device.state != DeviceState.ONLINE:
            return False
        first_heartbeat = not device.first_heartbeat_observed
        device.first_heartbeat_observed = True
        device.last_heartbeat_at = heartbeat.timestamp
        device.active_sessions = max(device.active_sessions, heartbeat.active_sessions)
        device.active_commands = max(device.active_commands, heartbeat.active_commands)
        device.load = heartbeat.load
        device.runtime_health = dict(heartbeat.runtime_health)
        device.capability_health = dict(heartbeat.capability_health)
        capability_ready = _heartbeat_capabilities_ready(heartbeat)
        first_capability_ready = capability_ready and not device.capability_ready_observed
        if first_capability_ready:
            device.capability_ready_observed = True
        should_retry_queued = heartbeat.device_id in self._retry_after_capacity_release_device_ids
        self._retry_after_capacity_release_device_ids.discard(heartbeat.device_id)
        should_persist = self._should_persist_heartbeat(heartbeat.device_id)
        db_commit_ms = -1
        if should_persist:
            load_json = json.dumps(
                {
                    "load": heartbeat.load,
                    "active_commands": device.active_commands,
                    "runtime_health": heartbeat.runtime_health,
                    "capability_health": {key.value: value for key, value in heartbeat.capability_health.items()},
                },
                ensure_ascii=False,
            )
            self._heartbeat_batcher.publish(
                device_id=heartbeat.device_id,
                last_heartbeat_at=utc_now_iso(),
                load_json=load_json,
            )
            self.events.write(
                "device_heartbeat",
                trace_id=trace_id,
                entity_type="device",
                entity_id=heartbeat.device_id,
                active_sessions=device.active_sessions,
                load=heartbeat.load,
                active_commands=device.active_commands,
                sample_interval_seconds=self.heartbeat_persist_interval_seconds,
                persistence="queued",
            )
        self._emit_readiness_heartbeat_stages(
            heartbeat,
            trace_id=trace_id,
            first_heartbeat=first_heartbeat,
            first_capability_ready=first_capability_ready,
            heartbeat_persisted=False,
            heartbeat_persistence_queued=should_persist,
            db_commit_ms=db_commit_ms,
        )
        return should_retry_queued

    def heartbeat_persistence_snapshot(self) -> dict[str, int]:
        """Return coalesced heartbeat persistence health counters."""

        return self._heartbeat_batcher.snapshot()

    async def aclose(self) -> None:
        """Flush and close background device-heartbeat persistence."""

        await self._heartbeat_batcher.aclose()

    def _emit_readiness_heartbeat_stages(
        self,
        heartbeat: DeviceHeartbeat,
        *,
        trace_id: str,
        first_heartbeat: bool,
        first_capability_ready: bool,
        heartbeat_persisted: bool,
        db_commit_ms: int,
        heartbeat_persistence_queued: bool = False,
    ) -> None:
        if first_heartbeat:
            self.record_readiness_stage(
                trace_id=trace_id,
                device_id=heartbeat.device_id,
                stage="first_heartbeat",
                heartbeat_persisted=heartbeat_persisted,
                heartbeat_persistence_queued=heartbeat_persistence_queued,
                heartbeat_db_commit_ms=db_commit_ms,
                active_sessions=heartbeat.active_sessions,
                active_commands=heartbeat.active_commands,
                load=heartbeat.load,
                runtime_healthy=bool(heartbeat.runtime_health.get("healthy", True)),
                capability_health_count=len(heartbeat.capability_health),
                unhealthy_capability_count=_unhealthy_capability_count(heartbeat),
            )
        if first_capability_ready:
            self.record_readiness_stage(
                trace_id=trace_id,
                device_id=heartbeat.device_id,
                stage="capability_ready",
                heartbeat_persisted=heartbeat_persisted,
                heartbeat_persistence_queued=heartbeat_persistence_queued,
                heartbeat_db_commit_ms=db_commit_ms,
                capability_health_count=len(heartbeat.capability_health),
                runtime_healthy=bool(heartbeat.runtime_health.get("healthy", True)),
            )

    def record_readiness_stage(self, *, trace_id: str, device_id: str, stage: str, **payload: object) -> None:
        """Persist one best-effort device readiness stage event.

        Args:
            trace_id: Trace id used for controller event correlation.
            device_id: Device id that emitted the readiness stage.
            stage: Readiness stage name.
            **payload: JSON-like stage timing and diagnostic details.
        """

        try:
            self.events.write(
                "device_readiness_stage",
                trace_id=trace_id,
                entity_type="device",
                entity_id=device_id,
                stage=stage,
                device_id=device_id,
                **payload,
            )
        except Exception:
            logger.warning(
                "failed to write device readiness stage telemetry",
                extra={"device_id": device_id, "stage": stage},
                exc_info=True,
            )

    def _should_persist_heartbeat(self, device_id: str) -> bool:
        if self.heartbeat_persist_interval_seconds <= 0:
            self._last_heartbeat_persist_at[device_id] = datetime.now(timezone.utc)
            return True
        now = datetime.now(timezone.utc)
        last_persisted_at = self._last_heartbeat_persist_at.get(device_id)
        if last_persisted_at is None:
            self._last_heartbeat_persist_at[device_id] = now
            return True
        if now - last_persisted_at < timedelta(seconds=self.heartbeat_persist_interval_seconds):
            return False
        self._last_heartbeat_persist_at[device_id] = now
        return True

    def mark_draining(self, device_id: str, *, trace_id: str, reason: str = "") -> None:
        """Mark a registered device as draining.

        Args:
            device_id: Device to drain.
            trace_id: Trace id used for controller event correlation.
            reason: Operator or lifecycle reason for draining.
        """

        device = self._devices.get(device_id)
        if device is None:
            self.store.update_device_status(device_id, DeviceState.DRAINING)
            self.events.write(
                "device_draining",
                trace_id=trace_id,
                entity_type="device",
                entity_id=device_id,
                reason=reason,
                live_connection=False,
            )
            return
        device.state = DeviceState.DRAINING
        self.store.upsert_device(device.descriptor, status=DeviceState.DRAINING, active_sessions=device.active_sessions)
        self.events.write(
            "device_draining", trace_id=trace_id, entity_type="device", entity_id=device_id, reason=reason
        )

    async def mark_draining_async(self, device_id: str, *, trace_id: str, reason: str = "") -> None:
        """Mark a device as draining without blocking the event loop.

        Args:
            device_id: Device to drain.
            trace_id: Trace id used for controller event correlation.
            reason: Operator or lifecycle reason for draining.
        """

        device = self._devices.get(device_id)
        if device is None:
            await controller_store_call(
                self.store.path,
                lambda store: store.update_device_status(device_id, DeviceState.DRAINING),
                operation_name="device_mark_draining",
                priority=ControllerStorePriority.CRITICAL,
            )
            self.events.write(
                "device_draining",
                trace_id=trace_id,
                entity_type="device",
                entity_id=device_id,
                reason=reason,
                live_connection=False,
            )
            return
        device.state = DeviceState.DRAINING
        await self._upsert_device_async(
            device.descriptor,
            status=DeviceState.DRAINING,
            active_sessions=device.active_sessions,
            operation_name="device_mark_draining",
            priority=ControllerStorePriority.CRITICAL,
        )
        self.events.write(
            "device_draining", trace_id=trace_id, entity_type="device", entity_id=device_id, reason=reason
        )

    def available(self) -> list[RegisteredDevice]:
        """Return online devices that can accept work.

        Returns:
            Registered devices currently marked online.
        """

        return [device for device in self._devices.values() if device.state == DeviceState.ONLINE]

    def get(self, device_id: str) -> RegisteredDevice | None:
        """Return one registered device.

        Args:
            device_id: Device id to look up.

        Returns:
            Registered device state, or `None` when unknown.
        """

        return self._devices.get(device_id)

    def mark_offline(self, device_id: str, *, trace_id: str, reason: str = "") -> None:
        """Mark a device offline after disconnect or health failure.

        Args:
            device_id: Device to mark offline.
            trace_id: Trace id used for controller event correlation.
            reason: Disconnect or health reason stored in the event log.
        """

        device = self._devices.get(device_id)
        if device is None:
            return
        device.state = DeviceState.OFFLINE
        if device.descriptor.gui_resource_id:
            current_owner = self._gui_resource_owner_by_id.get(device.descriptor.gui_resource_id)
            if current_owner == device_id:
                self._gui_resource_owner_by_id.pop(device.descriptor.gui_resource_id, None)
        self.store.upsert_device(device.descriptor, status=DeviceState.OFFLINE, active_sessions=device.active_sessions)
        self.events.write("device_offline", trace_id=trace_id, entity_type="device", entity_id=device_id, reason=reason)

    async def mark_offline_async(self, device_id: str, *, trace_id: str, reason: str = "") -> None:
        """Mark a device offline without blocking the event loop on SQLite.

        Args:
            device_id: Device to mark offline.
            trace_id: Trace id used for controller event correlation.
            reason: Disconnect or health reason stored in the event log.
        """

        device = self._devices.get(device_id)
        if device is None:
            return
        device.state = DeviceState.OFFLINE
        if device.descriptor.gui_resource_id:
            current_owner = self._gui_resource_owner_by_id.get(device.descriptor.gui_resource_id)
            if current_owner == device_id:
                self._gui_resource_owner_by_id.pop(device.descriptor.gui_resource_id, None)
        await self._upsert_device_async(
            device.descriptor,
            status=DeviceState.OFFLINE,
            active_sessions=device.active_sessions,
            operation_name="device_mark_offline",
            priority=ControllerStorePriority.CRITICAL,
        )
        self.events.write("device_offline", trace_id=trace_id, entity_type="device", entity_id=device_id, reason=reason)

    def reserve_session(self, device_id: str) -> None:
        """Increment controller-side active session pressure.

        Args:
            device_id: Device receiving a newly granted lease.
        """

        device = self._devices[device_id]
        device.active_sessions += 1
        if device.descriptor.gui_resource_id:
            self._gui_resource_owner_by_id[device.descriptor.gui_resource_id] = device_id
        self.store.upsert_device(device.descriptor, status=device.state, active_sessions=device.active_sessions)

    async def reserve_session_async(self, device_id: str) -> None:
        """Increment controller-side session pressure without blocking SQLite.

        Args:
            device_id: Device receiving a newly granted lease.
        """

        device = self._devices[device_id]
        device.active_sessions += 1
        if device.descriptor.gui_resource_id:
            self._gui_resource_owner_by_id[device.descriptor.gui_resource_id] = device_id
        await self._upsert_device_async(
            device.descriptor,
            status=device.state,
            active_sessions=device.active_sessions,
            operation_name="device_reserve_session",
            priority=ControllerStorePriority.CRITICAL,
        )

    def release_session(self, device_id: str) -> None:
        """Decrement controller-side active session pressure.

        Args:
            device_id: Device whose lease/session pressure should decrease.
        """

        device = self._devices.get(device_id)
        if device is None:
            return
        device.active_sessions = max(0, device.active_sessions - 1)
        self._retry_after_capacity_release_device_ids.add(device_id)
        self.store.upsert_device(device.descriptor, status=device.state, active_sessions=device.active_sessions)

    async def release_session_async(self, device_id: str) -> None:
        """Decrement controller-side session pressure without blocking SQLite.

        Args:
            device_id: Device whose lease/session pressure should decrease.
        """

        device = self._devices.get(device_id)
        if device is None:
            return
        device.active_sessions = max(0, device.active_sessions - 1)
        self._retry_after_capacity_release_device_ids.add(device_id)
        await self._upsert_device_async(
            device.descriptor,
            status=device.state,
            active_sessions=device.active_sessions,
            operation_name="device_release_session",
            priority=ControllerStorePriority.CRITICAL,
        )

    def reserve_command(self, device_id: str) -> None:
        """Increment controller-side active command pressure.

        Args:
            device_id: Device receiving a dispatched command.
        """

        device = self._devices.get(device_id)
        if device is None:
            return
        device.active_commands += 1

    def release_command(self, device_id: str) -> None:
        """Decrement controller-side active command pressure.

        Args:
            device_id: Device whose in-flight command pressure should decrease.
        """

        device = self._devices.get(device_id)
        if device is None:
            return
        device.active_commands = max(0, device.active_commands - 1)
        self._retry_after_capacity_release_device_ids.add(device_id)

    def gui_resource_owner(self, resource_id: str) -> RegisteredDevice | None:
        """Return the preferred device process for one shared GUI resource.

        Args:
            resource_id: GUI resource id shared by one physical desktop or sandbox display.

        Returns:
            Online owner device, or ``None`` when no owner has been established.
        """

        owner_id = self._gui_resource_owner_by_id.get(resource_id)
        if not owner_id:
            return None
        owner = self._devices.get(owner_id)
        if owner is None or owner.state != DeviceState.ONLINE:
            self._gui_resource_owner_by_id.pop(resource_id, None)
            return None
        return owner

    def tool_descriptor(self, device_id: str, tool_id: str) -> ToolDescriptor | None:
        """Return the descriptor for one device tool.

        Args:
            device_id: Device id that advertised the tool surface.
            tool_id: Tool id requested by a command.

        Returns:
            Matching tool descriptor, or `None` when the tool is unknown.
        """

        device = self._devices.get(device_id)
        if device is None:
            return None
        return next((tool for tool in device.descriptor.tools if tool.tool_id == tool_id), None)

    async def _upsert_device_async(
        self,
        descriptor: DeviceDescriptor,
        *,
        status: DeviceState,
        active_sessions: int = 0,
        load_json: str = "{}",
        operation_name: str = "device_upsert",
        priority: ControllerStorePriority = ControllerStorePriority.NORMAL,
    ) -> None:
        await controller_store_call(
            self.store.path,
            lambda store: store.upsert_device(
                descriptor,
                status=status,
                active_sessions=active_sessions,
                load_json=load_json,
            ),
            operation_name=operation_name,
            priority=priority,
        )


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _heartbeat_capabilities_ready(heartbeat: DeviceHeartbeat) -> bool:
    if not bool(heartbeat.runtime_health.get("healthy", True)):
        return False
    return bool(heartbeat.capability_health) and all(bool(value) for value in heartbeat.capability_health.values())


def _unhealthy_capability_count(heartbeat: DeviceHeartbeat) -> int:
    return sum(1 for value in heartbeat.capability_health.values() if not bool(value))
