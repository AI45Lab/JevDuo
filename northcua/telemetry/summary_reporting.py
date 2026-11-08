"""Experiment summary aggregation and audit markdown rendering."""

from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Callable

from northcua.telemetry.contracts import (
    CaseMetrics,
    CaseStatus,
    CaseTelemetrySnapshot,
    ExperimentTelemetrySummary,
    TelemetryGroupStats,
)
from northcua.telemetry.report_values import MetricValue, TimeValue


class ExperimentSummaryBuilder:
    """Aggregate case metrics into an experiment telemetry summary."""

    def build(
        self,
        *,
        metrics: list[CaseMetrics],
        snapshots: list[CaseTelemetrySnapshot],
    ) -> ExperimentTelemetrySummary:
        """Build an experiment-level summary from case rows.

        Args:
            metrics: Normalized case metric rows.
            snapshots: Latest case snapshots used to compute wall-clock elapsed time.

        Returns:
            Typed aggregate summary for JSON and markdown reports.
        """

        total_cases = len(metrics)
        terminal_cases = [
            metric
            for metric in metrics
            if metric.status in {CaseStatus.SUCCEEDED, CaseStatus.FAILED, CaseStatus.TIMED_OUT, CaseStatus.CANCELED}
        ]
        succeeded = sum(1 for metric in metrics if self._is_success(metric))
        failed = sum(1 for metric in metrics if metric.status == CaseStatus.FAILED and not self._is_success(metric))
        timed_out = sum(1 for metric in metrics if metric.status == CaseStatus.TIMED_OUT)
        canceled = sum(1 for metric in metrics if metric.status == CaseStatus.CANCELED)
        total_seconds = [metric.total_seconds for metric in metrics if metric.total_seconds > 0]
        elapsed_seconds = self._wall_elapsed_seconds(snapshots) or (max(total_seconds) if total_seconds else 0.0)
        completed = succeeded + failed + timed_out + canceled
        failure_breakdown = Counter(
            metric.failure_category.value
            for metric in metrics
            if metric.failure_category is not None and not self._is_success(metric)
        )
        attempt_failure_breakdown: Counter[str] = Counter()
        for metric in metrics:
            attempt_failure_breakdown.update(metric.attempt_failure_category_breakdown)
        safety_risk_breakdown = Counter(
            metric.evaluator_safety_risk_level for metric in metrics if metric.evaluator_safety_risk_level
        )
        diagnosis_category_breakdown: Counter[str] = Counter()
        for metric in metrics:
            diagnosis_category_breakdown.update(metric.evaluator_diagnosis_category_breakdown)
        efficiency_ratings = [
            metric.evaluator_efficiency_rating for metric in metrics if metric.evaluator_efficiency_rating > 0
        ]
        prepared_hits = sum(1 for metric in metrics if metric.prepared_sandbox_hit)
        prepared_misses = sum(1 for metric in metrics if metric.prepared_sandbox_miss)
        prepared_saved_seconds = [
            metric.prepared_sandbox_estimated_saved_seconds
            for metric in metrics
            if metric.prepared_sandbox_estimated_saved_seconds > 0
        ]
        prepared_claim_wait_seconds = [
            metric.prepared_sandbox_claim_wait_seconds
            for metric in metrics
            if metric.prepared_sandbox_claim_wait_seconds > 0
        ]
        benchmark_scores = [metric.benchmark_score for metric in metrics if metric.benchmark_hook_id]
        return ExperimentTelemetrySummary(
            experiment_id=metrics[0].experiment_id if metrics else "",
            batch_id=metrics[0].batch_id if metrics else "",
            total_cases=total_cases,
            succeeded=succeeded,
            failed=failed,
            timed_out=timed_out,
            canceled=canceled,
            running=total_cases - len(terminal_cases),
            success_rate=succeeded / total_cases if total_cases else 0.0,
            elapsed_seconds=elapsed_seconds,
            throughput_cases_per_hour=completed / elapsed_seconds * 3600.0 if elapsed_seconds else 0.0,
            p50_total_seconds=self._p50(total_seconds),
            p95_total_seconds=self._p95(total_seconds),
            failure_breakdown=dict(failure_breakdown),
            attempt_failure_breakdown=dict(attempt_failure_breakdown),
            artifact_count=sum(metric.artifact_count for metric in metrics),
            artifact_bytes=sum(metric.artifact_bytes for metric in metrics),
            retry_summary=dict(Counter(metric.recovery_action for metric in metrics if metric.recovery_action)),
            cleanup_failures=sum(1 for metric in metrics if metric.cleanup_status and metric.cleanup_status != "ok"),
            safety_risk_breakdown=dict(safety_risk_breakdown),
            diagnosis_category_breakdown=dict(diagnosis_category_breakdown),
            average_efficiency_rating=(
                round(sum(efficiency_ratings) / len(efficiency_ratings), 4) if efficiency_ratings else 0.0
            ),
            prepared_sandbox_hits=prepared_hits,
            prepared_sandbox_misses=prepared_misses,
            prepared_sandbox_hit_rate=(
                prepared_hits / (prepared_hits + prepared_misses) if prepared_hits + prepared_misses else 0.0
            ),
            prepared_sandbox_estimated_saved_seconds=sum(prepared_saved_seconds),
            prepared_sandbox_p50_estimated_saved_seconds=self._p50(prepared_saved_seconds),
            prepared_sandbox_p95_estimated_saved_seconds=self._p95(prepared_saved_seconds),
            prepared_sandbox_p50_claim_wait_seconds=self._p50(prepared_claim_wait_seconds),
            benchmark_passed=sum(1 for metric in metrics if metric.benchmark_passed is True),
            benchmark_failed=sum(1 for metric in metrics if metric.benchmark_passed is False),
            benchmark_errors=sum(
                1 for metric in metrics if metric.benchmark_hook_id and metric.benchmark_passed is None
            ),
            benchmark_avg_score=(round(sum(benchmark_scores) / len(benchmark_scores), 4) if benchmark_scores else 0.0),
            by_primary_app=self._group_by_primary_app(metrics),
            by_involved_app=self._group_by_involved_app(metrics),
            by_app_count=self._group_by_app_count(metrics),
            by_operation_family=self._group_by_operation(metrics),
            by_expected_feasibility=self._group_by_feasibility(metrics),
        )

    @staticmethod
    def _wall_elapsed_seconds(snapshots: list[CaseTelemetrySnapshot]) -> float:
        starts = [TimeValue.parse(snapshot.started_at) for snapshot in snapshots if snapshot.started_at]
        finishes = [
            TimeValue.parse(snapshot.finished_at or snapshot.updated_at)
            for snapshot in snapshots
            if snapshot.finished_at or snapshot.updated_at
        ]
        starts = [item for item in starts if item is not None]
        finishes = [item for item in finishes if item is not None]
        if not starts or not finishes:
            return 0.0
        return max((max(finishes) - min(starts)).total_seconds(), 0.0)

    @staticmethod
    def _p50(values: list[float]) -> float:
        return float(median(values)) if values else 0.0

    @staticmethod
    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        values = sorted(values)
        return float(values[min(len(values) - 1, int(round((len(values) - 1) * 0.95)))])

    @classmethod
    def _group_by_primary_app(cls, metrics: list[CaseMetrics]) -> dict[str, TelemetryGroupStats]:
        return cls._group_by(metrics, lambda metric: [metric.app_profile or "unknown"])

    @classmethod
    def _group_by_involved_app(cls, metrics: list[CaseMetrics]) -> dict[str, TelemetryGroupStats]:
        return cls._group_by(metrics, lambda metric: metric.involved_app_profiles or [metric.app_profile or "unknown"])

    @classmethod
    def _group_by_app_count(cls, metrics: list[CaseMetrics]) -> dict[str, TelemetryGroupStats]:
        return cls._group_by(metrics, lambda metric: [str(metric.involved_app_count or 1)])

    @classmethod
    def _group_by_operation(cls, metrics: list[CaseMetrics]) -> dict[str, TelemetryGroupStats]:
        return cls._group_by(metrics, lambda metric: [metric.operation_family or "unknown"])

    @classmethod
    def _group_by_feasibility(cls, metrics: list[CaseMetrics]) -> dict[str, TelemetryGroupStats]:
        return cls._group_by(metrics, lambda metric: [metric.expected_feasibility or "feasible"])

    @classmethod
    def _group_by(
        cls,
        metrics: list[CaseMetrics],
        key_factory: Callable[[CaseMetrics], list[str]],
    ) -> dict[str, TelemetryGroupStats]:
        groups: dict[str, list[CaseMetrics]] = {}
        for metric in metrics:
            for key in key_factory(metric):
                groups.setdefault(key, []).append(metric)
        return {key: cls._group_stats(rows) for key, rows in sorted(groups.items())}

    @classmethod
    def _group_stats(cls, metrics: list[CaseMetrics]) -> TelemetryGroupStats:
        total = len(metrics)
        succeeded = sum(1 for metric in metrics if cls._is_success(metric))
        failed = sum(1 for metric in metrics if metric.status == CaseStatus.FAILED and not cls._is_success(metric))
        timed_out = sum(1 for metric in metrics if metric.status == CaseStatus.TIMED_OUT)
        canceled = sum(1 for metric in metrics if metric.status == CaseStatus.CANCELED)
        total_seconds = [metric.total_seconds for metric in metrics if metric.total_seconds > 0]
        return TelemetryGroupStats(
            total=total,
            succeeded=succeeded,
            failed=failed,
            timed_out=timed_out,
            canceled=canceled,
            success_rate=succeeded / total if total else 0.0,
            p50_total_seconds=cls._p50(total_seconds),
            p95_total_seconds=cls._p95(total_seconds),
            avg_tool_calls=round(sum(metric.tool_call_count for metric in metrics) / total, 4) if total else 0.0,
            avg_commands=round(sum(metric.command_count for metric in metrics) / total, 4) if total else 0.0,
            avg_main_agent_seconds=(
                round(sum(metric.main_agent_seconds for metric in metrics) / total, 4) if total else 0.0
            ),
        )

    @staticmethod
    def _is_success(metric: CaseMetrics) -> bool:
        if metric.status == CaseStatus.SUCCEEDED:
            return True
        return metric.expected_feasibility == "infeasible" and metric.terminal_reason == "infeasible"


