"""Artifact catalog CRUD helpers for controller persistence."""

from __future__ import annotations

import sqlite3

from northcua.controller.storage.identifiers import artifact_catalog_id, utc_now_iso
from northcua.controller.storage.result_compaction import compact_artifact_ref_json_for_sqlite
from northcua_protocol.artifacts import ArtifactRef


class ArtifactRepositoryMixin:
    """Persist and query controller artifact catalog rows."""

    connection: sqlite3.Connection

    def record_artifact_ref(
        self,
        artifact: ArtifactRef,
        *,
        trace_id: str,
        task_id: str = "",
        session_id: str = "",
        command_id: str = "",
        source: str = "",
        catalog_artifact_id: str = "",
    ) -> None:
        """Persist one artifact catalog row for later inspection and lookup.

        Args:
            artifact: Artifact reference to catalog.
            trace_id: Trace id associated with the artifact.
            task_id: Optional task id for task-scoped artifacts.
            session_id: Optional session id for session-scoped artifacts.
            command_id: Optional command id for command output artifacts.
            source: Human-readable source label.
            catalog_artifact_id: Precomputed stable catalog id override.
        """

        persisted_artifact_id = catalog_artifact_id or artifact_catalog_id(
            artifact,
            trace_id=trace_id,
            task_id=task_id,
            session_id=session_id,
            command_id=command_id,
        )
        self.connection.execute(
            """
            INSERT OR REPLACE INTO artifacts(
                artifact_id,
                original_artifact_id,
                source,
                trace_id,
                task_id,
                session_id,
                command_id,
                kind,
                storage_kind,
                mime_type,
                sha256,
                size_bytes,
                uri,
                artifact_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                persisted_artifact_id,
                artifact.artifact_id,
                source,
                trace_id,
                task_id,
                session_id,
                command_id,
                artifact.kind.value,
                artifact.storage_kind.value,
                artifact.mime_type,
                artifact.sha256,
                artifact.size_bytes,
                artifact.uri,
                compact_artifact_ref_json_for_sqlite(artifact),
                utc_now_iso(),
            ),
        )
        self.connection.commit()

    def artifact_row_by_ref(
        self,
        artifact: ArtifactRef,
        *,
        task_id: str = "",
        session_id: str = "",
        command_id: str = "",
    ) -> sqlite3.Row | None:
        """Return an existing catalog row that owns a controller blob ref.

        Args:
            artifact: Controller blob artifact claimed by a peer.
            task_id: Optional task scope that must match the catalog row.
            session_id: Optional session scope that must match the catalog row.
            command_id: Optional command scope that must match the catalog row.

        Returns:
            Matching artifact row, or ``None`` when the blob was not minted for
            the requested scope.
        """

        clauses = ["sha256=?", "storage_kind=?", "uri=?"]
        parameters = [artifact.sha256, artifact.storage_kind.value, artifact.uri]
        if task_id:
            clauses.append("task_id=?")
            parameters.append(task_id)
        if session_id:
            clauses.append("session_id=?")
            parameters.append(session_id)
        if command_id:
            clauses.append("command_id=?")
            parameters.append(command_id)
        return self.connection.execute(
            f"SELECT * FROM artifacts WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 1",
            parameters,
        ).fetchone()

    def artifact_rows(self, *, trace_id: str = "", task_id: str = "") -> list[sqlite3.Row]:
        """Return artifact catalog rows filtered by trace or task when requested.

        Args:
            trace_id: Optional trace filter.
            task_id: Optional task filter.

        Returns:
            Artifact catalog rows ordered by creation time.
        """

        clauses: list[str] = []
        parameters: list[str] = []
        if trace_id:
            clauses.append("trace_id=?")
            parameters.append(trace_id)
        if task_id:
            clauses.append("task_id=?")
            parameters.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return list(
            self.connection.execute(
                f"SELECT * FROM artifacts {where} ORDER BY created_at DESC, artifact_id ASC",
                parameters,
            )
        )
