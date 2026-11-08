"""SQLite store for prepared sandbox reservations."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Concatenate, ParamSpec, TypeVar

from northcua.batch.contracts import BatchTaskStatus
from northcua.batch.preparation.contracts import (
    PreparedSandboxDepth,
    PreparedSandboxRecord,
    PreparedSandboxStatus,
)
from northcua.batch.retry_policy import terminal_decision_blocks_retry
from northcua.batch.state.sqlite_helpers import _valid_recovery_action_from_values
from northcua_protocol.capabilities import JsonObject

_UNASSIGNED_ACTIVE_STATUSES = {
    PreparedSandboxStatus.RESERVED.value,
    PreparedSandboxStatus.PREPARING.value,
    PreparedSandboxStatus.PREPARED.value,
}

_ASSIGNED_LIVE_STATUSES = {
    PreparedSandboxStatus.ASSIGNED.value,
    PreparedSandboxStatus.CLEANUP_DEFERRED.value,
}

_CLAIM_BLOCKING_STATUSES = {
    *_UNASSIGNED_ACTIVE_STATUSES,
    *_ASSIGNED_LIVE_STATUSES,
}

_CLEANUP_REQUIRED_STATUSES = {
    PreparedSandboxStatus.CLEANUP_FAILED.value,
}

_REUSABLE_HISTORY_STATUSES = {
    PreparedSandboxStatus.FAILED.value,
    PreparedSandboxStatus.RELEASED.value,
    PreparedSandboxStatus.CANCELED.value,
    PreparedSandboxStatus.EXPIRED.value,
}

_CLAIMABLE_TASK_STATUSES = {
    BatchTaskStatus.PENDING.value,
    BatchTaskStatus.FAILED.value,
    BatchTaskStatus.TIMED_OUT.value,
    BatchTaskStatus.STARTING_SANDBOX.value,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE.value,
    BatchTaskStatus.WAITING_DEVICE.value,
    BatchTaskStatus.SUBMITTED.value,
    BatchTaskStatus.RUNNING.value,
}

_TERMINAL_TASK_STATUSES = {
    BatchTaskStatus.SUCCEEDED.value,
    BatchTaskStatus.EVALUATION_PENDING.value,
    BatchTaskStatus.FAILED.value,
    BatchTaskStatus.TIMED_OUT.value,
    BatchTaskStatus.CANCELED.value,
}

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_StoreT = TypeVar("_StoreT", bound="_PreparedSandboxQueryMixin")


def _synchronized_store(
    method: Callable[Concatenate[_StoreT, _P], _R],
) -> Callable[Concatenate[_StoreT, _P], _R]:
    """Serialize prepared-store SQLite access on its connection lock.

    Args:
        method: Store method that uses the shared SQLite connection.

    Returns:
        Wrapped method that holds the store re-entrant lock for the duration of
        the SQLite operation.
    """

    @wraps(method)
    def wrapper(self: _StoreT, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Run the wrapped prepared-store method under the connection lock.

        Returns:
            Value returned by the wrapped method.
        """

        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


_CLAIMABLE_TASK_COLUMNS = """
    task_id, state_json, status, attempts, lease_token, lease_expires_at,
    cancel_requested, controller_task_id, last_failure_category,
    recovery_action, order_index
"""
_CLAIMABLE_TASK_SCAN_LIMIT = max(_env_int("NORTHCUA_PREPARED_CLAIMABLE_SCAN_LIMIT", 2048), 1)
_CLAIMABLE_TASK_SLOW_LOG_SECONDS = max(_env_float("NORTHCUA_PREPARED_CLAIMABLE_SLOW_LOG_SECONDS", 1.0), 0.0)
_CLAIMABLE_TASK_SLOW_LOG_INTERVAL_SECONDS = max(
    _env_float("NORTHCUA_PREPARED_CLAIMABLE_SLOW_LOG_INTERVAL_SECONDS", 30.0),
    0.0,
)


