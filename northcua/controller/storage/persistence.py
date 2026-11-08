"""SQLite persistence facade for controller storage."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from urllib.parse import quote

from northcua.controller.storage.artifacts import ArtifactRepositoryMixin
from northcua.controller.storage.commands import CommandRepositoryMixin
from northcua.controller.storage.devices import DeviceRepositoryMixin
from northcua.controller.storage.events import EventRepositoryMixin
from northcua.controller.storage.identifiers import artifact_catalog_id, command_storage_key, utc_now_iso
from northcua.controller.storage.schema import SchemaMixin
from northcua.controller.storage.sessions import SessionRepositoryMixin
from northcua.controller.storage.telemetry import TelemetryRepositoryMixin
from northcua.controller.storage.worker_jobs import WorkerJobRepositoryMixin


class ProfiledSQLiteConnection(sqlite3.Connection):
    """SQLite connection with opt-in per-operation SQL timing."""

    _profile_active = False
    _profile_sql_seconds = 0.0
    _profile_sql_count = 0

    def begin_operation_profile(self) -> None:
        self._profile_active = True
        self._profile_sql_seconds = 0.0
        self._profile_sql_count = 0

    def end_operation_profile(self) -> tuple[float, int]:
        self._profile_active = False
        return self._profile_sql_seconds * 1000.0, self._profile_sql_count

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        started_at = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        finally:
            self._record_sql_timing(started_at)

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> sqlite3.Cursor:
        started_at = time.perf_counter()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            self._record_sql_timing(started_at)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        started_at = time.perf_counter()
        try:
            return super().executescript(sql_script)
        finally:
            self._record_sql_timing(started_at)

    def commit(self) -> None:
        started_at = time.perf_counter()
        try:
            super().commit()
        finally:
            self._record_sql_timing(started_at, count_statement=False)

    def rollback(self) -> None:
        started_at = time.perf_counter()
        try:
            super().rollback()
        finally:
            self._record_sql_timing(started_at, count_statement=False)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        started_at = time.perf_counter()
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self._record_sql_timing(started_at, count_statement=False)

    def _record_sql_timing(self, started_at: float, *, count_statement: bool = True) -> None:
        if self._profile_active:
            self._profile_sql_seconds += time.perf_counter() - started_at
            if count_statement:
                self._profile_sql_count += 1


class SQLiteControllerStore(
    SchemaMixin,
    DeviceRepositoryMixin,
    SessionRepositoryMixin,
    CommandRepositoryMixin,
    ArtifactRepositoryMixin,
    EventRepositoryMixin,
    TelemetryRepositoryMixin,
    WorkerJobRepositoryMixin,
):
    """Small SQLite store backing the single-controller v1 deployment."""

    connection: ProfiledSQLiteConnection

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            uri_path = quote(str(self.path.expanduser().resolve()), safe="/:")
            self.connection = cast(
                ProfiledSQLiteConnection,
                sqlite3.connect(
                    f"file:{uri_path}?mode=ro",
                    uri=True,
                    timeout=30.0,
                    factory=ProfiledSQLiteConnection,
                ),
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.execute("PRAGMA query_only=ON")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = cast(
            ProfiledSQLiteConnection,
            sqlite3.connect(self.path, factory=ProfiledSQLiteConnection),
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    def close(self) -> None:
        """Close the SQLite connection."""

        self.connection.close()

    def begin_operation_profile(self) -> None:
        self.connection.begin_operation_profile()

    def end_operation_profile(self) -> tuple[float, int]:
        return self.connection.end_operation_profile()


__all__ = [
    "SQLiteControllerStore",
    "artifact_catalog_id",
    "command_storage_key",
    "utc_now_iso",
]
