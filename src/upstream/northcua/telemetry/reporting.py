"""Telemetry report aggregation for batch and experiment outputs."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from northcua.telemetry.case_metrics import CaseMetricBuilder
from northcua.telemetry.contracts import CaseArtifactTelemetry, CaseMetrics, CaseStatus, ExperimentTelemetrySummary
from northcua.telemetry.report_sources import CasePhaseTimingSource, CaseSnapshotSource
from northcua.telemetry.report_store import TelemetryReportStore
from northcua.telemetry.report_values import MetricValue
from northcua.telemetry.summary_reporting import AuditMarkdownRenderer, ExperimentSummaryBuilder

if TYPE_CHECKING:
    from northcua.batch.state import BatchStateRepository

_TERMINAL_CASE_STATUSES = {
    CaseStatus.SUCCEEDED,
    CaseStatus.FAILED,
    CaseStatus.TIMED_OUT,
    CaseStatus.CANCELED,
}
_PRESERVED_ARTIFACT_METRIC_FIELDS = (
    "artifact_count",
    "screenshot_count",
    "artifact_bytes",
    "command_count",
    "tool_call_count",
    "prompt_tokens",
    "completion_tokens",
    "evaluator_seconds",
    "evaluator_safety_risk_level",
    "evaluator_safety_finding_count",
    "evaluator_efficiency_rating",
    "evaluator_diagnosis_category_breakdown",
    "evaluator_unnecessary_step_count",
    "evaluator_repeated_action_count",
    "evaluator_detour_count",
    "benchmark_score",
    "benchmark_passed",
    "benchmark_error",
    "reward_artifact_path",
    "main_trace_dir",
    "main_steps_path",
    "main_llm_trace_path",
    "evaluator_trace_dir",
    "evaluator_report_path",
    "evaluator_evidence_manifest_path",
    "evaluator_llm_trace_path",
    "sandbox_log_dir",
)


class TelemetryReportWriter:
    """Build audit reports from authoritative batch state and derived telemetry."""

    def __init__(self, output_dir: Path, *, state_repository: "BatchStateRepository | None" = None) -> None:
        """Create a telemetry report writer for one batch output directory.

        Args:
            output_dir: Batch output directory containing telemetry projections.
            state_repository: Optional SQLite state repository. When provided,
                it is the source of truth for latest per-case status.
        """

        self.store = TelemetryReportStore(output_dir)
        self.snapshot_source = CaseSnapshotSource(output_dir=output_dir, state_repository=state_repository)
        self.timing_source = CasePhaseTimingSource(output_dir)
        self.metric_builder = CaseMetricBuilder()
        self.summary_builder = ExperimentSummaryBuilder()
        self.audit_renderer = AuditMarkdownRenderer()

    def write(self, *, case_artifacts: list[CaseArtifactTelemetry] | None = None) -> ExperimentTelemetrySummary:
        """Write case metrics, failure breakdown, and markdown audit reports.

        Args:
            case_artifacts: Optional artifact path contracts discovered by a
                higher-level experiment runner.

        Returns:
            The typed summary written to ``telemetry_summary.json``.
        """

        previous_metrics = self._previous_metrics_by_case() if case_artifacts is None else {}
        snapshots = self.snapshot_source.latest()
        timings = self.timing_source.by_case()
        artifacts = {artifact.case_id: artifact for artifact in case_artifacts or []}
        manifest_metadata = self._manifest_metadata_by_case()
        prepared_metrics = self._prepared_metrics_by_case()
        snapshot_values = list(snapshots.values())
        metrics = [
            self._preserve_existing_artifact_projection(
                self.metric_builder.build(
                    snapshot,
                    timings.get(snapshot.case_id, []),
                    artifacts.get(snapshot.case_id),
                    manifest_metadata.get(snapshot.case_id),
                    prepared_metrics.get(snapshot.case_id),
                ),
                previous_metrics.get(snapshot.case_id),
            )
            for snapshot in snapshot_values
        ]
        summary = self.summary_builder.build(metrics=metrics, snapshots=snapshot_values)
        summary = summary.model_copy(update=self._prepared_summary_from_events())
        self.store.write_jsonl("case_metrics.jsonl", [metric.model_dump(mode="json") for metric in metrics])
        self.store.write_json("failure_breakdown.json", summary.failure_breakdown)
        self.store.write_json("telemetry_summary.json", summary.model_dump(mode="json"))
        self.store.write_text("audit_report.md", self.audit_renderer.render(summary=summary, metrics=metrics))
        return summary

    def _manifest_metadata_by_case(self) -> dict[str, dict[str, object]]:
        manifest_path = self.store.output_dir / "manifest.json"
        if not manifest_path.exists():
            return {}
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            return {}
        result: dict[str, dict[str, object]] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            metadata = task.get("metadata")
            if isinstance(task_id, str) and isinstance(metadata, dict):
                result[task_id] = metadata
        return result

    def _prepared_metrics_by_case(self) -> dict[str, dict[str, object]]:
        ready_prepare_seconds: dict[tuple[str, int], float] = {}
        result: dict[str, dict[str, object]] = {}
        for row in self.store.read_jsonl("telemetry_events.jsonl"):
            case_id = row.get("case_id")
            message = row.get("message")
            details = row.get("details")
            if not isinstance(case_id, str) or not isinstance(message, str) or not isinstance(details, dict):
                continue
            attempt = MetricValue.as_int(details.get("attempt"))
            key = (case_id, attempt)
            if message == "prepared_sandbox_ready":
                ready_prepare_seconds[key] = MetricValue.as_float(details.get("prepare_seconds"))
                continue
            if message == "prepared_sandbox_fallback" and details.get("reason") != "disabled":
                metrics = result.setdefault(case_id, {})
                metrics["prepared_sandbox_miss"] = True
                reason = details.get("reason")
                if isinstance(reason, str) and reason:
                    breakdown = metrics.setdefault("prepared_sandbox_miss_breakdown", {})
                    if isinstance(breakdown, dict):
                        breakdown[reason] = int(breakdown.get(reason, 0)) + 1
                continue
            if message != "prepared_sandbox_assigned":
                continue
            prepare_seconds = MetricValue.as_float(details.get("prepare_seconds")) or ready_prepare_seconds.get(
                key, 0.0
            )
            claim_wait_seconds = MetricValue.as_float(details.get("claim_wait_seconds"))
            estimated_saved = max(prepare_seconds - claim_wait_seconds, 0.0)
            metrics = result.setdefault(case_id, {})
            metrics["prepared_sandbox_hit"] = True
            metrics["prepared_sandbox_claim_wait_seconds"] = (
                MetricValue.as_float(metrics.get("prepared_sandbox_claim_wait_seconds")) + claim_wait_seconds
            )
            metrics["prepared_sandbox_estimated_saved_seconds"] = (
                MetricValue.as_float(metrics.get("prepared_sandbox_estimated_saved_seconds")) + estimated_saved
            )
        return result

    def _prepared_summary_from_events(self) -> dict[str, object]:
        miss_breakdown: Counter[str] = Counter()
        status_breakdown: Counter[str] = Counter()
        failures = 0
        cleanup_failures = 0
        for row in self.store.read_jsonl("telemetry_events.jsonl"):
            message = str(row.get("message") or "")
            details = row.get("details")
            details = details if isinstance(details, dict) else {}
            if message == "prepared_sandbox_fallback":
                reason = str(details.get("reason") or "")
                if reason and reason != "disabled":
                    miss_breakdown[reason] += 1
            elif message == "prepared_sandbox_failed":
                failures += 1
            elif message == "prepared_sandbox_cleanup_failed":
                cleanup_failures += 1
                if details.get("prepare_failed") is True:
                    failures += 1
            raw_counts = details.get("prepared_status_counts")
            if isinstance(raw_counts, dict):
                status_breakdown = Counter(
                    {str(key): int(value) for key, value in raw_counts.items() if isinstance(value, int)}
                )
        return {
            "prepared_sandbox_miss_breakdown": dict(sorted(miss_breakdown.items())),
            "prepared_sandbox_failures": failures,
            "prepared_sandbox_cleanup_failures": cleanup_failures,
            "prepared_sandbox_status_breakdown": dict(sorted(status_breakdown.items())),
        }

    def _previous_metrics_by_case(self) -> dict[str, dict[str, object]]:
        previous: dict[str, dict[str, object]] = {}
        for row in self.store.read_jsonl("case_metrics.jsonl"):
            case_id = row.get("case_id")
            if isinstance(case_id, str) and case_id:
                previous[case_id] = row
        return previous

    @staticmethod
    def _preserve_existing_artifact_projection(
        metric: CaseMetrics,
        previous: dict[str, object] | None,
    ) -> CaseMetrics:
        if previous is None or metric.artifact_count > 0 or metric.status not in _TERMINAL_CASE_STATUSES:
            return metric
        previous_artifact_count = previous.get("artifact_count")
        if not isinstance(previous_artifact_count, int) or previous_artifact_count <= 0:
            return metric
        updates = {
            field: previous[field]
            for field in _PRESERVED_ARTIFACT_METRIC_FIELDS
            if field in previous and previous[field] not in ("", None, {}, [])
        }
        if not updates:
            return metric
        return metric.model_copy(update=updates)
