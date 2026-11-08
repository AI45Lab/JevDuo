"""Shared GUI resource policy for controller scheduling and command dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from northcua.controller.control_plane.registry import RegisteredDevice
from northcua_protocol.capabilities import Capability, ToolDescriptor

GUI_SESSION_CAPABILITIES = frozenset(
    {
        Capability.DESKTOP_SCREENSHOT,
        Capability.DESKTOP_INPUT,
        Capability.DESKTOP_WINDOW,
        Capability.A11Y_TREE,
    }
)


@dataclass(frozen=True, slots=True)
class GuiResourcePolicy:
    """Decide when a task or command must coordinate shared GUI focus.

    The controller has two related but distinct decisions:

    - session placement: GUI-capable desktop tasks should prefer an existing
      owner of the same physical display so multiple sessions do not fight the
      scheduler's affinity model.
    - command dispatch: only concrete tools that advertise
      ``requires_focus=True`` need the command-level GUI mutex. Read-only
      screenshot and accessibility probes may run without holding the focus
      lock, while click/type/drag commands remain serialized.

    Attributes:
        session_capabilities: Coarse capabilities that make task placement use
            GUI-resource affinity before command-level focus details are known.
    """

    session_capabilities: frozenset[Capability] = GUI_SESSION_CAPABILITIES

    def session_requires_gui_resource(self, device: RegisteredDevice, required: set[Capability]) -> bool:
        """Return whether a scheduled task should use GUI-resource affinity.

        Args:
            device: Candidate registered device.
            required: Task-required capabilities.

        Returns:
            ``True`` when the task touches a foreground-sensitive GUI resource.
        """

        return (
            bool(required & self.session_capabilities)
            and device.descriptor.requires_foreground_focus
            and bool(device.descriptor.gui_resource_id)
        )

    def command_gui_resource_id(self, *, device: RegisteredDevice | None, descriptor: ToolDescriptor | None) -> str:
        """Return the GUI resource id that should serialize one command.

        Args:
            device: Target device, if it is still registered.
            descriptor: Descriptor for the command's concrete tool id.

        Returns:
            Shared GUI resource id, or an empty string when no command lock is
            required.
        """

        if device is None or descriptor is None:
            return ""
        if not device.descriptor.requires_foreground_focus:
            return ""
        if not device.descriptor.gui_resource_id:
            return ""
        return device.descriptor.gui_resource_id if descriptor.requires_focus else ""


DEFAULT_GUI_RESOURCE_POLICY = GuiResourcePolicy()
