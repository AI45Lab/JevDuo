"""Cancellation helpers for the SQLite batch state repository."""

from __future__ import annotations

import sqlite3
import time

from northcua.batch.contracts import BatchTaskStatus
from northcua.batch.state.sqlite_helpers import _state_from_json_text, _state_update_values

_TERMINAL_STATUSES = {
    BatchTaskStatus.SUCCEEDED.value,
    BatchTaskStatus.EVALUATION_PENDING.value,
    BatchTaskStatus.CANCELED.value,
}


def mark_cancel_requested(connection: sqlite3.Connection) -> None:
    """Cancel nonterminal rows and block future scheduler claims.

    Args:
        connection: Open SQLite connection for the batch state database.
    """

    now = time.time()
    connection.execute("UPDATE batch_tasks SET cancel_requested=1, updated_at=?", (now,))
    connection.execute(
        """
        INSERT INTO batch_control(name, value, updated_at)
        VALUES ('cancel_requested', '1', ?)
        ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (now,),
    )
    rows = connection.execute(
        """
        SELECT state_json
        FROM batch_tasks
        WHERE status NOT IN ({terminal})
        """.format(terminal=",".join("?" for _ in _TERMINAL_STATUSES)),
        tuple(sorted(_TERMINAL_STATUSES)),
    ).fetchall()
    for row in rows:
        _cancel_row(connection, state_json=str(row["state_json"]), updated_at=now)


def _cancel_row(connection: sqlite3.Connection, *, state_json: str, updated_at: float) -> None:
    state = _state_from_json_text(state_json).model_copy(
        update={"status": BatchTaskStatus.CANCELED, "error": "batch canceled"}
    )
    connection.execute(
        """
        UPDATE batch_tasks
        SET state_json=?, status=?, attempts=?, controller_task_id=?,
            trace_id=?, session_id=?, sandbox_id=?, device_id=?,
            terminal_state=?, error=?, recovery_action=?,
            last_failure_category=?, last_reconciled_at=?,
            last_reconciled_status=?, cleanup_status=?,
            retry_budget_remaining=?, next_attempt_after=?, updated_at=?
        WHERE task_id=?
        """,
        (*_state_update_values(state, updated_at=updated_at), state.task_id),
    )