class _PreparedSandboxQueryMixin:
    """Read/query helpers shared by the prepared sandbox SQLite store."""

    _connection: sqlite3.Connection
    _lock: threading.RLock

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> PreparedSandboxRecord:
        raise NotImplementedError

    def _control_blocks_preparation(self) -> bool:
        raise NotImplementedError

    def _active_count_locked(self, now: float, *, runner_id: str = "") -> int:
        raise NotImplementedError

    def _live_count_locked(self, now: float, *, runner_id: str = "") -> int:
        raise NotImplementedError

    def _count_statuses_locked(
        self,
        statuses: set[str],
        *,
        runner_clause: str = "",
        runner_params: tuple[object, ...] = (),
    ) -> int:
        raise NotImplementedError

    def _assigned_terminal_count_locked(
        self,
        *,
        runner_clause: str = "",
        runner_params: tuple[object, ...] = (),
    ) -> int:
        raise NotImplementedError

    @_synchronized_store
    def active_record(self, *, task_id: str, attempt: int) -> PreparedSandboxRecord | None:
        """Return an active prepared row when present.

        Args:
            task_id: Batch task id.
            attempt: One-based attempt number.

        Returns:
            Active row with unexpired TTL, if present.
        """

        now = time.time()
        row = self._connection.execute(
            """
            SELECT *
            FROM prepared_sandboxes
            WHERE task_id=? AND attempt=? AND status IN ({placeholders}) AND expires_at>?
            """.format(placeholders=",".join("?" for _ in _CLAIM_BLOCKING_STATUSES)),
            (task_id, attempt, *sorted(_CLAIM_BLOCKING_STATUSES), now),
        ).fetchone()
        return self._record_from_row(row) if row is not None else None

    @_synchronized_store
    def active_count(self, *, runner_id: str = "") -> int:
        """Return active unassigned reservation count.

        Args:
            runner_id: Optional runner id to scope the count. When omitted,
                the count is batch-global and should be used for buffer caps.

        Returns:
            Number of unexpired unassigned active rows.
        """

        now = time.time()
        query = """
            SELECT COUNT(*)
            FROM prepared_sandboxes
            WHERE runner_id=? AND status IN ({placeholders}) AND expires_at>?
            """
        params: tuple[object, ...] = (runner_id, *sorted(_UNASSIGNED_ACTIVE_STATUSES), now)
        if not runner_id:
            query = """
                SELECT COUNT(*)
                FROM prepared_sandboxes
                WHERE status IN ({placeholders}) AND expires_at>?
                """
            params = (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now)
        row = self._connection.execute(
            query.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
            params,
        ).fetchone()
        return int(row[0] if row else 0)

    @_synchronized_store
    def live_count(self, *, runner_id: str = "") -> int:
        """Return live prepared-sandbox capacity, including assigned rows.

        Unassigned rows stop counting after their TTL expires. Assigned rows
        continue counting until the attempt release path, deferred cleanup
        repair, or maintenance terminates and resolves them.
        """

        return self._live_count_locked(time.time(), runner_id=runner_id)

    @_synchronized_store
    def live_budget_counts(self, *, runner_id: str = "") -> dict[str, int]:
        """Return prepared live-budget counters for telemetry and planning.

        The counters distinguish the desired unassigned buffer from assigned
        capacity that is still consuming provider resources.
        """

        now = time.time()
        runner_clause = " AND runner_id=?" if runner_id else ""
        runner_params: tuple[object, ...] = (runner_id,) if runner_id else ()
        active_unassigned = self._active_count_locked(now, runner_id=runner_id)
        assigned_total = self._count_statuses_locked(
            _ASSIGNED_LIVE_STATUSES,
            runner_clause=runner_clause,
            runner_params=runner_params,
        )
        cleanup_deferred = self._count_statuses_locked(
            {PreparedSandboxStatus.CLEANUP_DEFERRED.value},
            runner_clause=runner_clause,
            runner_params=runner_params,
        )
        assigned_terminal = self._assigned_terminal_count_locked(
            runner_clause=runner_clause,
            runner_params=runner_params,
        )
        live_total = active_unassigned + assigned_total
        return {
            "active_unassigned": active_unassigned,
            "assigned_total": assigned_total,
            "assigned_live": max(assigned_total - assigned_terminal, 0),
            "assigned_terminal": assigned_terminal,
            "cleanup_deferred": cleanup_deferred,
            "live_total": live_total,
        }

    @_synchronized_store
    def status_counts(self, *, runner_id: str = "") -> dict[str, int]:
        """Return prepared-sandbox row counts by status.

        Args:
            runner_id: Optional runner id filter.

        Returns:
            Mapping from prepared row status to row count.
        """

        query = "SELECT status, COUNT(*) AS count FROM prepared_sandboxes {where} GROUP BY status"
        params: tuple[object, ...] = ()
        where = ""
        if runner_id:
            where = "WHERE runner_id=?"
            params = (runner_id,)
        rows = self._connection.execute(query.format(where=where), params).fetchall()
        return {str(row["status"]): int(row["count"] or 0) for row in rows}

    @_synchronized_store
    def foreign_claim_blocking_count(
        self,
        *,
        runner_id: str,
        max_attempts_by_task: dict[str, int],
        stale_after_seconds: float,
    ) -> int:
        """Return foreign reservations that still block a claimable task.

        Args:
            runner_id: Current runner id. Only rows owned by other runners are
                considered blocking.
            max_attempts_by_task: Per-task retry budget used to ignore
                exhausted rows.
            stale_after_seconds: Reservation heartbeat grace; rows older than
                this no longer block task claiming.

        Returns:
            Number of live foreign reservations whose task attempt would be
            claimable if the reservation were absent.
        """

        now = time.time()
        stale_cutoff = now - stale_after_seconds if stale_after_seconds > 0 else 0
        rows = self._connection.execute(
            """
            SELECT t.*, p.attempt AS prepared_attempt
            FROM prepared_sandboxes p
            JOIN batch_tasks t ON t.task_id=p.task_id
            WHERE p.runner_id!=?
              AND p.status IN ({placeholders})
              AND p.expires_at>?
              AND p.updated_at>?
              AND p.attempt=CAST(t.attempts AS INTEGER)+1
            ORDER BY t.order_index ASC
            """.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
            (runner_id, *sorted(_UNASSIGNED_ACTIVE_STATUSES), now, stale_cutoff),
        ).fetchall()
        count = 0
        for row in rows:
            attempt = int(row["prepared_attempt"] or 0)
            if _task_row_can_be_prepared(
                row,
                attempt=attempt,
                now=now,
                max_attempts_by_task=max_attempts_by_task,
            ):
                count += 1
        return count

    @_synchronized_store
    def heartbeat_runner(self, *, runner_id: str) -> None:
        """Refresh liveness for this runner's unassigned active reservations.

        Args:
            runner_id: Runner id whose prepared manager is still alive.
        """

        now = time.time()
        self._connection.execute(
            """
            UPDATE prepared_sandboxes
            SET updated_at=?
            WHERE runner_id=? AND status IN ({placeholders}) AND expires_at>?
            """.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
            (now, runner_id, *sorted(_UNASSIGNED_ACTIVE_STATUSES), now),
        )

    @_synchronized_store
    def active_records(self, *, runner_id: str = "") -> list[PreparedSandboxRecord]:
        """Return active prepared rows, optionally scoped to one runner.

        Args:
            runner_id: Optional runner id filter.

        Returns:
            Active rows ordered by reservation time.
        """

        now = time.time()
        params: tuple[object, ...]
        query = """
            SELECT *
            FROM prepared_sandboxes
            WHERE status IN ({placeholders}) AND expires_at>?
            {runner_filter}
            ORDER BY reserved_at ASC
        """
        if runner_id:
            params = (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now, runner_id)
            runner_filter = "AND runner_id=?"
        else:
            params = (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now)
            runner_filter = ""
        rows = self._connection.execute(
            query.format(
                placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES),
                runner_filter=runner_filter,
            ),
            params,
        ).fetchall()
        return [self._record_from_row(row) for row in rows]

    @_synchronized_store
    def claimable_task_ids(
        self,
        *,
        max_attempts_by_task: dict[str, int],
        limit: int,
    ) -> list[tuple[str, int]]:
        """Return upcoming unowned task attempts that are safe to prepare.

        Args:
            max_attempts_by_task: Per-task retry budget used to skip exhausted
                rows before reserving background capacity.
            limit: Maximum number of task-attempt keys to return.

        Returns:
            Ordered ``(task_id, next_attempt)`` tuples.
        """

        started_at = time.monotonic()
        stats = {
            "existing_attempt_keys": 0,
            "scan_chunks": 0,
            "scan_rows": 0,
            "last_order_index": -1,
        }
        now = time.time()
        try:
            existing_attempt_keys = {
                (str(row["task_id"]), int(row["attempt"]))
                for row in self._connection.execute(
                    f"""
                    SELECT task_id, attempt
                    FROM prepared_sandboxes
                    WHERE status NOT IN ({",".join("?" for _ in _REUSABLE_HISTORY_STATUSES)})
                    """,
                    tuple(sorted(_REUSABLE_HISTORY_STATUSES)),
                ).fetchall()
            }
            stats["existing_attempt_keys"] = len(existing_attempt_keys)
            if self._control_blocks_preparation():
                stats["control_blocked"] = 1
                return []
            selected: list[tuple[str, int]] = []
            last_order_index = -1
            scan_limit = _CLAIMABLE_TASK_SCAN_LIMIT
            while len(selected) < limit:
                rows = self._claimable_task_rows(
                    now=now,
                    after_order_index=last_order_index,
                    limit=scan_limit,
                    stats=stats,
                )
                if not rows:
                    return selected
                last_order_index = int(rows[-1]["order_index"])
                for row in rows:
                    if len(selected) >= limit:
                        break
                    task_id = str(row["task_id"])
                    attempts = int(row["attempts"] or 0)
                    budget = max_attempts_by_task.get(task_id)
                    attempt = attempts + 1
                    if not _task_row_can_be_prepared(
                        row,
                        attempt=attempt,
                        now=now,
                        max_attempts_by_task=max_attempts_by_task,
                    ):
                        continue
                    if budget is not None and attempts >= budget:
                        continue
                    if (task_id, attempt) in existing_attempt_keys:
                        continue
                    selected.append((task_id, attempt))
            return selected
        finally:
            self._maybe_log_claimable_task_latency(
                started_at=started_at,
                selected=len(locals().get("selected", [])),
                stats=stats,
            )

    def _claimable_task_rows(
        self,
        *,
        now: float,
        after_order_index: int,
        limit: int,
        stats: dict[str, int],
    ) -> list[sqlite3.Row]:
        stats["scan_chunks"] += 1
        stats["last_order_index"] = after_order_index
        rows = self._connection.execute(
            """
            SELECT {columns}
            FROM batch_tasks INDEXED BY idx_batch_tasks_claim_order
            WHERE cancel_requested=0
              AND order_index > ?
              AND status IN ({placeholders})
              AND controller_task_id=''
              AND (
                lease_token=''
                OR lease_expires_at <= ?
              )
            ORDER BY order_index ASC
            LIMIT ?
            """.format(
                columns=_CLAIMABLE_TASK_COLUMNS,
                placeholders=",".join("?" for _ in _CLAIMABLE_TASK_STATUSES),
            ),
            (after_order_index, *sorted(_CLAIMABLE_TASK_STATUSES), now, limit),
        ).fetchall()
        stats["scan_rows"] += len(rows)
        return rows

    def _maybe_log_claimable_task_latency(
        self,
        *,
        started_at: float,
        selected: int,
        stats: dict[str, int],
    ) -> None:
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds < _CLAIMABLE_TASK_SLOW_LOG_SECONDS:
            return
        now = time.monotonic()
        if (
            _CLAIMABLE_TASK_SLOW_LOG_INTERVAL_SECONDS > 0
            and now - self._last_claimable_slow_log_at < _CLAIMABLE_TASK_SLOW_LOG_INTERVAL_SECONDS
        ):
            return
        self._last_claimable_slow_log_at = now
        logger.warning(
            "prepared sandbox claimable task scan slow",
            extra={
                "elapsed_seconds": round(elapsed_seconds, 3),
                "selected": selected,
                "claimable_task_scan_limit": _CLAIMABLE_TASK_SCAN_LIMIT,
                **stats,
            },
        )


