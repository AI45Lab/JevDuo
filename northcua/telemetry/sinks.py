"""Telemetry sink implementations for local files and controller SQLite."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from northcua.telemetry.background_jsonl import BackgroundJsonlWriter
from northcua.telemetry.contracts import CaseTelemetrySnapshot, PhaseTiming, TelemetryEvent

logger = logging.getLogger(__name__)


class TelemetrySink(Protocol):
    """Persistence boundary for typed telemetry projections."""

    def append_event(self, event: TelemetryEvent) -> None:
        """Append one telemetry event.

        Args:
            event: Typed event to persist.
        """

    def upsert_snapshot(self, snapshot: CaseTelemetrySnapshot) -> None:
        """Persist the latest case snapshot.

        Args:
            snapshot: Latest case projection.
        """

    def record_phase(self, timing: PhaseTiming) -> None:
        """Persist one measured phase timing.

        Args:
            timing: Case phase timing to persist.
        """

    def flush(self) -> None:
        """Flush buffered telemetry, if any."""


class LocalTelemetrySink:
    """Append telemetry projections to JSONL files in a run output directory."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = output_dir / "telemetry_events.jsonl"
        self.snapshots_path = output_dir / "case_snapshots.jsonl"
        self.timings_path = output_dir / "case_timelines.jsonl"
        self._next_event_id = self._initial_next_event_id()

    def append_event(self, event: TelemetryEvent) -> None:
        """Append one event JSON row.

        Args:
            event: Typed event to persist.
        """

        event_id = event.event_id or self._next_event_id
        self._next_event_id = max(self._next_event_id, event_id + 1)
        _append_jsonl(self.events_path, event.model_copy(update={"event_id": event_id}).model_dump(mode="json"))

    def upsert_snapshot(self, snapshot: CaseTelemetrySnapshot) -> None:
        """Append the latest snapshot JSON row.

        The file is append-only so operators can inspect how a case changed
        over time; final report aggregation keeps only the last row per case.

        Args:
            snapshot: Latest case projection.
        """

        _append_jsonl(self.snapshots_path, snapshot.model_dump(mode="json"))

    def record_phase(self, timing: PhaseTiming) -> None:
        """Append one phase timing JSON row.

        Args:
            timing: Case phase timing to persist.
        """

        _append_jsonl(self.timings_path, timing.model_dump(mode="json"))

    def flush(self) -> None:
        """Flush local telemetry.

        Local telemetry writes are unbuffered per row, so this is intentionally
        a no-op used to satisfy the common sink protocol.
        """

    def _initial_next_event_id(self) -> int:
        if not self.events_path.exists():
            return 1
        max_event_id = 0
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            value = row.get("event_id")
            if isinstance(value, int):
                max_event_id = max(max_event_id, value)
        return max_event_id + 1


class BufferedLocalTelemetrySink:
    """Local JSONL telemetry sink with background file appends.

    Event ids are still assigned synchronously in the caller so downstream
    reports keep deterministic ordering, while the open/write/flush work moves
    off the event loop.
    """

    def __init__(self, output_dir: Path, *, writer: BackgroundJsonlWriter | None = None) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = output_dir / "telemetry_events.jsonl"
        self.snapshots_path = output_dir / "case_snapshots.jsonl"
        self.timings_path = output_dir / "case_timelines.jsonl"
        self._writer = writer or BackgroundJsonlWriter(name="northcua-local-telemetry-writer")
        self._owns_writer = writer is None
        self._next_event_id = self._initial_next_event_id()
        self._closed = False

    def append_event(self, event: TelemetryEvent) -> None:
        """Append one event JSON row.

        Args:
            event: Telemetry event to append to the local JSONL stream.
        """

        if self._closed:
            return
        event_id = event.event_id or self._next_event_id
        self._next_event_id = max(self._next_event_id, event_id + 1)
        self._writer.append_json(
            self.events_path,
            event.model_copy(update={"event_id": event_id}).model_dump(mode="json"),
        )

    def upsert_snapshot(self, snapshot: CaseTelemetrySnapshot) -> None:
        """Append the latest case snapshot JSON row.

        Args:
            snapshot: Case snapshot to append for downstream latest-state
                reconstruction.
        """

        if self._closed:
            return
        self._writer.append_json(self.snapshots_path, snapshot.model_dump(mode="json"))

    def record_phase(self, timing: PhaseTiming) -> None:
        """Append one phase timing JSON row.

        Args:
            timing: Phase timing record to append to the local JSONL stream.
        """

        if self._closed:
            return
        self._writer.append_json(self.timings_path, timing.model_dump(mode="json"))

    def flush(self) -> None:
        """Drain and close owned background telemetry resources."""

        if self._closed:
            return
        self._closed = True
        if self._owns_writer:
            self._writer.close()
        else:
            self._writer.flush()

    def _initial_next_event_id(self) -> int:
        if not self.events_path.exists():
            return 1
        max_event_id = 0
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            value = row.get("event_id")
            if isinstance(value, int):
                max_event_id = max(max_event_id, value)
        return max_event_id + 1


class CompositeTelemetrySink:
    """Fan out telemetry writes to multiple sinks."""

    def __init__(self, sinks: list[TelemetrySink]) -> None:
        self._sinks = sinks

    def append_event(self, event: TelemetryEvent) -> None:
        """Append one event to every configured sink.

        Args:
            event: Typed event to persist.
        """

        for sink in self._sinks:
            try:
                sink.append_event(event)
            except Exception:
                logger.warning("telemetry sink failed while appending event", exc_info=True)

    def upsert_snapshot(self, snapshot: CaseTelemetrySnapshot) -> None:
        """Persist a snapshot in every configured sink.

        Args:
            snapshot: Latest case projection.
        """

        for sink in self._sinks:
            try:
                sink.upsert_snapshot(snapshot)
            except Exception:
                logger.warning("telemetry sink failed while upserting snapshot", exc_info=True)

    def record_phase(self, timing: PhaseTiming) -> None:
        """Persist a phase timing in every configured sink.

        Args:
            timing: Case phase timing to persist.
        """

        for sink in self._sinks:
            try:
                sink.record_phase(timing)
            except Exception:
                logger.warning("telemetry sink failed while recording phase timing", exc_info=True)

    def flush(self) -> None:
        """Flush all configured sinks."""

        for sink in self._sinks:
            try:
                sink.flush()
            except Exception:
                logger.warning("telemetry sink failed while flushing", exc_info=True)


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
