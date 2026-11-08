"""Capability-aware device scheduler for controller lease decisions."""

from __future__ import annotations

from northcua.controller.control_plane.registry import DeviceRegistry, RegisteredDevice
from northcua.controller.policy.gui_resource import DEFAULT_GUI_RESOURCE_POLICY, GuiResourcePolicy
from northcua_protocol.capabilities import Capability


class Scheduler:
    """Select an available device for a required capability set."""

    def __init__(
        self, registry: DeviceRegistry, *, gui_policy: GuiResourcePolicy = DEFAULT_GUI_RESOURCE_POLICY
    ) -> None:
        self.registry = registry
        self.gui_policy = gui_policy

    def select_device(
        self,
        required: set[Capability],
        *,
        preferred_device_id: str = "",
        target_device_id: str = "",
    ) -> RegisteredDevice:
        """Return the best device for required capabilities.

        Args:
            required: Capabilities requested by the task.
            preferred_device_id: Connection-affinity preference, if present.
            target_device_id: Strict target device id; never spills to a pool.

        Returns:
            Registered device selected for the lease.

        Raises:
            RuntimeError: If no online matching device has available capacity.
        """

        candidates = [device for device in self.registry.available() if device.descriptor.has_capabilities(required)]
        if not candidates:
            raise RuntimeError(f"No online device satisfies capabilities: {sorted(item.value for item in required)}")
        if target_device_id:
            targeted = [device for device in candidates if device.descriptor.device_id == target_device_id]
            if not targeted:
                raise RuntimeError(f"Target device is not online or lacks capabilities: {target_device_id}")
            if not self._has_capacity(targeted[0], required):
                raise RuntimeError(f"Target device is at capacity: {target_device_id}")
            return targeted[0]
        if preferred_device_id:
            preferred = [device for device in candidates if device.descriptor.device_id == preferred_device_id]
            if preferred and self._has_capacity(preferred[0], required):
                return preferred[0]
        resource_owner = self._select_gui_resource_owner(candidates, required)
        if resource_owner is not None:
            return resource_owner
        candidates.sort(
            key=lambda device: (
                device.active_sessions,
                device.active_commands,
                device.load,
                device.descriptor.device_id,
            )
        )
        for device in candidates:
            if self._has_capacity(device, required):
                return device
        raise RuntimeError("All matching devices are at capacity")

    @staticmethod
    def _has_capacity(device: RegisteredDevice, required: set[Capability]) -> bool:
        if not device.descriptor.supports_parallel_sessions and device.active_sessions >= 1:
            return False
        if device.descriptor.requires_foreground_focus and device.active_sessions >= device.descriptor.max_gui_sessions:
            return False
        if device.active_sessions >= device.descriptor.max_sessions:
            return False
        if device.active_commands >= device.descriptor.max_concurrent_commands:
            return False
        if device.runtime_health and not bool(device.runtime_health.get("healthy", True)):
            return False
        for capability in required:
            if device.capability_health and device.capability_health.get(capability) is False:
                return False
        return True

    def _select_gui_resource_owner(
        self, candidates: list[RegisteredDevice], required: set[Capability]
    ) -> RegisteredDevice | None:
        for candidate in candidates:
            if not self.gui_policy.session_requires_gui_resource(candidate, required):
                continue
            owner = self.registry.gui_resource_owner(candidate.descriptor.gui_resource_id)
            if owner is not None and owner in candidates and self._has_session_capacity(owner, required):
                return owner
        return None

    @staticmethod
    def _has_session_capacity(device: RegisteredDevice, required: set[Capability]) -> bool:
        if not device.descriptor.supports_parallel_sessions and device.active_sessions >= 1:
            return False
        if device.descriptor.requires_foreground_focus and device.active_sessions >= device.descriptor.max_gui_sessions:
            return False
        if device.active_sessions >= device.descriptor.max_sessions:
            return False
        if device.runtime_health and not bool(device.runtime_health.get("healthy", True)):
            return False
        for capability in required:
            if device.capability_health and device.capability_health.get(capability) is False:
                return False
        return True
