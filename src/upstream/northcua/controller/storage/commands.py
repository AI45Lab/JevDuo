"""Command CRUD and idempotency helpers for controller persistence."""

from __future__ import annotations

import sqlite3
from typing import Literal

from northcua.controller.storage.identifiers import command_storage_key, utc_now_iso
from northcua.controller.storage.result_compaction import compact_command_result_for_sqlite
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.state import CommandState

_TERMINAL_COMMAND_STATE_VALUES = (
    CommandState.SUCCEEDED.value,
    CommandState.FAILED.value,
    CommandState.TIMED_OUT.value,
    CommandState.CANCELLED.value,
)


class CommandQueryMixin:
    """Read command rows by protocol id, storage key, device, session, or result payload."""

    connection: sqlite3.Connection

    def command_row(self, command_id: str, *, session_id: str = "", lease_id: str = "") -> sqlite3.Row | None:
        """Return one command row.

        Args:
            command_id: Protocol command id or controller-private storage key
                to look up.
            session_id: Optional session filter for protocol command ids.
            lease_id: Optional lease filter for protocol command ids.

        Returns:
            Matching command row, or `None`.
        """

        exact = self.connection.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
        if exact is not None:
            if session_id and str(exact["session_id"]) != session_id:
                exact = None
            elif lease_id and str(exact["lease_id"]) != lease_id:
                exact = None
            else:
                return exact
        clauses = ["protocol_command_id=?"]
        parameters = [command_id]
        if session_id:
            clauses.append("session_id=?")
            parameters.append(session_id)
        if lease_id:
            clauses.append("lease_id=?")
            parameters.append(lease_id)
        return self.connection.execute(
            f"SELECT * FROM commands WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 1",
            parameters,
        ).fetchone()

    def command_row_for_result(self, result: CommandResult) -> sqlite3.Row | None:
        """Return the command row addressed by a device result.

        Args:
            result: Device result carrying the protocol command identity.

        Returns:
            Matching command row, or `None`.
        """

        return self.connection.execute(
            "SELECT * FROM commands WHERE command_id=? AND protocol_command_id=? AND session_id=? AND lease_id=?",
            (
                command_storage_key(
                    session_id=result.session_id,
                    lease_id=result.lease_id,
                    command_id=result.command_id,
                ),
                result.command_id,
                result.session_id,
                result.lease_id,
            ),
        ).fetchone()

    def command_rows_for_device(self, device_id: str, statuses: set[CommandState]) -> list[sqlite3.Row]:
        """Return command rows for a device filtered by lifecycle state.

        Args:
            device_id: Device id to query.
            statuses: Command lifecycle states to include.

        Returns:
            Matching command rows, or an empty list when no statuses are supplied.
        """

        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.connection.execute(
                f"SELECT * FROM commands WHERE device_id=? AND status IN ({placeholders})",
                (device_id, *(status.value for status in statuses)),
            )
        )

    def command_rows_for_session(self, session_id: str, statuses: set[CommandState]) -> list[sqlite3.Row]:
        """Return command rows for a session filtered by lifecycle state.

        Args:
            session_id: Session id to query.
            statuses: Command lifecycle states to include.

        Returns:
            Matching command rows, or an empty list when no statuses are supplied.
        """

        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.connection.execute(
                f"SELECT * FROM commands WHERE session_id=? AND status IN ({placeholders})",
                (session_id, *(status.value for status in statuses)),
            )
        )


