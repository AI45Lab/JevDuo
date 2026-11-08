"""SQLite schema and migration helpers for controller persistence."""

from __future__ import annotations

import sqlite3


class SchemaMixin:
    """Create and migrate the controller SQLite schema."""

    connection: sqlite3.Connection

    def _initialize(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                last_heartbeat_at TEXT,
                active_sessions INTEGER NOT NULL DEFAULT 0,
                load_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                device_id TEXT,
                lease_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT,
                generation INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS leases_session_generation_idx
                ON leases(session_id, generation);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                agent_kind TEXT NOT NULL,
                submitter_id TEXT NOT NULL DEFAULT '',
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS tasks_created_idx
                ON tasks(created_at DESC);
            CREATE INDEX IF NOT EXISTS tasks_created_task_idx
                ON tasks(created_at DESC, task_id ASC);
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                protocol_command_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                lease_id TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS commands_device_status_idx
                ON commands(device_id, status);
            CREATE INDEX IF NOT EXISTS commands_session_status_idx
                ON commands(session_id, status);
            CREATE INDEX IF NOT EXISTS sessions_device_status_idx
                ON sessions(device_id, status);
            CREATE INDEX IF NOT EXISTS sessions_task_status_idx
                ON sessions(task_id, status);
            CREATE INDEX IF NOT EXISTS sessions_status_task_idx
                ON sessions(status, task_id);
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_type_created_idx
                ON events(event_type, created_at);
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                original_artifact_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                command_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                storage_kind TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uri TEXT NOT NULL DEFAULT '',
                artifact_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telemetry_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL DEFAULT '',
                case_id TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                entity_kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS telemetry_events_experiment_idx
                ON telemetry_events(experiment_id, event_id);
            CREATE INDEX IF NOT EXISTS telemetry_events_batch_case_idx
                ON telemetry_events(batch_id, case_id, event_id);
            CREATE TABLE IF NOT EXISTS case_snapshots (
                experiment_id TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(experiment_id, batch_id, case_id)
            );
            CREATE INDEX IF NOT EXISTS case_snapshots_experiment_idx
                ON case_snapshots(experiment_id, status);
            CREATE TABLE IF NOT EXISTS case_phase_timings (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                failure_category TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS case_phase_timings_case_idx
                ON case_phase_timings(batch_id, case_id, row_id);
            CREATE TABLE IF NOT EXISTS worker_jobs (
                job_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                agent_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_pid INTEGER NOT NULL DEFAULT 0,
                failure_kind TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                artifact_manifest_json TEXT NOT NULL DEFAULT '{}',
                heartbeat_payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                heartbeat_at TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS worker_jobs_task_idx
                ON worker_jobs(task_id, attempt_id);
            CREATE UNIQUE INDEX IF NOT EXISTS worker_jobs_task_attempt_unique
                ON worker_jobs(task_id, attempt_id);
            CREATE INDEX IF NOT EXISTS worker_jobs_status_idx
                ON worker_jobs(status, updated_at);
            """)
        self._ensure_column("tasks", "submitter_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("commands", "protocol_command_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("commands", "lease_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("artifacts", "original_artifact_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("artifacts", "source", "TEXT NOT NULL DEFAULT ''")
        self._ensure_worker_jobs_columns()
        self.connection.commit()

    def _ensure_column(self, table_name: str, column_name: str, ddl: str) -> None:
        """Add a missing SQLite column in-place for existing dev databases."""

        existing_columns = {str(row["name"]) for row in self.connection.execute(f"PRAGMA table_info({table_name})")}
        if column_name in existing_columns:
            return
        self.connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")

    def _ensure_worker_jobs_columns(self) -> None:
        """Migrate worker job columns for existing controller databases."""

        for column_name, ddl in (
            ("attempt_id", "TEXT NOT NULL DEFAULT ''"),
            ("worker_pid", "INTEGER NOT NULL DEFAULT 0"),
            ("failure_kind", "TEXT NOT NULL DEFAULT ''"),
            ("error", "TEXT NOT NULL DEFAULT ''"),
            ("artifact_manifest_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("heartbeat_payload_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("heartbeat_at", "TEXT"),
            ("finished_at", "TEXT"),
        ):
            self._ensure_column("worker_jobs", column_name, ddl)