class AuditMarkdownRenderer:
    """Render human-readable audit reports from typed summary and metrics."""

    def render(self, *, summary: ExperimentTelemetrySummary, metrics: list[CaseMetrics]) -> str:
        """Render the markdown audit report body.

        Args:
            summary: Experiment-level aggregate metrics.
            metrics: Per-case metrics displayed in the case table.

        Returns:
            Markdown report content.
        """

        lines = [
            "# Telemetry Audit Report",
            "",
            f"- batch: `{summary.batch_id or '-'}`",
            f"- total cases: {summary.total_cases}",
            f"- succeeded: {summary.succeeded}",
            f"- failed: {summary.failed}",
            f"- timed out: {summary.timed_out}",
            f"- canceled: {summary.canceled}",
            f"- success rate: {summary.success_rate:.2%}",
            f"- p50 total seconds: {summary.p50_total_seconds:.3f}",
            f"- p95 total seconds: {summary.p95_total_seconds:.3f}",
            f"- attempt failure breakdown: {summary.attempt_failure_breakdown or {}}",
            f"- cleanup failures: {summary.cleanup_failures}",
            f"- safety risk breakdown: {summary.safety_risk_breakdown or {}}",
            f"- diagnosis category breakdown: {summary.diagnosis_category_breakdown or {}}",
            f"- average efficiency rating: {summary.average_efficiency_rating:.3f}",
            f"- prepared sandbox misses: {summary.prepared_sandbox_miss_breakdown or {}}",
            f"- prepared sandbox failures: {summary.prepared_sandbox_failures}",
            f"- prepared sandbox cleanup failures: {summary.prepared_sandbox_cleanup_failures}",
            f"- prepared sandbox statuses: {summary.prepared_sandbox_status_breakdown or {}}",
            f"- benchmark passed: {summary.benchmark_passed}",
            f"- benchmark failed: {summary.benchmark_failed}",
            f"- benchmark errors: {summary.benchmark_errors}",
            f"- benchmark average score: {summary.benchmark_avg_score:.4f}",
            f"- primary app success: {_compact_group_success(summary.by_primary_app)}",
            f"- operation success: {_compact_group_success(summary.by_operation_family)}",
            f"- feasibility success: {_compact_group_success(summary.by_expected_feasibility)}",
            "",
            "## Recovery",
            "",
            *[f"- {action}: {count}" for action, count in sorted(summary.retry_summary.items())],
            "" if summary.retry_summary else "- no recovery actions recorded",
            "",
            "## Cases",
            "",
            (
                "| case | status | attempt | total(s) | sandbox(s) | prepared(s) | main agent(s) | evaluator(s) | "
                "benchmark | reward | tokens(in/out) | tools | commands | safety | efficiency | diagnoses | terminal | recovery | "
                "failure | attempt failures | trace dir | log dir |"
            ),
            (
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | --- | ---: | "
                "--- | --- | --- | --- | --- | --- | --- |"
            ),
        ]
        for metric in metrics:
            lines.append(self._case_row(metric))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _case_row(metric: CaseMetrics) -> str:
        failure = metric.failure_category.value if metric.failure_category else ""
        attempt_failures = MetricValue.diagnosis_summary(metric.attempt_failure_category_breakdown)
        terminal = metric.terminal_reason
        if metric.terminal_scope:
            terminal = f"{terminal}:{metric.terminal_scope} {metric.terminal_current}/{metric.terminal_limit}"
        return (
            f"| `{metric.case_id}` | {metric.status.value} | {metric.retry_count + 1} | "
            f"{metric.total_seconds:.3f} | {metric.sandbox_seconds:.3f} | "
            f"{metric.prepared_sandbox_seconds:.3f} | "
            f"{metric.main_agent_seconds:.3f} | {metric.evaluator_seconds:.3f} | "
            f"{_benchmark_cell(metric)} | {metric.reward_artifact_path} | "
            f"{metric.prompt_tokens}/{metric.completion_tokens} | {metric.tool_call_count} | "
            f"{metric.command_count} | {metric.evaluator_safety_risk_level} | "
            f"{metric.evaluator_efficiency_rating or ''} | "
            f"{MetricValue.diagnosis_summary(metric.evaluator_diagnosis_category_breakdown)} | "
            f"{terminal} | {metric.recovery_action} | {failure} | {attempt_failures} | "
            f"{metric.main_trace_dir or metric.evaluator_trace_dir} | {metric.sandbox_log_dir} |"
        )


def _benchmark_cell(metric: CaseMetrics) -> str:
    if not metric.benchmark_hook_id:
        return ""
    passed = "" if metric.benchmark_passed is None else str(metric.benchmark_passed).lower()
    return f"{metric.benchmark_status}:{metric.benchmark_score:.3f}:{passed}"


def _compact_group_success(groups: dict[str, TelemetryGroupStats]) -> dict[str, str]:
    return {
        key: f"{value.succeeded}/{value.total} ({value.success_rate:.1%})"
        for key, value in groups.items()
        if value.total
    }
