"""Read-only telemetry projections for controller monitor clients."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.payloads import TelemetrySubscribeRequest, TelemetryUpdate

_TELEMETRY_READ_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="northcua-controller-telemetry-read",
)


class ControllerTelemetryReader:
    """Build redacted telemetry updates from controller persistence."""

    def __init__(self, store: SQLiteControllerStore) -> None:
        self.store = store

    def update(self, request: TelemetrySubscribeRequest) -> TelemetryUpdate:
        """Return one snapshot-plus-events telemetry update.

        Args:
            request: Monitor subscription request.

        Returns:
            Telemetry update matching the request filters.
        """

        case_ids = set(request.case_ids)
        event_types = set(request.event_types)
        events = []
        next_event_id = request.since_event_id
        for row in self.store.telemetry_event_rows(
            experiment_id=request.experiment_id,
            batch_id=request.batch_id,
            case_ids=case_ids,
            event_types=event_types,
            since_event_id=request.since_event_id,
            limit=request.limit,
        ):
            event_id = int(row["event_id"])
            next_event_id = max(next_event_id, event_id)
            events.append(_payload(row))
        matching_snapshots = []
        for row in self.store.case_snapshot_rows(
            experiment_id=request.experiment_id,
            batch_id=request.batch_id,
        ):
            if case_ids and str(row["case_id"]) not in case_ids:
                continue
            matching_snapshots.append(_payload(row))
        snapshots = []
        if request.include_snapshot:
            snapshots = matching_snapshots
        return TelemetryUpdate(
            events=events,
            case_snapshots=snapshots,
            summary=_summary(matching_snapshots),
            next_event_id=next_event_id,
        )

    async def update_async(self, request: TelemetrySubscribeRequest) -> TelemetryUpdate:
        """Build a telemetry snapshot without blocking the websocket event loop."""

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _TELEMETRY_READ_EXECUTOR,
            _update_from_db,
            str(self.store.path),
            request,
        )


def _update_from_db(db_path: str, request: TelemetrySubscribeRequest) -> TelemetryUpdate:
    store = SQLiteControllerStore(db_path)
    try:
        return ControllerTelemetryReader(store).update(request)
    finally:
        store.close()


def _payload(row: sqlite3.Row) -> dict[str, object]:
    row_dict = dict(row)
    raw = row_dict.get("payload_json")
    if isinstance(raw, str) and raw:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if "event_id" in row_dict:
                parsed["event_id"] = row_dict["event_id"]
            return parsed
    return row_dict


def _summary(snapshots: list[dict[str, object]]) -> dict[str, object]:
    counts = Counter(str(snapshot.get("status", "")) for snapshot in snapshots)
    terminal = sum(counts[status] for status in ("succeeded", "failed", "timed_out", "canceled"))
    total = len(snapshots)
    return {
        "total_cases": total,
        "running": total - terminal,
        "succeeded": counts["succeeded"],
        "failed": counts["failed"],
        "timed_out": counts["timed_out"],
        "canceled": counts["canceled"],
    }
