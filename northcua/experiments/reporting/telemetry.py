"""Experiment artifact handoff for batch telemetry reports."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.batch.state import SQLiteBatchStateRepository
from northcua.experiments.contracts import ExperimentTaskResult
from northcua.telemetry import CaseArtifactTelemetry, TelemetryReportWriter


def enrich_telemetry_reports(*, batch_output_dir: Path, task_results: list[ExperimentTaskResult]) -> None:
    """Rebuild batch telemetry reports with verifier-discovered artifacts.

    Args:
        batch_output_dir: Concrete batch output directory containing telemetry
            reports and, for current runs, ``batch_state.sqlite``.
        task_results: Verifier rows with main-agent and evaluator artifact paths.
    """

    ExperimentTelemetryReportEnricher(batch_output_dir).rebuild(task_results)


class ExperimentTelemetryReportEnricher:
    """Rebuild telemetry reports with experiment-level artifact paths."""

    def __init__(self, batch_output_dir: Path) -> None:
        """Create an enricher for one batch output directory.

        Args:
            batch_output_dir: Directory containing batch reports and artifacts.
        """

        self.batch_output_dir = batch_output_dir

    def rebuild(self, task_results: list[ExperimentTaskResult]) -> None:
        """Rebuild reports when the initial case metric projection exists.

        Args:
            task_results: Verifier rows with main-agent and evaluator artifacts.
        """

        if not self._can_rebuild_reports():
            return
        repository = self._sqlite_repository()
        summary = TelemetryReportWriter(self.batch_output_dir, state_repository=repository).write(
            case_artifacts=[self._case_artifact_telemetry(result) for result in task_results]
        )
        self._rewrite_batch_summary_telemetry(summary.model_dump(mode="json"))

    def _rewrite_batch_summary_telemetry(self, summary: dict[str, object]) -> None:
        """Keep embedded batch-summary telemetry aligned with rebuilt reports."""

        summary_path = self.batch_output_dir / "batch_summary.json"
        if not summary_path.exists():
            return
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        telemetry = payload.get("telemetry")
        if not isinstance(telemetry, dict):
            telemetry = {}
        telemetry.update(
            {
                "summary_path": str(self.batch_output_dir / "telemetry_summary.json"),
                "case_metrics_path": str(self.batch_output_dir / "case_metrics.jsonl"),
                "case_timelines_path": str(self.batch_output_dir / "case_timelines.jsonl"),
                "failure_breakdown_path": str(self.batch_output_dir / "failure_breakdown.json"),
                "audit_report_path": str(self.batch_output_dir / "audit_report.md"),
                "summary": summary,
            }
        )
        payload["telemetry"] = telemetry
        tmp_path = summary_path.with_name(f".{summary_path.name}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(summary_path)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return

    def _can_rebuild_reports(self) -> bool:
        if not (self.batch_output_dir / "case_metrics.jsonl").exists():
            return False
        return (self.batch_output_dir / "batch_state.sqlite").exists() or (
            self.batch_output_dir / "case_snapshots.jsonl"
        ).exists()

    def _sqlite_repository(self) -> SQLiteBatchStateRepository | None:
        sqlite_path = self.batch_output_dir / "batch_state.sqlite"
        if not sqlite_path.exists():
            return None
        return SQLiteBatchStateRepository(sqlite_path)

    def _case_artifact_telemetry(self, result: ExperimentTaskResult) -> CaseArtifactTelemetry:
        main_trace_dir = self._parent_dir(result.main_steps_path) or self._parent_dir(result.main_llm_trace_path)
        evaluator_trace_dir = self._parent_dir(result.evaluator_report_path) or self._parent_dir(
            result.evaluator_llm_trace_path
        )
        sandbox_log_dir = self.batch_output_dir / "sandbox-logs" / result.task_id
        return CaseArtifactTelemetry(
            case_id=result.task_id,
            main_trace_dir=main_trace_dir,
            main_steps_path=result.main_steps_path,
            main_llm_trace_path=result.main_llm_trace_path,
            evaluator_trace_dir=evaluator_trace_dir,
            evaluator_report_path=result.evaluator_report_path,
            evaluator_evidence_manifest_path=result.evaluator_evidence_manifest_path,
            evaluator_llm_trace_path=result.evaluator_llm_trace_path,
            sandbox_log_dir=str(sandbox_log_dir) if sandbox_log_dir.exists() else "",
            screenshot_paths=result.screenshot_paths,
            evaluator_safety_risk_level=result.evaluator_safety_risk_level,
            evaluator_efficiency_rating=result.evaluator_efficiency_rating,
            evaluator_diagnosis_category_breakdown=result.evaluator_diagnosis_category_breakdown,
        )

    @staticmethod
    def _parent_dir(path: str) -> str:
        if not path:
            return ""
        return str(Path(path).parent)
