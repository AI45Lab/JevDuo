"""Telemetry report enrichment for reusable case suites."""

from __future__ import annotations

from pathlib import Path

from northcua.batch.state import SQLiteBatchStateRepository
from northcua.experiments.cases.verification import CaseArtifactResult
from northcua.telemetry import CaseArtifactTelemetry, TelemetryReportWriter


class CaseTelemetryReportEnricher:
    """Rebuild telemetry reports with verifier-discovered artifact paths."""

    def __init__(self, batch_output_dir: Path) -> None:
        self._batch_output_dir = batch_output_dir

    def rebuild(self, artifact_results: list[CaseArtifactResult]) -> None:
        """Rebuild report projections when batch telemetry already exists.

        Args:
            artifact_results: Verifier-discovered artifact rows to merge into
                telemetry report projections.
        """

        if not self._can_rebuild_reports():
            return
        TelemetryReportWriter(self._batch_output_dir, state_repository=self._sqlite_repository()).write(
            case_artifacts=[self._artifact_telemetry(result) for result in artifact_results]
        )

    def _can_rebuild_reports(self) -> bool:
        return (self._batch_output_dir / "batch_state.sqlite").exists() or (
            self._batch_output_dir / "case_snapshots.jsonl"
        ).exists()

    def _sqlite_repository(self) -> SQLiteBatchStateRepository | None:
        sqlite_path = self._batch_output_dir / "batch_state.sqlite"
        if not sqlite_path.exists():
            return None
        return SQLiteBatchStateRepository(sqlite_path)

    def _artifact_telemetry(self, result: CaseArtifactResult) -> CaseArtifactTelemetry:
        sandbox_log_dir = self._batch_output_dir / "sandbox-logs" / result.task_id
        return CaseArtifactTelemetry(
            case_id=result.task_id,
            main_trace_dir=result.main_trace_dir,
            main_steps_path=result.main_steps_path,
            main_llm_trace_path=result.main_llm_trace_path,
            evaluator_trace_dir=result.evaluator_trace_dir,
            evaluator_report_path=result.evaluator_report_path,
            evaluator_evidence_manifest_path=result.evaluator_evidence_manifest_path,
            evaluator_llm_trace_path=result.evaluator_llm_trace_path,
            sandbox_log_dir=str(sandbox_log_dir) if sandbox_log_dir.exists() else "",
            screenshot_paths=result.screenshot_paths,
            evaluator_safety_risk_level=result.evaluator_safety_risk_level,
            evaluator_efficiency_rating=result.evaluator_efficiency_rating,
            evaluator_diagnosis_category_breakdown=result.evaluator_diagnosis_category_breakdown,
        )
