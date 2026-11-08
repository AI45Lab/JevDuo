"""Input sources for telemetry report aggregation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from northcua.telemetry.contracts import CaseTelemetrySnapshot, PhaseTiming
from northcua.telemetry.recorder import snapshot_from_batch_state
from northcua.telemetry.report_store import TelemetryReportStore

if TYPE_CHECKING:
    from northcua.batch.state import BatchStateRepository


class CaseSnapshotSource:
    """Load latest case snapshots, with SQLite state taking precedence."""

    def __init__(self, *, output_dir: Path, state_repository: "BatchStateRepository | None") -> None:
        """Create a source for per-case state snapshots.

        Args:
            output_dir: Batch output directory containing telemetry projections.
            state_repository: Optional authoritative SQLite state repository.
        """

        self.output_dir = output_dir
        self.store = TelemetryReportStore(output_dir)
        self.state_repository = state_repository

    def latest(self) -> dict[str, CaseTelemetrySnapshot]:
        """Return one latest snapshot per case.

        Returns:
            Snapshots keyed by case id. When SQLite state is available, the
            returned status and terminal fields come from SQLite.
        """

        snapshots = self._telemetry_snapshots()
        if self.state_repository is None:
            return snapshots
        batch_id = self._first_snapshot_value(snapshots, "batch_id") or self.output_dir.name
        experiment_id = self._first_snapshot_value(snapshots, "experiment_id") or batch_id
        state_snapshots: dict[str, CaseTelemetrySnapshot] = {}
        for state in self.state_repository.all():
            previous = snapshots.get(state.task_id)
            observed_at = previous.updated_at if previous is not None else ""
            projected = snapshot_from_batch_state(
                state,
                batch_id=batch_id,
                experiment_id=experiment_id,
                previous=previous,
                observed_at=observed_at,
            )
            if previous is not None and previous.metrics:
                projected = projected.model_copy(
                    update={"metrics": {**previous.metrics, **projected.metrics}},
                )
            state_snapshots[state.task_id] = projected
        return state_snapshots

    def _telemetry_snapshots(self) -> dict[str, CaseTelemetrySnapshot]:
        snapshots: dict[str, CaseTelemetrySnapshot] = {}
        for row in self.store.read_jsonl("case_snapshots.jsonl"):
            try:
                snapshot = CaseTelemetrySnapshot.model_validate(row)
            except ValidationError:
                continue
            snapshots[snapshot.case_id] = snapshot
        return snapshots

    @staticmethod
    def _first_snapshot_value(snapshots: dict[str, CaseTelemetrySnapshot], field: str) -> str:
        for snapshot in snapshots.values():
            value = getattr(snapshot, field, "")
            if isinstance(value, str) and value:
                return value
        return ""


class CasePhaseTimingSource:
    """Load phase timings keyed by case id."""

    def __init__(self, output_dir: Path) -> None:
        """Create a timing source for one batch output directory.

        Args:
            output_dir: Batch output directory containing ``case_timelines.jsonl``.
        """

        self.store = TelemetryReportStore(output_dir)

    def by_case(self) -> dict[str, list[PhaseTiming]]:
        """Return phase timings grouped by case id.

        Returns:
            Mapping from case id to ordered phase timing rows.
        """

        timings: dict[str, list[PhaseTiming]] = {}
        for row in self.store.read_jsonl("case_timelines.jsonl"):
            try:
                timing = PhaseTiming.model_validate(row)
            except ValidationError:
                continue
            timings.setdefault(timing.case_id, []).append(timing)
        return timings
