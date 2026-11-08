"""Event log CRUD helpers for controller persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from northcua.controller.storage.identifiers import utc_now_iso


@dataclass(frozen=True)
class ControllerEventRow:
    """Controller event row ready for SQLite insertion.

    Attributes:
        trace_id: Trace id associated with the event.
        entity_type: Entity category, such as task, session, or command.
        entity_id: Entity id within the category.
        event_type: Stable event type string.
        payload: Redacted JSON-safe event metadata.
    """

    trace_id: str
    entity_type: str
    entity_id: str
    event_type: str
    payload: dict[str, object]


class EventRepositoryMixin:
    """Append and query controller event rows."""

    connection: sqlite3.Connection

    def append_event(
        self,
        *,
        trace_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        """Append one controller event.

        Args:
            trace_id: Trace id associated with the event.
            entity_type: Entity category, such as task, session, or command.
            entity_id: Entity id within the category.
            event_type: Stable event type string.
            payload: Redacted event metadata.
        """

        self.append_events(
            [
                ControllerEventRow(
                    trace_id=trace_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    event_type=event_type,
                    payload=payload,
                )
            ]
        )

    def append_events(self, events: Iterable[ControllerEventRow]) -> None:
        """Append controller events with one commit.

        Args:
            events: Redacted controller events to persist.
        """

        rows = [
            (
                event.trace_id,
                event.entity_type,
                event.entity_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False),
                utc_now_iso(),
            )
            for event in events
        ]
        if not rows:
            return
        self.connection.executemany(
            """
            INSERT INTO events(trace_id, entity_type, entity_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()

    def event_rows(self) -> list[sqlite3.Row]:
        """Return all event rows.

        Returns:
            Controller event rows ordered by append id.
        """

        return list(self.connection.execute("SELECT * FROM events ORDER BY event_id"))
