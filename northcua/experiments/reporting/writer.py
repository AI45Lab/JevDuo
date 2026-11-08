"""Machine-readable report writing for experiment runs."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.batch.contracts import BatchRunResult
from northcua.experiments.artifacts import ArtifactIndex, ArtifactIndexRow
from northcua.experiments.contracts import ExperimentRunResult, ExperimentSpec, ExperimentTaskResult
from northcua.experiments.reporting.telemetry import enrich_telemetry_reports


class ExperimentReportWriter:
    """Persist experiment summaries, assertions, and dataset indexes."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        spec: ExperimentSpec,
        batch_result: BatchRunResult,
        task_results: list[ExperimentTaskResult],
    ) -> ExperimentRunResult:
        """Write all experiment report files.

        Args:
            spec: Experiment spec that produced the run.
            batch_result: Underlying batch result.
            task_results: Verification rows to include in the dataset index.

        Returns:
            Typed run result containing report paths.
        """

        dataset_path = self.output_dir / "dataset_index.jsonl"
        artifact_index_path = self.output_dir / "artifact_index.jsonl"
        assertions_path = self.output_dir / "golden_assertions.json"
        summary_path = self.output_dir / "experiment_summary.json"
        dataset_lines = [json.dumps(row.model_dump(mode="json"), ensure_ascii=False) for row in task_results]
        dataset_path.write_text("\n".join(dataset_lines) + ("\n" if dataset_lines else ""), encoding="utf-8")
        ArtifactIndex(artifact_index_path).write(_artifact_index_rows(task_results))
        enrich_telemetry_reports(batch_output_dir=Path(batch_result.output_dir), task_results=task_results)
        assertions = {
            "experiment_id": spec.experiment_id,
            "passed": all(row.passed for row in task_results),
            "tasks": [row.model_dump(mode="json") for row in task_results],
        }
        assertions_path.write_text(json.dumps(assertions, ensure_ascii=False, indent=2), encoding="utf-8")
        result = ExperimentRunResult(
            experiment_id=spec.experiment_id,
            output_dir=str(self.output_dir),
            batch_result=batch_result,
            task_results=task_results,
            dataset_index_path=str(dataset_path),
            artifact_index_path=str(artifact_index_path),
            assertions_path=str(assertions_path),
            summary_path=str(summary_path),
            telemetry_summary_path=str(Path(batch_result.output_dir) / "telemetry_summary.json"),
            case_metrics_path=str(Path(batch_result.output_dir) / "case_metrics.jsonl"),
            case_timelines_path=str(Path(batch_result.output_dir) / "case_timelines.jsonl"),
            audit_report_path=str(Path(batch_result.output_dir) / "audit_report.md"),
        )
        summary_path.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def read_summary(self) -> dict[str, object]:
        """Read the persisted experiment summary.

        Returns:
            Parsed summary JSON.

        Raises:
            FileNotFoundError: If no summary has been written.
        """

        path = self.output_dir / "experiment_summary.json"
        return json.loads(path.read_text(encoding="utf-8"))


def _artifact_index_rows(task_results: list[ExperimentTaskResult]) -> list[ArtifactIndexRow]:
    rows: list[ArtifactIndexRow] = []
    for result in task_results:
        artifacts = {
            "main_steps": [result.main_steps_path],
            "main_llm_trace": [result.main_llm_trace_path],
            "screenshot": result.screenshot_paths,
            "evaluator_report": [result.evaluator_report_path],
            "evaluator_evidence": [result.evaluator_evidence_manifest_path],
            "evaluator_llm_trace": [result.evaluator_llm_trace_path],
        }
        for kind, paths in artifacts.items():
            for value in paths:
                path = Path(value) if value else None
                if path is None or not path.exists():
                    continue
                rows.append(
                    ArtifactIndexRow(
                        task_id=result.task_id,
                        kind=kind,
                        uri=path.resolve().as_uri(),
                        path=str(path),
                        size_bytes=path.stat().st_size,
                    )
                )
    return rows
