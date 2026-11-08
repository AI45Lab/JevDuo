"""Status and live-inspection helpers for experiment operations."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.batch.contracts import BatchTaskStatus
from northcua.batch.state import SQLiteBatchStateRepository
from northcua.controller.storage import SQLiteControllerStore


class ExperimentOperationsStatusMixin:
    """Status, report, audit, and live-state projections."""

    def status(self, *, experiment_id: str, output_dir: Path) -> dict[str, object]:
        """Read the latest experiment summary or return a missing marker.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Parsed summary JSON or a small missing-status payload.
        """

        summary_path = output_dir / experiment_id / "experiment_summary.json"
        if not summary_path.exists():
            live_status = self._live_status(experiment_id=experiment_id, output_dir=output_dir)
            if live_status is not None:
                return live_status
            return {"experiment_id": experiment_id, "status": "missing", "summary_path": str(summary_path)}
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def report(self, *, experiment_id: str, output_dir: Path) -> dict[str, object]:
        """Read the persisted experiment summary.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Parsed summary JSON.

        Raises:
            FileNotFoundError: If the experiment has no summary yet.
        """

        summary_path = output_dir / experiment_id / "experiment_summary.json"
        if not summary_path.exists():
            raise FileNotFoundError(f"experiment summary not found: {summary_path}")
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def monitor(self, *, experiment_id: str, output_dir: Path) -> dict[str, object]:
        """Read latest experiment telemetry for operators.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Latest live status plus telemetry file locations.
        """

        status = self.status(experiment_id=experiment_id, output_dir=output_dir)
        experiment_dir = output_dir / experiment_id
        batch_dir = experiment_dir / "batch" / experiment_id
        status["telemetry_paths"] = {
            "events": str(batch_dir / "telemetry_events.jsonl"),
            "snapshots": str(batch_dir / "case_snapshots.jsonl"),
            "timelines": str(batch_dir / "case_timelines.jsonl"),
            "metrics": str(batch_dir / "case_metrics.jsonl"),
            "summary": str(batch_dir / "telemetry_summary.json"),
            "failure_breakdown": str(batch_dir / "failure_breakdown.json"),
            "audit_report": str(batch_dir / "audit_report.md"),
        }
        return status

    def audit(self, *, experiment_id: str, output_dir: Path, output_format: str = "json") -> dict[str, object] | str:
        """Read the final telemetry audit report.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.
            output_format: ``json`` for structured summary or ``md`` for the
                human-readable markdown report.

        Returns:
            Structured audit payload or markdown text.

        Raises:
            FileNotFoundError: If the requested audit output is missing.
        """

        batch_dir = output_dir / experiment_id / "batch" / experiment_id
        if output_format == "md":
            return (batch_dir / "audit_report.md").read_text(encoding="utf-8")
        summary_path = batch_dir / "telemetry_summary.json"
        metrics_path = batch_dir / "case_metrics.jsonl"
        failure_path = batch_dir / "failure_breakdown.json"
        return {
            "experiment_id": experiment_id,
            "telemetry_summary": json.loads(summary_path.read_text(encoding="utf-8")),
            "case_metrics": [
                json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()
            ],
            "failure_breakdown": json.loads(failure_path.read_text(encoding="utf-8")),
            "paths": {
                "summary": str(summary_path),
                "case_metrics": str(metrics_path),
                "failure_breakdown": str(failure_path),
                "audit_report": str(batch_dir / "audit_report.md"),
            },
        }

    @staticmethod
    def _live_status(*, experiment_id: str, output_dir: Path) -> dict[str, object] | None:
        experiment_dir = output_dir / experiment_id
        controller_db_path = experiment_dir / "controller.sqlite"
        if controller_db_path.exists():
            store = SQLiteControllerStore(controller_db_path)
            try:
                snapshot_rows = store.case_snapshot_rows(experiment_id=experiment_id)
                if snapshot_rows:
                    snapshots = [json.loads(str(row["payload_json"])) for row in snapshot_rows]
                    running = sum(
                        1
                        for snapshot in snapshots
                        if snapshot.get("status") not in {"succeeded", "failed", "timed_out", "canceled"}
                    )
                    return {
                        "experiment_id": experiment_id,
                        "status": "running" if running else "incomplete",
                        "summary_path": str(experiment_dir / "experiment_summary.json"),
                        "controller_db_path": str(controller_db_path),
                        "total": len(snapshots),
                        "succeeded": sum(1 for snapshot in snapshots if snapshot.get("status") == "succeeded"),
                        "failed": sum(1 for snapshot in snapshots if snapshot.get("status") == "failed"),
                        "timed_out": sum(1 for snapshot in snapshots if snapshot.get("status") == "timed_out"),
                        "canceled": sum(1 for snapshot in snapshots if snapshot.get("status") == "canceled"),
                        "running": running,
                        "case_snapshots": snapshots,
                    }
            finally:
                store.close()
        sqlite_path = experiment_dir / "batch" / experiment_id / "batch_state.sqlite"
        if not sqlite_path.exists():
            return None
        states = SQLiteBatchStateRepository(sqlite_path).all()
        succeeded = sum(1 for state in states if state.status == BatchTaskStatus.SUCCEEDED)
        failed = sum(1 for state in states if state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT})
        canceled = sum(1 for state in states if state.status == BatchTaskStatus.CANCELED)
        running = sum(
            1
            for state in states
            if state.status
            in {
                BatchTaskStatus.STARTING_SANDBOX,
                BatchTaskStatus.BOOTSTRAPPING_DEVICE,
                BatchTaskStatus.WAITING_DEVICE,
                BatchTaskStatus.SUBMITTED,
                BatchTaskStatus.RUNNING,
            }
        )
        return {
            "experiment_id": experiment_id,
            "status": "running" if running else "incomplete",
            "summary_path": str(experiment_dir / "experiment_summary.json"),
            "batch_state_path": str(sqlite_path),
            "total": len(states),
            "succeeded": succeeded,
            "failed": failed,
            "canceled": canceled,
            "running": running,
            "task_states": [state.model_dump(mode="json") for state in states],
        }
