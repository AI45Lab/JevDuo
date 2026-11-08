"""Device CRUD helpers for controller persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from northcua.controller.storage.identifiers import utc_now_iso
from northcua_protocol.capabilities import DeviceDescriptor
from northcua_protocol.state import DeviceState


class DeviceRepositoryMixin:
    """Persist and query controller device rows."""

    connection: sqlite3.Connection

    def upsert_device(
        self,
        descriptor: DeviceDescriptor,
        *,
        status: DeviceState,
        active_sessions: int = 0,
        load_json: str = "{}",
    ) -> None:
        """Insert or update a device row.

        Args:
            descriptor: Latest device descriptor.
            status: Persisted device lifecycle state.
            active_sessions: Current active session count.
            load_json: Serialized heartbeat/load projection.
        """

        self.connection.execute(
            """
            INSERT INTO devices(device_id, status, descriptor_json, last_heartbeat_at, active_sessions, load_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
              status=excluded.status,
              descriptor_json=excluded.descriptor_json,
              last_heartbeat_at=excluded.last_heartbeat_at,
              active_sessions=excluded.active_sessions,
              load_json=excluded.load_json
            """,
            (
                descriptor.device_id,
                status.value,
                descriptor.model_dump_json(),
                utc_now_iso(),
                active_sessions,
                load_json,
            ),
        )
        self.connection.commit()

    def update_device_status(self, device_id: str, status: DeviceState) -> None:
        """Update one persisted device status without replacing its descriptor.

        Args:
            device_id: Device row to update.
            status: New device lifecycle state.
        """

        self.connection.execute(
            "UPDATE devices SET status=?, last_heartbeat_at=? WHERE device_id=?",
            (status.value, utc_now_iso(), device_id),
        )
        self.connection.commit()

    def heartbeat_devices_batch(self, rows: Sequence[tuple[str, str, str]]) -> int:
        """Persist coalesced heartbeat projections without changing lifecycle state.

        Args:
            rows: ``(last_heartbeat_at, load_json, device_id)`` tuples.

        Returns:
            Number of online device rows updated.

        Heartbeats are deliberately update-only and guarded by the current
        online state. A delayed background flush must never recreate a device,
        move a draining/offline device back online, or overwrite the
        controller-owned active-session count.
        """

        if not rows:
            return 0
        cursor = self.connection.executemany(
            """
            UPDATE devices
            SET last_heartbeat_at=?, load_json=?
            WHERE device_id=? AND status=? AND last_heartbeat_at < ?
            """,
            [(*row, DeviceState.ONLINE.value, row[0]) for row in rows],
        )
        self.connection.commit()
        return max(int(cursor.rowcount), 0)

    def device_rows(self) -> list[sqlite3.Row]:
        """Return all device rows.

        Returns:
            SQLite rows ordered by device id.
        """

        return list(self.connection.execute("SELECT * FROM devices ORDER BY device_id"))
