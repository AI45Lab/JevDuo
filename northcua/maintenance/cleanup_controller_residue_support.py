"""Support helpers for controller residue cleanup."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from northcua.maintenance.cleanup_controller_models import CleanupPaths, CleanupPlan, JsonDict


def _ensure_controller_cleanup_indexes(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "tasks"):
        _create_index_if_not_covered(
            connection,
            table="tasks",
            columns=("status", "task_id"),
            sql=("CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_tasks_status_task ON tasks(status, task_id)"),
        )
    if _table_exists(connection, "devices"):
        _create_index_if_not_covered(
            connection,
            table="devices",
            columns=("status",),
            sql="CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_devices_status ON devices(status)",
        )
    if _table_exists(connection, "sessions"):
        _create_index_if_not_covered(
            connection,
            table="sessions",
            columns=("status", "task_id"),
            sql=("CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_sessions_status_task ON sessions(status, task_id)"),
        )
        _create_index_if_not_covered(
            connection,
            table="sessions",
            columns=("task_id", "updated_at"),
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_sessions_task_updated ON sessions(task_id, updated_at)"
            ),
        )
        _create_index_if_not_covered(
            connection,
            table="sessions",
            columns=("device_id",),
            sql="CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_sessions_device ON sessions(device_id)",
        )
    if _table_exists(connection, "leases"):
        _create_index_if_not_covered(
            connection,
            table="leases",
            columns=("status", "session_id"),
            sql=("CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_leases_status_session ON leases(status, session_id)"),
        )
        _create_index_if_not_covered(
            connection,
            table="leases",
            columns=("session_id", "generation"),
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_leases_session_generation "
                "ON leases(session_id, generation)"
            ),
        )
    if _table_exists(connection, "commands"):
        _create_index_if_not_covered(
            connection,
            table="commands",
            columns=("status", "session_id"),
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_commands_status_session "
                "ON commands(status, session_id)"
            ),
        )
        _create_index_if_not_covered(
            connection,
            table="commands",
            columns=("device_id", "status"),
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_commands_device_status ON commands(device_id, status)"
            ),
        )
        _create_index_if_not_covered(
            connection,
            table="commands",
            columns=("session_id", "status"),
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_northcua_cleanup_commands_session_status "
                "ON commands(session_id, status)"
            ),
        )


def _create_index_if_not_covered(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    sql: str,
) -> None:
    if _has_covering_index(connection, table=table, columns=columns):
        return
    connection.execute(sql)


def _has_covering_index(connection: sqlite3.Connection, *, table: str, columns: tuple[str, ...]) -> bool:
    expected = tuple(columns)
    for row in connection.execute(f"PRAGMA index_list({_sqlite_identifier(table)})"):
        is_partial = bool(row[4]) if len(row) > 4 else False
        if is_partial:
            continue
        index_name = str(row[1])
        key_columns: list[str] = []
        usable = True
        for info in connection.execute(f"PRAGMA index_xinfo({_sqlite_identifier(index_name)})"):
            is_key_column = bool(info[5]) if len(info) > 5 else True
            if not is_key_column:
                continue
            cid = int(info[1])
            name = info[2]
            collseq = str(info[4] or "").upper() if len(info) > 4 else "BINARY"
            if cid < 0 or name is None or collseq != "BINARY":
                usable = False
                break
            key_columns.append(str(name))
            if len(key_columns) >= len(expected):
                break
        if not usable:
            continue
        indexed_columns = tuple(key_columns)
        if indexed_columns[: len(expected)] == expected:
            return True
    return False


def _sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _write_tsv(path: Path, fieldnames: list[str], rows: list[JsonDict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _backup_controller_rows(connection: sqlite3.Connection, plan: CleanupPlan, backup_path: Path | None) -> None:
    if backup_path is None:
        return
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    task_ids = {item.controller_task_id for item in plan.candidates if item.controller_task_id}
    session_ids = {item.session_id for item in plan.candidates if item.session_id}
    lease_ids = {item.lease_id for item in plan.candidates if item.lease_id}
    device_ids = {item.device_id for item in plan.candidates if item.device_id}
    payload: JsonDict = {
        "format": "controller_residue_delta_backup_v1",
        "created_at": _utc_now(),
        "batch_id": plan.batch_id,
        "controller_db": str(plan.controller_db),
        "tables": {
            "tasks": _rows_for_ids(connection, table="tasks", key="task_id", ids=task_ids),
            "sessions": _rows_for_ids(connection, table="sessions", key="session_id", ids=session_ids),
            "leases": _rows_for_ids(connection, table="leases", key="lease_id", ids=lease_ids),
            "devices": _rows_for_ids(connection, table="devices", key="device_id", ids=device_ids),
            "commands": _command_rows_for_sessions(connection, session_ids=session_ids),
        },
    }
    backup_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _rows_for_ids(
    connection: sqlite3.Connection,
    *,
    table: str,
    key: str,
    ids: set[str],
) -> list[JsonDict]:
    if not ids or not _table_exists(connection, table):
        return []
    rows: list[JsonDict] = []
    for chunk in _chunks(sorted(ids), 500):
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            _row_dict(row)
            for row in connection.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({placeholders})",
                tuple(chunk),
            )
        )
    return rows


def _command_rows_for_sessions(connection: sqlite3.Connection, *, session_ids: set[str]) -> list[JsonDict]:
    if not session_ids or not _table_exists(connection, "commands"):
        return []
    rows: list[JsonDict] = []
    for chunk in _chunks(sorted(session_ids), 500):
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            _row_dict(row)
            for row in connection.execute(
                f"SELECT * FROM commands WHERE session_id IN ({placeholders})",
                tuple(chunk),
            )
        )
    return rows


def _row_dict(row: sqlite3.Row) -> JsonDict:
    return {key: row[key] for key in row.keys()}


def _paths(*, batch_db: Path, output_dir: Path | None, apply: bool) -> CleanupPaths:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = output_dir or batch_db.parent / "batch_maintenance" / f"controller_residue_{ts}"
    backup = root / f"controller-residue-delta-backup-{ts}.json" if apply else None
    return CleanupPaths(
        output_dir=root,
        summary=root / "summary.json",
        devices_tsv=root / "cleanup_devices.tsv",
        sessions_tsv=root / "cleanup_sessions.tsv",
        leases_tsv=root / "cleanup_leases.tsv",
        blocked_tsv=root / "blocked.tsv",
        backup=backup,
    )


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {path}")
    connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _loads(raw: str) -> JsonDict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def _int_value(value: object, *, default: int) -> int:
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _blocked(reason: str, **fields: object) -> JsonDict:
    return {"reason": reason, **{key: str(value) for key, value in fields.items()}}


def _infer_batch_id(batch_db: Path) -> str:
    return batch_db.parent.name


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
