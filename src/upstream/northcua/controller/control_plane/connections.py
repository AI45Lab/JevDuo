"""Connection registry for controller websocket peers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class WebSocketPeer(Protocol):
    """Minimal websocket surface used by the controller."""

    async def send(self, raw: str, /) -> None:
        """Send a serialized envelope.

        Args:
            raw: JSON serialized protocol envelope.
        """


class ConnectionRole(str, Enum):
    """Controller-visible role for a websocket connection."""

    UNKNOWN = "unknown"
    DEVICE = "device"
    SUBMITTER = "submitter"
    DEVICE_SUBMITTER = "device_submitter"


@dataclass(slots=True)
class ConnectionInfo:
    """One websocket peer registered with the controller.

    Attributes:
        connection_id: Controller-local stable id for this websocket.
        websocket: Minimal websocket peer surface used for sends.
        role: Current connection role derived from observed messages.
        device_id: Device id when the peer has sent DEVICE_HELLO.
        submitter_id: Submitter id when the peer has submitted a task.
        websocket_open_handshake_ms: Server-side websocket opening handshake duration.
        websocket_handler_start_delay_ms: Delay between handshake start and handler start.
        first_envelope_ms: Delay from handler start to first parsed envelope.
        first_envelope_type: First parsed protocol envelope type.
    """

    connection_id: str
    websocket: WebSocketPeer
    role: ConnectionRole = ConnectionRole.UNKNOWN
    device_id: str = ""
    submitter_id: str = ""
    websocket_open_handshake_ms: int = -1
    websocket_handler_start_delay_ms: int = -1
    first_envelope_ms: int = -1
    first_envelope_type: str = ""


class ConnectionRegistry:
    """Track websocket peer roles and lookup by device or submitter id."""

    def __init__(self) -> None:
        self._by_websocket: dict[object, ConnectionInfo] = {}
        self._by_device_id: dict[str, ConnectionInfo] = {}
        self._by_submitter_id: dict[str, ConnectionInfo] = {}

    def ensure(self, websocket: WebSocketPeer) -> ConnectionInfo:
        """Return existing connection info or register a new unknown peer.

        Args:
            websocket: Websocket-like peer object.

        Returns:
            Connection info associated with the peer.
        """

        key = websocket
        info = self._by_websocket.get(key)
        if info is not None:
            return info
        info = ConnectionInfo(connection_id=uuid.uuid4().hex, websocket=websocket)
        self._by_websocket[key] = info
        return info

    def mark_device(self, websocket: WebSocketPeer, device_id: str) -> ConnectionInfo:
        """Mark a websocket as a device connection.

        Args:
            websocket: Websocket-like peer object.
            device_id: Device id advertised by DEVICE_HELLO.

        Returns:
            Updated connection info.
        """

        previous = self._by_device_id.get(device_id)
        if previous is not None and previous.websocket is not websocket:
            previous.device_id = ""
            previous.role = ConnectionRole.SUBMITTER if previous.submitter_id else ConnectionRole.UNKNOWN
        info = self.ensure(websocket)
        info.device_id = device_id
        info.role = ConnectionRole.DEVICE_SUBMITTER if info.submitter_id else ConnectionRole.DEVICE
        self._by_device_id[device_id] = info
        return info

    def mark_submitter(self, websocket: WebSocketPeer, submitter_id: str) -> ConnectionInfo:
        """Mark a websocket as a task submitter connection.

        Args:
            websocket: Websocket-like peer object.
            submitter_id: Stable submitter id for task ownership checks.

        Returns:
            Updated connection info.
        """

        previous = self._by_submitter_id.get(submitter_id)
        if previous is not None and previous.websocket is not websocket:
            previous.submitter_id = ""
            previous.role = ConnectionRole.DEVICE if previous.device_id else ConnectionRole.UNKNOWN
        info = self.ensure(websocket)
        info.submitter_id = submitter_id
        info.role = ConnectionRole.DEVICE_SUBMITTER if info.device_id else ConnectionRole.SUBMITTER
        self._by_submitter_id[submitter_id] = info
        return info

    def get(self, websocket: WebSocketPeer) -> ConnectionInfo:
        """Return connection info for a websocket.

        Args:
            websocket: Websocket-like peer object.

        Returns:
            Existing or newly created connection info.
        """

        return self.ensure(websocket)

    def find(self, websocket: WebSocketPeer) -> ConnectionInfo | None:
        """Return connection info for a websocket when it is still registered.

        Args:
            websocket: Websocket-like peer object.

        Returns:
            Registered connection info, or `None` after disconnect cleanup.
        """

        return self._by_websocket.get(websocket)

    def for_device(self, device_id: str) -> ConnectionInfo | None:
        """Return the active connection for a device id.

        Args:
            device_id: Device id advertised by DEVICE_HELLO.

        Returns:
            Active connection info for that device, if connected.
        """

        return self._by_device_id.get(device_id)

    def for_submitter(self, submitter_id: str) -> ConnectionInfo | None:
        """Return the active connection for a submitter id.

        Args:
            submitter_id: Submitter id advertised by TASK_REQUEST ownership metadata.

        Returns:
            Active connection info for the submitter, if connected.
        """

        return self._by_submitter_id.get(submitter_id)

    def snapshot(self) -> dict[str, object]:
        """Return lightweight connection counts for controller telemetry.

        Returns:
            JSON-safe connection count snapshot grouped by role.
        """

        role_counts: dict[str, int] = {role.value: 0 for role in ConnectionRole}
        for info in self._by_websocket.values():
            role_counts[info.role.value] = role_counts.get(info.role.value, 0) + 1
        return {
            "active_connections": len(self._by_websocket),
            "device_connections": len(self._by_device_id),
            "submitter_connections": len(self._by_submitter_id),
            "role_counts": role_counts,
        }

    def unregister(self, websocket: WebSocketPeer) -> ConnectionInfo | None:
        """Remove a websocket and all role indexes.

        Args:
            websocket: Websocket-like peer object being disconnected.

        Returns:
            Removed connection info, or `None` when it was already absent.
        """

        info = self._by_websocket.pop(websocket, None)
        if info is None:
            return None
        if info.device_id:
            if self._by_device_id.get(info.device_id) is info:
                self._by_device_id.pop(info.device_id, None)
        if info.submitter_id:
            if self._by_submitter_id.get(info.submitter_id) is info:
                self._by_submitter_id.pop(info.submitter_id, None)
        return info
