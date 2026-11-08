"""Controller telemetry CRUD helpers."""

from __future__ import annotations

import json
import sqlite3

from northcua.telemetry.contracts import CaseTelemetrySnapshot, PhaseTiming, TelemetryEvent


class TelemetryRepositoryMixin:
    """Persist typed telemetry projections in the controller database."""

    connection: sqlite3.Connection

    def append_telemetry_event(self, event: TelemetryEvent) -> int:
        """Append one typed telemetry event.

        Args:
            event: Redacted telemetry event to persist.

        Returns:
            SQLite row id assigned to the event.

        Raises:
            RuntimeError: If SQLite does not expose a row id after insertion.
        """

        payload = event.model_dump(mode="json")
        cursor = self._insert_telemetry_event(event)
        self.connection.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("SQLite did not return a telemetry event row id")
        return int(row_id)

    def _insert_telemetry_event(self, event: TelemetryEvent) -> sqlite3.Cursor:
        """Insert one telemetry event without committing the transaction."""

        payload = event.model_dump(mode="json")
        return self.connection.execute(
            """
            INSERT INTO telemetry_events(
                experiment_id, batch_id, case_id, trace_id, entity_kind, entity_id,
                event_type, phase, status, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.experiment_id,
                event.batch_id,
                event.case_id,
                event.trace_id,
                event.entity_kind.value,
                event.entity_id,
                event.event_type.value,
                event.phase.value if event.phase is not None else "",
                event.status.value if event.status is not None else "",
                json.dumps(payload, ensure_ascii=False),
                event.created_at,
            ),
        )

    def telemetry_event_rows(
        self,
        *,
        experiment_id: str = "",
        batch_id: str = "",
        case_id: str = "",
        case_ids: set[str] | None = None,
        event_types: set[str] | None = None,
        since_event_id: int = 0,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Return telemetry event rows matching optional filters.

        Args:
            experiment_id: Optional experiment namespace.
            batch_id: Optional batch namespace.
            case_id: Optional manifest task/case id.
            case_ids: Optional set of manifest task/case ids.
            event_types: Optional set of telemetry event type strings.
            since_event_id: Return rows strictly newer than this event id.
            limit: Maximum rows to return.

        Returns:
            Matching rows ordered by event id.
        """

        clauses = ["event_id > ?"]
        values: list[object] = [since_event_id]
        if experiment_id:
            clauses.append("experiment_id = ?")
            values.append(experiment_id)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        if case_id:
            clauses.append("case_id = ?")
            values.append(case_id)
        if case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            clauses.append(f"case_id IN ({placeholders})")
            values.extend(sorted(case_ids))
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            values.extend(sorted(event_types))
        sql = "SELECT * FROM telemetry_events WHERE " + " AND ".join(clauses) + " ORDER BY event_id LIMIT ?"
        values.append(max(limit, 1))
        return list(self.connection.execute(sql, values))

    def upsert_case_snapshot(self, snapshot: CaseTelemetrySnapshot) -> None:
        """Insert or replace a case telemetry snapshot.

        Args:
            snapshot: Latest case projection.
        """

        self._upsert_case_snapshot(snapshot)
        self.connection.commit()

    def _upsert_case_snapshot(self, snapshot: CaseTelemetrySnapshot) -> sqlite3.Cursor:
        """Upsert one case snapshot without committing the transaction."""

        return self.connection.execute(
            """
            INSERT INTO case_snapshots(
                experiment_id, batch_id, case_id, status, phase, updated_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, batch_id, case_id) DO UPDATE SET
                experiment_id=excluded.experiment_id,
                status=excluded.status,
                phase=excluded.phase,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (
                snapshot.experiment_id,
                snapshot.batch_id,
                snapshot.case_id,
                snapshot.status.value,
                snapshot.phase.value,
                snapshot.updated_at,
                snapshot.model_dump_json(),
            ),
        )

    def case_snapshot_rows(
        self,
        *,
        experiment_id: str = "",
        batch_id: str = "",
    ) -> list[sqlite3.Row]:
        """Return latest case snapshots for an experiment or batch.

        Args:
            experiment_id: Optional experiment namespace.
            batch_id: Optional batch namespace.

        Returns:
            Snapshot rows ordered by case id.
        """

        clauses: list[str] = []
        values: list[object] = []
        if experiment_id:
            clauses.append("experiment_id = ?")
            values.append(experiment_id)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return list(self.connection.execute(f"SELECT * FROM case_snapshots{where} ORDER BY case_id", values))

    def record_case_phase_timing(self, timing: PhaseTiming) -> None:
        """Persist one phase timing row.

        Args:
            timing: Measured case phase timing.
        """

        self._insert_case_phase_timing(timing)
        self.connection.commit()

    def _insert_case_phase_timing(self, timing: PhaseTiming) -> sqlite3.Cursor:
        """Insert one case phase timing without committing the transaction."""

        return self.connection.execute(
            """
            INSERT INTO case_phase_timings(
                experiment_id, batch_id, case_id, attempt, phase, status,
                started_at, finished_at, duration_seconds, failure_category, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timing.experiment_id,
                timing.batch_id,
                timing.case_id,
                timing.attempt,
                timing.phase.value,
                timing.status.value,
                timing.started_at,
                timing.finished_at,
                timing.duration_seconds,
                timing.failure_category.value if timing.failure_category is not None else "",
                timing.error,
            ),
        )

    def case_phase_timing_rows(
        self,
        *,
        experiment_id: str = "",
        batch_id: str = "",
        case_id: str = "",
    ) -> list[sqlite3.Row]:
        """Return phase timing rows matching optional filters.

        Args:
            experiment_id: Optional experiment namespace.
            batch_id: Optional batch namespace.
            case_id: Optional manifest task/case id.

        Returns:
            Matching phase timing rows ordered by row id.
        """

        clauses: list[str] = []
        values: list[object] = []
        if experiment_id:
            clauses.append("experiment_id = ?")
            values.append(experiment_id)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        if case_id:
            clauses.append("case_id = ?")
            values.append(case_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return list(self.connection.execute(f"SELECT * FROM case_phase_timings{where} ORDER BY row_id", values))