class CommandMutationMixin:
    """Persist command state transitions, idempotency claims, and results."""

    connection: sqlite3.Connection

    def update_commands_for_device(self, device_id: str, from_status: CommandState, to_status: CommandState) -> None:
        """Bulk update commands for a disconnected or drained device.

        Args:
            device_id: Device whose commands should be updated.
            from_status: Current state to match.
            to_status: New state for matching commands.
        """

        self.connection.execute(
            "UPDATE commands SET status=?, updated_at=? WHERE device_id=? AND status=?",
            (to_status.value, utc_now_iso(), device_id, from_status.value),
        )
        self.connection.commit()

    def update_commands_for_device_statuses(
        self,
        device_id: str,
        from_statuses: set[CommandState],
        to_status: CommandState,
    ) -> None:
        """Bulk update commands for a device from any matching lifecycle state.

        Args:
            device_id: Device whose commands should be updated.
            from_statuses: Current states to match.
            to_status: New terminal state for matching commands.
        """

        if not from_statuses:
            return
        placeholders = ",".join("?" for _ in from_statuses)
        self.connection.execute(
            f"UPDATE commands SET status=?, updated_at=? WHERE device_id=? AND status IN ({placeholders})",
            (to_status.value, utc_now_iso(), device_id, *(status.value for status in from_statuses)),
        )
        self.connection.commit()

    def update_commands_for_session(
        self,
        session_id: str,
        from_statuses: set[CommandState],
        to_status: CommandState,
    ) -> None:
        """Bulk update active commands when a session becomes terminal.

        Args:
            session_id: Session whose active commands should be updated.
            from_statuses: Current states to match.
            to_status: New terminal state for matching commands.
        """

        if not from_statuses:
            return
        placeholders = ",".join("?" for _ in from_statuses)
        self.connection.execute(
            f"UPDATE commands SET status=?, updated_at=? WHERE session_id=? AND status IN ({placeholders})",
            (to_status.value, utc_now_iso(), session_id, *(status.value for status in from_statuses)),
        )
        self.connection.commit()

    def claim_command(
        self, request: CommandRequest
    ) -> tuple[Literal["new", "cached", "in_flight"], CommandResult | None]:
        """Claim a command idempotency key before dispatching to a device.

        Args:
            request: Command request to insert or dedupe.

        Returns:
            Claim state plus cached result when a terminal result already exists.

        Raises:
            ValueError: If an existing command/idempotency key has different payload.
        """

        with self.connection:
            return self.claim_command_in_transaction(request)

    def claim_command_in_transaction(
        self, request: CommandRequest
    ) -> tuple[Literal["new", "cached", "in_flight"], CommandResult | None]:
        """Claim a command inside a transaction owned by the caller.

        The normal path inserts first and only reads on a uniqueness conflict.
        Callers must already hold a transaction when atomicity with authority
        checks is required.
        """

        storage_key = command_storage_key(
            session_id=request.session_id,
            lease_id=request.lease_id,
            command_id=request.command_id,
        )
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO commands(
                command_id,
                protocol_command_id,
                idempotency_key,
                session_id,
                lease_id,
                device_id,
                status,
                request_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                storage_key,
                request.command_id,
                request.idempotency_key,
                request.session_id,
                request.lease_id,
                request.device_id,
                CommandState.QUEUED.value,
                request.model_dump_json(),
                now,
                now,
            ),
        )
        if cursor.rowcount > 0:
            return "new", None

        existing = self.connection.execute(
            "SELECT request_json, result_json, status FROM commands WHERE command_id=? OR idempotency_key=?",
            (storage_key, request.idempotency_key),
        ).fetchone()
        if existing and existing["request_json"]:
            existing_request = CommandRequest.model_validate_json(existing["request_json"])
            if existing_request.model_dump(mode="json") != request.model_dump(mode="json"):
                raise ValueError("command idempotency conflict: duplicate command id or idempotency key differs")
        if existing and existing["result_json"]:
            return "cached", CommandResult.model_validate_json(existing["result_json"])
        if existing:
            terminal_statuses = {
                CommandState.SUCCEEDED.value,
                CommandState.FAILED.value,
                CommandState.TIMED_OUT.value,
                CommandState.CANCELLED.value,
            }
            if str(existing["status"]) in terminal_statuses:
                return (
                    "cached",
                    CommandResult(
                        command_id=request.command_id,
                        session_id=request.session_id,
                        lease_id=request.lease_id,
                        device_id=request.device_id,
                        lease_generation=request.lease_generation,
                        status=CommandState(str(existing["status"])),
                        error=f"command already {existing['status']} without cached result",
                    ),
                )
            return "in_flight", None
        raise RuntimeError("command insert conflicted but no existing command was found")

    def update_command_status(self, command_id: str, status: CommandState) -> None:
        """Update command lifecycle state without replacing its result.

        Args:
            command_id: Command row to update.
            status: New command lifecycle state.
        """

        self.connection.execute(
            "UPDATE commands SET status=?, updated_at=? WHERE command_id=?",
            (status.value, utc_now_iso(), command_id),
        )
        self.connection.commit()

    def transition_command_status(
        self,
        command_id: str,
        *,
        from_statuses: set[CommandState],
        to_status: CommandState,
    ) -> bool:
        """Update one command only when it is still in an expected state.

        Args:
            command_id: Controller-private command row id.
            from_statuses: Allowed source states.
            to_status: New command lifecycle state.

        Returns:
            ``True`` when the row was updated.
        """

        if not from_statuses:
            return False
        placeholders = ",".join("?" for _ in from_statuses)
        cursor = self.connection.execute(
            f"UPDATE commands SET status=?, updated_at=? WHERE command_id=? AND status IN ({placeholders})",
            (to_status.value, utc_now_iso(), command_id, *(status.value for status in from_statuses)),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def update_command_status_for_request(self, request: CommandRequest, status: CommandState) -> None:
        """Update command lifecycle state using the controller-private key.

        Args:
            request: Command request whose row should be updated.
            status: New command lifecycle state.
        """

        self.update_command_status(
            command_storage_key(
                session_id=request.session_id,
                lease_id=request.lease_id,
                command_id=request.command_id,
            ),
            status,
        )

    def mark_command_dispatched_for_request(self, request: CommandRequest) -> CommandResult | None:
        """Atomically move a claimed command from queued to running.

        Args:
            request: Command request whose row should be dispatched.

        Returns:
            ``None`` when the command is now running and can be sent to the
            device. A terminal/cached result when another lifecycle transition
            already won the race.
        """

        storage_key = command_storage_key(
            session_id=request.session_id,
            lease_id=request.lease_id,
            command_id=request.command_id,
        )
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE commands SET status=?, updated_at=? WHERE command_id=? AND status=?",
                (CommandState.RUNNING.value, utc_now_iso(), storage_key, CommandState.QUEUED.value),
            )
            if cursor.rowcount > 0:
                return None
            row = self.connection.execute(
                "SELECT status, result_json FROM commands WHERE command_id=?",
                (storage_key,),
            ).fetchone()
            return _cached_result_from_command_row(request, row)

    def record_command_result(
        self,
        result: CommandResult,
        *,
        persisted_result: CommandResult | None = None,
    ) -> CommandResult | None:
        """Persist command result and return cached result if this was duplicate.

        Args:
            result: Command result received by the controller.
            persisted_result: Optional pre-compacted SQLite-safe result. Passing
                this avoids compacting large command payloads twice when the
                caller also needs the compact artifact refs.

        Returns:
            Cached or synthetic late result when the command was already terminal.
        """

        storage_key = command_storage_key(
            session_id=result.session_id,
            lease_id=result.lease_id,
            command_id=result.command_id,
        )
        if persisted_result is None:
            persisted_result = compact_command_result_for_sqlite(result)
        persisted_json = persisted_result.model_dump_json()
        terminal_placeholders = ",".join("?" for _ in _TERMINAL_COMMAND_STATE_VALUES)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                UPDATE commands
                SET status=?, result_json=?, updated_at=?
                WHERE command_id=?
                  AND result_json IS NULL
                  AND status NOT IN ({terminal_placeholders})
                """,
                (
                    result.status.value,
                    persisted_json,
                    utc_now_iso(),
                    storage_key,
                    *_TERMINAL_COMMAND_STATE_VALUES,
                ),
            )
            if cursor.rowcount > 0:
                return None

            existing = self.connection.execute(
                "SELECT status, result_json FROM commands WHERE command_id=?",
                (storage_key,),
            ).fetchone()
            if existing and existing["result_json"]:
                return CommandResult.model_validate_json(existing["result_json"])
            if existing and existing["status"] in _TERMINAL_COMMAND_STATE_VALUES:
                if result.status.value == existing["status"]:
                    self.connection.execute(
                        "UPDATE commands SET result_json=?, updated_at=? WHERE command_id=?",
                        (persisted_json, utc_now_iso(), storage_key),
                    )
                    return None
                return CommandResult(
                    command_id=result.command_id,
                    session_id=result.session_id,
                    lease_id=result.lease_id,
                    device_id=result.device_id,
                    status=CommandState(existing["status"]),
                    error=f"late result ignored after {existing['status']}",
                )
            return None


def _cached_result_from_command_row(request: CommandRequest, row: sqlite3.Row | None) -> CommandResult:
    if row is not None and row["result_json"]:
        return CommandResult.model_validate_json(row["result_json"])
    status = CommandState(str(row["status"])) if row is not None else CommandState.FAILED
    error = f"command already {status.value} before dispatch" if row is not None else "command row missing"
    return CommandResult(
        command_id=request.command_id,
        session_id=request.session_id,
        lease_id=request.lease_id,
        device_id=request.device_id,
        lease_generation=request.lease_generation,
        status=status if status.value in _TERMINAL_COMMAND_STATE_VALUES else CommandState.FAILED,
        error=error,
    )


class CommandRepositoryMixin(CommandQueryMixin, CommandMutationMixin):
    """Persist, query, and deduplicate command rows."""