class SQLitePreparedSandboxStore(_PreparedSandboxQueryMixin):
    """Small SQLite repository dedicated to prepared sandbox state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._create_schema()
        self._last_claimable_slow_log_at = 0.0

    def run_locked(self, operation: Callable[[], _R]) -> _R:
        """Run one prepared-store operation under the connection lock.

        Args:
            operation: Synchronous store operation to run with exclusive access
                to this SQLite connection.

        Returns:
            Result returned by ``operation``.
        """

        with self._lock:
            return operation()

    @_synchronized_store
    def reserve(
        self,
        *,
        task_id: str,
        attempt: int,
        runner_id: str,
        depth: PreparedSandboxDepth,
        provider: str,
        template: str,
        ttl_seconds: float,
        buffer_limit: int = 0,
        live_limit: int = 0,
        metadata: JsonObject | None = None,
    ) -> PreparedSandboxRecord | None:
        """Reserve one task attempt when no live prepared row owns it.

        Args:
            task_id: Batch task id to prepare.
            attempt: One-based task attempt number.
            runner_id: Runner process reserving the sandbox.
            depth: Initialization depth requested by the policy.
            provider: Sandbox provider id.
            template: Provider template alias.
            ttl_seconds: Seconds before an unassigned reservation expires.
            live_limit: Optional batch-global cap for total prepared provider
                capacity. This includes assigned/deferred rows so failed
                attempts cannot make the background pool grow without bound.
            metadata: Optional JSON-safe operator metadata.

        Returns:
            Created reservation row, or ``None`` when the same task attempt
            already has a live or cleanup-required prepared-sandbox row. Failed,
            released, and canceled history rows are replaced in-place.
        """

        now = time.time()
        expires_at = now + ttl_seconds
        token = uuid.uuid4().hex
        with _SQLiteTransaction(self._connection):
            self._expire_locked(now)
            if self._control_blocks_preparation():
                return None
            if buffer_limit > 0 and self._active_count_locked(now) >= buffer_limit:
                return None
            if live_limit > 0 and self._live_count_locked(now) >= live_limit:
                return None
            task_row = self._connection.execute(
                """
                SELECT task_id, status, attempts, lease_token, lease_expires_at,
                       cancel_requested, controller_task_id, state_json,
                       last_failure_category, recovery_action
                FROM batch_tasks
                WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            if not _task_row_can_be_prepared(
                task_row,
                attempt=attempt,
                now=now,
                max_attempts_by_task=None,
            ):
                return None
            existing_attempt = self._connection.execute(
                """
                SELECT status
                FROM prepared_sandboxes
                WHERE task_id=? AND attempt=?
                """,
                (task_id, attempt),
            ).fetchone()
            if existing_attempt is not None:
                if str(existing_attempt["status"]) not in _REUSABLE_HISTORY_STATUSES:
                    return None
                self._connection.execute(
                    """
                    UPDATE prepared_sandboxes
                    SET status=?,
                        runner_id=?,
                        depth=?,
                        provider=?,
                        template=?,
                        sandbox_id='',
                        device_id='',
                        lease_id='',
                        reservation_token=?,
                        reserved_at=?,
                        updated_at=?,
                        expires_at=?,
                        prepare_started_at=0,
                        prepared_at=0,
                        assigned_at=0,
                        released_at=0,
                        prepare_seconds=0,
                        error='',
                        metadata_json=?
                    WHERE task_id=? AND attempt=?
                    """,
                    (
                        PreparedSandboxStatus.RESERVED.value,
                        runner_id,
                        depth.value,
                        provider,
                        template,
                        token,
                        now,
                        now,
                        expires_at,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        task_id,
                        attempt,
                    ),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO prepared_sandboxes(
                        task_id, attempt, status, runner_id, depth, provider, template,
                        sandbox_id, device_id, lease_id, reservation_token, reserved_at,
                        updated_at, expires_at, prepare_started_at, prepared_at,
                        assigned_at, released_at, prepare_seconds, error, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, ?, ?, 0, 0, 0, 0, 0, '', ?)
                    """,
                    (
                        task_id,
                        attempt,
                        PreparedSandboxStatus.RESERVED.value,
                        runner_id,
                        depth.value,
                        provider,
                        template,
                        token,
                        now,
                        now,
                        expires_at,
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
        return self.get(task_id=task_id, attempt=attempt)

    @_synchronized_store
    def control_blocks_preparation(self) -> bool:
        """Return whether batch-level flags currently block new preparation.

        Returns:
            ``True`` when pause, drain, or cancel flags should stop preparing
            new sandbox capacity.
        """

        return self._control_blocks_preparation()

    @_synchronized_store
    def mark_preparing(self, record: PreparedSandboxRecord) -> PreparedSandboxRecord | None:
        """Move a reserved row to preparing.

        Args:
            record: Reservation row returned by ``reserve``.

        Returns:
            Updated row, or ``None`` when compare-and-swap ownership failed.
        """

        return self._update_status(
            record,
            PreparedSandboxStatus.PREPARING,
            fields={"prepare_started_at": time.time()},
            current_statuses={PreparedSandboxStatus.RESERVED},
        )

    @_synchronized_store
    def mark_prepared(
        self,
        record: PreparedSandboxRecord,
        *,
        sandbox_id: str,
        device_id: str,
        lease_id: str,
        prepare_seconds: float,
    ) -> PreparedSandboxRecord | None:
        """Persist a fully prepared sandbox lease.

        Args:
            record: Preparing row being finalized.
            sandbox_id: Provider sandbox id.
            device_id: Device worker id.
            lease_id: Internal sandbox lease id.
            prepare_seconds: Wall-clock seconds spent preparing this sandbox.

        Returns:
            Prepared row, or ``None`` if the row changed concurrently.
        """

        return self._update_status(
            record,
            PreparedSandboxStatus.PREPARED,
            fields={
                "sandbox_id": sandbox_id,
                "device_id": device_id,
                "lease_id": lease_id,
                "prepared_at": time.time(),
                "prepare_seconds": prepare_seconds,
            },
            current_statuses={PreparedSandboxStatus.PREPARING},
        )

    @_synchronized_store
    def mark_failed(self, record: PreparedSandboxRecord, *, error: str) -> PreparedSandboxRecord | None:
        """Record prepare failure without consuming the task retry budget.

        Args:
            record: Reserved or preparing row that failed.
            error: Operator-facing failure reason.

        Returns:
            Failed row, or ``None`` if the row no longer accepts failure.
        """

        return self._update_status(
            record,
            PreparedSandboxStatus.FAILED,
            fields={"error": error[:2000]},
            current_statuses={
                PreparedSandboxStatus.RESERVED,
                PreparedSandboxStatus.PREPARING,
                PreparedSandboxStatus.PREPARED,
                PreparedSandboxStatus.ASSIGNED,
            },
        )

    @_synchronized_store
    def assign(
        self,
        *,
        task_id: str,
        attempt: int,
        runner_id: str,
        min_remaining_ttl_seconds: float = 0.0,
    ) -> PreparedSandboxRecord | None:
        """Assign a ready prepared sandbox to the runner that reserved it.

        Args:
            task_id: Batch task id being claimed.
            attempt: One-based attempt number being claimed.
            runner_id: Runner id that originally reserved the prepared row.
            min_remaining_ttl_seconds: Optional guard requiring the prepared
                row's original TTL to remain live for at least this long.

        Returns:
            Assigned row, or ``None`` when the row is not ready/owned/live.
        """

        now = time.time()
        min_expires_at = now + max(min_remaining_ttl_seconds, 0.0)
        with _SQLiteTransaction(self._connection):
            row = self._connection.execute(
                """
                SELECT *
                FROM prepared_sandboxes
                WHERE task_id=? AND attempt=? AND status=? AND runner_id=? AND expires_at>?
                """,
                (task_id, attempt, PreparedSandboxStatus.PREPARED.value, runner_id, min_expires_at),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                """
                UPDATE prepared_sandboxes
                SET status=?, assigned_at=?, updated_at=?
                WHERE task_id=? AND attempt=? AND status=? AND runner_id=?
                """,
                (
                    PreparedSandboxStatus.ASSIGNED.value,
                    now,
                    now,
                    task_id,
                    attempt,
                    PreparedSandboxStatus.PREPARED.value,
                    runner_id,
                ),
            )
        return self.get(task_id=task_id, attempt=attempt)

    @_synchronized_store
    def assignable(self, *, task_id: str, attempt: int, runner_id: str) -> bool:
        """Return whether a prepared row is currently ready to assign.

        Args:
            task_id: Batch task id.
            attempt: One-based attempt number.
            runner_id: Runner that owns the prepared row.

        Returns:
            ``True`` when a non-expired prepared row can be assigned.
        """

        now = time.time()
        row = self._connection.execute(
            """
            SELECT 1
            FROM prepared_sandboxes
            WHERE task_id=? AND attempt=? AND status=? AND runner_id=? AND expires_at>?
            """,
            (task_id, attempt, PreparedSandboxStatus.PREPARED.value, runner_id, now),
        ).fetchone()
        return row is not None

    @_synchronized_store
    def mark_released(self, record: PreparedSandboxRecord) -> PreparedSandboxRecord | None:
        """Mark an assigned prepared sandbox as released.

        Args:
            record: Assigned row whose sandbox was released by normal teardown.

        Returns:
            Released row, or ``None`` if the row was no longer assigned.
        """

        return self._update_status(
            record,
            PreparedSandboxStatus.RELEASED,
            fields={"released_at": time.time()},
            current_statuses={PreparedSandboxStatus.ASSIGNED, PreparedSandboxStatus.CLEANUP_DEFERRED},
        )

    @_synchronized_store
    def mark_cleanup_deferred(self, record: PreparedSandboxRecord, *, error: str) -> PreparedSandboxRecord | None:
        """Track an assigned row whose release waits for controller reconcile.

        Deferred rows still count against prepared live capacity and still block
        duplicate preparation for the same task attempt, but they are no longer
        silent ``assigned`` rows.
        """

        return self._update_status(
            record,
            PreparedSandboxStatus.CLEANUP_DEFERRED,
            fields={"error": error[:2000]},
            current_statuses={PreparedSandboxStatus.ASSIGNED, PreparedSandboxStatus.CLEANUP_DEFERRED},
        )

    @_synchronized_store
    def mark_canceled(self, record: PreparedSandboxRecord) -> PreparedSandboxRecord | None:
        """Mark an unassigned prepared sandbox as canceled during shutdown.

        Args:
            record: Active unassigned row being cleaned up.

        Returns:
            Canceled row, or ``None`` if it was already assigned or terminal.
        """

        return self._update_status(
            record,
            PreparedSandboxStatus.CANCELED,
            current_statuses={
                PreparedSandboxStatus.RESERVED,
                PreparedSandboxStatus.PREPARING,
                PreparedSandboxStatus.PREPARED,
            },
        )

    @_synchronized_store
    def mark_cleanup_failed(
        self,
        record: PreparedSandboxRecord,
        *,
        error: str,
        sandbox_id: str = "",
        device_id: str = "",
        lease_id: str = "",
    ) -> PreparedSandboxRecord | None:
        """Record that a prepared sandbox still needs janitor cleanup.

        Args:
            record: Prepared row whose provider release failed.
            error: Redacted cleanup failure summary.
            sandbox_id: Provider sandbox id when cleanup failed before the row
                reached prepared state.
            device_id: Device worker id when known.
            lease_id: Internal lease id when known.

        Returns:
            Updated row, or ``None`` if ownership compare-and-swap failed.
        """

        fields: dict[str, object] = {"error": error[:2000]}
        if sandbox_id:
            fields["sandbox_id"] = sandbox_id
        if device_id:
            fields["device_id"] = device_id
        if lease_id:
            fields["lease_id"] = lease_id
        return self._update_status(
            record,
            PreparedSandboxStatus.CLEANUP_FAILED,
            fields=fields,
            current_statuses={
                PreparedSandboxStatus.RESERVED,
                PreparedSandboxStatus.PREPARING,
                PreparedSandboxStatus.PREPARED,
                PreparedSandboxStatus.ASSIGNED,
                PreparedSandboxStatus.CLEANUP_DEFERRED,
                PreparedSandboxStatus.EXPIRED,
                PreparedSandboxStatus.CANCELED,
            },
        )

    @_synchronized_store
    def expire_old(self) -> list[PreparedSandboxRecord]:
        """Expire active rows whose TTL elapsed.

        Returns:
            Rows that were active and are now marked expired.
        """

        now = time.time()
        with _SQLiteTransaction(self._connection):
            expired = [
                self._record_from_row(row)
                for row in self._connection.execute(
                    """
                    SELECT *
                    FROM prepared_sandboxes
                    WHERE status IN ({placeholders}) AND expires_at<=?
                    """.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
                    (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now),
                ).fetchall()
            ]
            self._expire_locked(now)
        return expired

    @_synchronized_store
    def expire_stale(self, *, stale_after_seconds: float) -> list[PreparedSandboxRecord]:
        """Expire unassigned rows not refreshed by a live prepared manager.

        Args:
            stale_after_seconds: Liveness grace since the last ``updated_at``.

        Returns:
            Rows that were marked expired because their runner heartbeat went stale.
        """

        if stale_after_seconds <= 0:
            return []
        now = time.time()
        cutoff = now - stale_after_seconds
        with _SQLiteTransaction(self._connection):
            stale = [
                self._record_from_row(row)
                for row in self._connection.execute(
                    """
                    SELECT *
                    FROM prepared_sandboxes
                    WHERE status IN ({placeholders}) AND expires_at>? AND updated_at<=?
                    """.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
                    (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now, cutoff),
                ).fetchall()
            ]
            self._connection.execute(
                """
                UPDATE prepared_sandboxes
                SET status=?, updated_at=?, error=CASE WHEN error='' THEN 'prepared sandbox runner heartbeat stale' ELSE error END
                WHERE status IN ({placeholders}) AND expires_at>? AND updated_at<=?
                """.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
                (PreparedSandboxStatus.EXPIRED.value, now, *sorted(_UNASSIGNED_ACTIVE_STATUSES), now, cutoff),
            )
        return stale

    @_synchronized_store
    def get(self, *, task_id: str, attempt: int) -> PreparedSandboxRecord | None:
        """Return the prepared row for one task attempt.

        Args:
            task_id: Batch task id.
            attempt: One-based attempt number.

        Returns:
            Matching row if present.
        """

        row = self._connection.execute(
            "SELECT * FROM prepared_sandboxes WHERE task_id=? AND attempt=?",
            (task_id, attempt),
        ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def _control_blocks_preparation(self) -> bool:
        try:
            rows = self._connection.execute("SELECT name, value FROM batch_control").fetchall()
        except sqlite3.Error:
            return False
        flags = {str(row["name"]): str(row["value"]) == "1" for row in rows}
        return bool(flags.get("paused") or flags.get("drain_requested") or flags.get("cancel_requested"))

    def _active_count_locked(self, now: float, *, runner_id: str = "") -> int:
        runner_clause = " AND runner_id=?" if runner_id else ""
        runner_params: tuple[object, ...] = (runner_id,) if runner_id else ()
        row = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM prepared_sandboxes
            WHERE status IN ({placeholders}) AND expires_at>?
            {runner_clause}
            """.format(
                placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES),
                runner_clause=runner_clause,
            ),
            (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now, *runner_params),
        ).fetchone()
        return int(row[0] if row else 0)

    def _live_count_locked(self, now: float, *, runner_id: str = "") -> int:
        runner_clause = " AND runner_id=?" if runner_id else ""
        runner_params: tuple[object, ...] = (runner_id,) if runner_id else ()
        row = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM prepared_sandboxes
            WHERE (
                status IN ({unassigned_placeholders}) AND expires_at>?
                OR status IN ({assigned_placeholders})
            )
            {runner_clause}
            """.format(
                unassigned_placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES),
                assigned_placeholders=",".join("?" for _ in _ASSIGNED_LIVE_STATUSES),
                runner_clause=runner_clause,
            ),
            (*sorted(_UNASSIGNED_ACTIVE_STATUSES), now, *sorted(_ASSIGNED_LIVE_STATUSES), *runner_params),
        ).fetchone()
        return int(row[0] if row else 0)

    def _count_statuses_locked(
        self,
        statuses: set[str],
        *,
        runner_clause: str = "",
        runner_params: tuple[object, ...] = (),
    ) -> int:
        if not statuses:
            return 0
        row = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM prepared_sandboxes
            WHERE status IN ({placeholders})
            {runner_clause}
            """.format(placeholders=",".join("?" for _ in statuses), runner_clause=runner_clause),
            (*sorted(statuses), *runner_params),
        ).fetchone()
        return int(row[0] if row else 0)

    def _assigned_terminal_count_locked(
        self,
        *,
        runner_clause: str = "",
        runner_params: tuple[object, ...] = (),
    ) -> int:
        if not self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_tasks'"
        ).fetchone():
            return 0
        row = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM prepared_sandboxes p
            JOIN batch_tasks t ON t.task_id=p.task_id
            WHERE p.status IN ({assigned_placeholders})
              AND t.status IN ({terminal_placeholders})
            {runner_clause}
            """.format(
                assigned_placeholders=",".join("?" for _ in _ASSIGNED_LIVE_STATUSES),
                terminal_placeholders=",".join("?" for _ in _TERMINAL_TASK_STATUSES),
                runner_clause=runner_clause.replace("runner_id", "p.runner_id"),
            ),
            (*sorted(_ASSIGNED_LIVE_STATUSES), *sorted(_TERMINAL_TASK_STATUSES), *runner_params),
        ).fetchone()
        return int(row[0] if row else 0)

    def _update_status(
        self,
        record: PreparedSandboxRecord,
        status: PreparedSandboxStatus,
        *,
        fields: dict[str, object] | None = None,
        current_statuses: set[PreparedSandboxStatus],
    ) -> PreparedSandboxRecord | None:
        now = time.time()
        updates = {"status": status.value, "updated_at": now, **(fields or {})}
        assignments = ", ".join(f"{name}=?" for name in updates)
        params = [*updates.values(), record.task_id, record.attempt, record.reservation_token]
        params.extend(sorted(item.value for item in current_statuses))
        with _SQLiteTransaction(self._connection):
            cursor = self._connection.execute(
                f"""
                UPDATE prepared_sandboxes
                SET {assignments}
                WHERE task_id=? AND attempt=? AND reservation_token=?
                  AND status IN ({",".join("?" for _ in current_statuses)})
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                return None
        return self.get(task_id=record.task_id, attempt=record.attempt)

    def _expire_locked(self, now: float) -> None:
        self._connection.execute(
            """
            UPDATE prepared_sandboxes
            SET status=?, updated_at=?, error=CASE WHEN error='' THEN 'prepared sandbox TTL expired' ELSE error END
            WHERE status IN ({placeholders}) AND expires_at<=?
            """.format(placeholders=",".join("?" for _ in _UNASSIGNED_ACTIVE_STATUSES)),
            (PreparedSandboxStatus.EXPIRED.value, now, *sorted(_UNASSIGNED_ACTIVE_STATUSES), now),
        )

    def _create_schema(self) -> None:
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS prepared_sandboxes (
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                runner_id TEXT NOT NULL,
                depth TEXT NOT NULL,
                provider TEXT NOT NULL,
                template TEXT NOT NULL,
                sandbox_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                reservation_token TEXT NOT NULL,
                reserved_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                prepare_started_at REAL NOT NULL,
                prepared_at REAL NOT NULL,
                assigned_at REAL NOT NULL,
                released_at REAL NOT NULL,
                prepare_seconds REAL NOT NULL,
                error TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(task_id, attempt)
            )
            """)
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_status ON prepared_sandboxes(status, expires_at)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_runner ON prepared_sandboxes(runner_id, status)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_task_status "
            "ON prepared_sandboxes(task_id, attempt, status)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_sandbox ON prepared_sandboxes(sandbox_id)"
        )
        if self._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_tasks'").fetchone():
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_claim_order ON batch_tasks(cancel_requested, order_index)"
            )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> PreparedSandboxRecord:
        metadata_raw = str(row["metadata_json"] or "{}")
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = {}
        return PreparedSandboxRecord(
            task_id=str(row["task_id"]),
            attempt=int(row["attempt"]),
            status=PreparedSandboxStatus(str(row["status"])),
            runner_id=str(row["runner_id"]),
            depth=PreparedSandboxDepth(str(row["depth"])),
            provider=str(row["provider"]),
            template=str(row["template"]),
            sandbox_id=str(row["sandbox_id"]),
            device_id=str(row["device_id"]),
            lease_id=str(row["lease_id"]),
            reservation_token=str(row["reservation_token"]),
            reserved_at=float(row["reserved_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"]),
            prepare_started_at=float(row["prepare_started_at"]),
            prepared_at=float(row["prepared_at"]),
            assigned_at=float(row["assigned_at"]),
            released_at=float(row["released_at"]),
            prepare_seconds=float(row["prepare_seconds"]),
            error=str(row["error"]),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


class _SQLiteTransaction:
    """Small BEGIN IMMEDIATE transaction helper for prepared rows."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()


def _task_row_can_be_prepared(
    row: sqlite3.Row | None,
    *,
    attempt: int,
    now: float,
    max_attempts_by_task: dict[str, int] | None,
) -> bool:
    if row is None:
        return False
    if int(row["cancel_requested"] or 0):
        return False
    status = str(row["status"])
    if status not in _CLAIMABLE_TASK_STATUSES:
        return False
    if str(row["lease_token"] or "") and float(row["lease_expires_at"] or 0) > now:
        return False
    if str(row["controller_task_id"] or ""):
        return False
    attempts = int(row["attempts"] or 0)
    if attempts + 1 != attempt:
        return False
    budget = (max_attempts_by_task or {}).get(str(row["task_id"]))
    if budget is not None and attempts >= budget:
        return False
    return not _row_blocks_retry(row)


def _row_blocks_retry(row: sqlite3.Row) -> bool:
    try:
        payload = json.loads(str(row["state_json"] or "{}"))
    except json.JSONDecodeError:
        return False
    return terminal_decision_blocks_retry(
        status=str(row["status"]),
        terminal_decision=payload.get("terminal_decision"),
        last_failure_category=payload.get("last_failure_category") or row["last_failure_category"],
        recovery_action=_valid_recovery_action_from_values(payload.get("recovery_action"), row["recovery_action"]),
    )
