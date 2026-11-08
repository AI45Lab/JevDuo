"""Report writer for reusable case-suite runs."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from northcua.batch.contracts import BatchRunResult, BatchTaskState, BatchTaskStatus
from northcua.experiments.cases.contracts import CaseRunMode
from northcua.experiments.cases.materialization import MaterializedCaseSuite
from northcua.experiments.cases.reporting.models import (
    CaseReportRow,
    CaseSetupPhaseReport,
    CaseSuiteReport,
    CaseTelemetryPathSet,
)
from northcua.experiments.cases.verification import CaseArtifactResult
from northcua.sandboxes.guest_capability_evidence import guest_capability_evidence_payloads
from northcua.telemetry.contracts import FailureCategory


class CaseReportWriter:
    """Write a stable report derived from batch SQLite state and case IR."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def write(
        self,
        *,
        materialized: MaterializedCaseSuite,
        batch_result: BatchRunResult,
        artifact_results: list[CaseArtifactResult] | None = None,
        dry_run: bool = False,
    ) -> Path:
        """Persist a typed case report and return its path.

        Args:
            materialized: Materialized suite containing case and fixture IR.
            batch_result: Authoritative batch result and task states.
            artifact_results: Optional verifier rows for trace/report paths.
            dry_run: Whether the run avoided live provider side effects.

        Returns:
            Path to the persisted case_report.json file.
        """

        batch_dir = Path(batch_result.output_dir)
        state_ids = [state.task_id for state in batch_result.task_states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("batch result task states must have unique task ids")
        states = {state.task_id: state for state in batch_result.task_states}
        artifacts = {result.task_id: result for result in artifact_results or []}
        metrics = self._case_metrics(batch_dir)
        run_mode = CaseRunMode.DRY_RUN if dry_run else CaseRunMode.LIVE
        rows = [
            self._row(
                case_suite=materialized,
                task_id=case.case.task_id,
                state=states.get(case.case.task_id),
                artifact_result=artifacts.get(case.case.task_id),
                metric=metrics.get(case.case.task_id, {}),
                batch_dir=batch_dir,
                run_mode=run_mode,
                dry_run=dry_run,
            )
            for case in materialized.cases
        ]
        runnable_rows = [row for row in rows if row.disposition.is_runnable]
        succeeded, failed, canceled, incomplete = _terminal_counts(runnable_rows)
        trace_checks_passed = sum(1 for row in runnable_rows if row.passed_trace_checks)
        trace_checks_failed = len(runnable_rows) - trace_checks_passed
        evaluator_verdict_breakdown = self._evaluator_verdict_breakdown(runnable_rows)
        benchmark_summary = self._benchmark_summary(rows)
        artifact_totals = self._artifact_totals(rows)
        app_group_summary = self._app_group_summary(rows)
        operation_group_summary = self._operation_group_summary(rows)
        feasibility_group_summary = self._feasibility_group_summary(rows)
        batch_metrics = self._batch_metrics(batch_dir)
        report = CaseSuiteReport(
            suite_id=materialized.spec.suite_id,
            run_id=batch_result.batch_id,
            run_mode=run_mode,
            dry_run=dry_run,
            batch_id=batch_result.batch_id,
            total=len(rows),
            runnable=len(runnable_rows),
            blocked=len(rows) - len(runnable_rows),
            succeeded=succeeded,
            failed=failed,
            canceled=canceled,
            incomplete=incomplete,
            trace_checks_passed=trace_checks_passed,
            trace_checks_failed=trace_checks_failed,
            evaluator_verdict_breakdown=evaluator_verdict_breakdown,
            benchmark_passed=benchmark_summary["passed"],
            benchmark_failed=benchmark_summary["failed"],
            benchmark_error_count=benchmark_summary["error"],
            app_group_summary=app_group_summary,
            operation_group_summary=operation_group_summary,
            feasibility_group_summary=feasibility_group_summary,
            batch_metrics=batch_metrics,
            output_dir=str(self._output_dir),
            batch_output_dir=str(batch_dir),
            sqlite_state_path=str(batch_dir / "batch_state.sqlite"),
            audit_jsonl_path=str(batch_dir / "batch_state.jsonl"),
            tasks_jsonl_path=str(batch_dir / "tasks.jsonl"),
            telemetry_paths=self._telemetry_paths(batch_dir),
            cases=rows,
        )
        path = self._output_dir / "case_report.json"
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        summary_path = self._output_dir / "case_report.summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "suite_id": report.suite_id,
                    "run_id": report.run_id,
                    "run_mode": report.run_mode.value,
                    "dry_run": report.dry_run,
                    "total": report.total,
                    "runnable": report.runnable,
                    "blocked": report.blocked,
                    "succeeded": report.succeeded,
                    "failed": report.failed,
                    "canceled": report.canceled,
                    "incomplete": report.incomplete,
                    "trace_checks_passed": report.trace_checks_passed,
                    "trace_checks_failed": report.trace_checks_failed,
                    "evaluator_verdict_breakdown": report.evaluator_verdict_breakdown,
                    "benchmark_passed": report.benchmark_passed,
                    "benchmark_failed": report.benchmark_failed,
                    "benchmark_error_count": report.benchmark_error_count,
                    "app_group_summary": report.app_group_summary,
                    "operation_group_summary": report.operation_group_summary,
                    "feasibility_group_summary": report.feasibility_group_summary,
                    "batch_metrics": report.batch_metrics,
                    "artifact_count": artifact_totals["artifact_count"],
                    "artifact_bytes": artifact_totals["artifact_bytes"],
                    "screenshot_count": artifact_totals["screenshot_count"],
                    "case_report_path": str(path),
                    "sqlite_state_path": report.sqlite_state_path,
                    "tasks_jsonl_path": report.tasks_jsonl_path,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def _row(
        self,
        *,
        case_suite: MaterializedCaseSuite,
        task_id: str,
        state: BatchTaskState | None,
        artifact_result: CaseArtifactResult | None,
        metric: dict[str, object],
        batch_dir: Path,
        run_mode: CaseRunMode,
        dry_run: bool,
    ) -> CaseReportRow:
        materialized = next(item for item in case_suite.cases if item.case.task_id == task_id)
        is_runnable = materialized.case.disposition.is_runnable
        if not is_runnable:
            state = None
            artifact_result = None
            metric = {}
        attempts = state.attempts if state is not None else 0
        attempt_dir = batch_dir / "sandbox-logs" / task_id / f"attempt-{max(1, attempts)}"
        return CaseReportRow(
            task_id=task_id,
            query=materialized.case.query,
            run_mode=run_mode,
            dry_run=dry_run,
            app_profile=materialized.case.app_profile,
            template_profile=materialized.case.template_profile,
            template_alias=materialized.sandbox.template,
            involved_app_profiles=materialized.case.involved_app_profiles,
            involved_app_count=materialized.case.involved_app_count,
            expected_feasibility=materialized.case.expected_feasibility,
            disposition=materialized.case.disposition,
            setup_contract=materialized.case.setup_contract_source,
            setup_phases=[
                CaseSetupPhaseReport(
                    phase=phase.phase,
                    recipe_kinds=[recipe.recipe for recipe in phase.recipes],
                    command_labels=[command.label for command in phase.commands],
                )
                for phase in materialized.setup_phases
            ],
            operation_family=str(materialized.case.metadata.get("operation_family") or ""),
            status=state.status if state is not None else ("unknown" if is_runnable else "blocked"),
            terminal_state=state.terminal_state.value if state is not None and state.terminal_state is not None else "",
            terminal_decision=state.terminal_decision if state is not None else {},
            cleanup_status=state.cleanup_status if state is not None else "",
            last_failure_category=state.last_failure_category if state is not None else None,
            quality_gate_failure_category=self._quality_gate_failure_category(
                artifact_result,
                is_runnable=is_runnable,
            ),
            attempt_failure_category_breakdown=self._int_map(metric.get("attempt_failure_category_breakdown")),
            last_reconciled_status=state.last_reconciled_status if state is not None else "",
            attempts=attempts,
            controller_task_id=state.controller_task_id if state is not None else "",
            trace_id=state.trace_id if state is not None else "",
            session_id=state.session_id if state is not None else "",
            sandbox_id=state.sandbox_id if state is not None else "",
            device_id=state.device_id if state is not None else "",
            error=state.error if state is not None else "",
            guest_capability_evidence=(guest_capability_evidence_payloads(state.error) if state is not None else []),
            fixture_remote_paths=[fixture.spec.remote_path for fixture in materialized.fixtures],
            fixture_sha256={fixture.spec.remote_path: fixture.spec.sha256 for fixture in materialized.fixtures},
            large_artifact_delivery_evidence=[
                fixture.large_artifact_evidence
                for fixture in materialized.fixtures
                if fixture.large_artifact_evidence is not None
            ],
            artifact_count=self._int_metric(metric.get("artifact_count")),
            screenshot_count=self._int_metric(metric.get("screenshot_count")),
            artifact_bytes=self._int_metric(metric.get("artifact_bytes")),
            command_count=self._int_metric(metric.get("command_count")),
            tool_call_count=self._int_metric(metric.get("tool_call_count")),
            sandbox_log_dir=str(attempt_dir) if is_runnable else "",
            main_trace_dir=artifact_result.main_trace_dir if artifact_result is not None else "",
            main_steps_path=artifact_result.main_steps_path if artifact_result is not None else "",
            main_llm_trace_path=artifact_result.main_llm_trace_path if artifact_result is not None else "",
            screenshot_paths=artifact_result.screenshot_paths if artifact_result is not None else [],
            final_verify_screenshot_path=(
                artifact_result.final_verify_screenshot_path if artifact_result is not None else ""
            ),
            evaluator_trace_dir=artifact_result.evaluator_trace_dir if artifact_result is not None else "",
            evaluator_report_path=artifact_result.evaluator_report_path if artifact_result is not None else "",
            evaluator_evidence_manifest_path=(
                artifact_result.evaluator_evidence_manifest_path if artifact_result is not None else ""
            ),
            evaluator_llm_trace_path=artifact_result.evaluator_llm_trace_path if artifact_result is not None else "",
            evaluator_verdict=artifact_result.evaluator_verdict if artifact_result is not None else "",
            evaluator_safety_risk_level=(
                artifact_result.evaluator_safety_risk_level if artifact_result is not None else ""
            ),
            evaluator_efficiency_rating=(
                artifact_result.evaluator_efficiency_rating if artifact_result is not None else 0
            ),
            evaluator_diagnosis_category_breakdown=(
                artifact_result.evaluator_diagnosis_category_breakdown if artifact_result is not None else {}
            ),
            benchmark_hook_id=self._benchmark_hook_id(state),
            benchmark_status=self._benchmark_status(state),
            benchmark_score=self._benchmark_score(state),
            benchmark_passed=self._benchmark_passed(state),
            benchmark_error=self._benchmark_error(state),
            reward_artifact_path=self._reward_artifact_path(state),
            post_run_results=(
                [result.model_dump(mode="json") for result in state.post_run_results] if state is not None else []
            ),
            passed_trace_checks=artifact_result.passed if artifact_result is not None else False,
            trace_errors=artifact_result.errors if artifact_result is not None else [],
            telemetry_paths=self._telemetry_paths(batch_dir),
            metadata=materialized.case.metadata,
        )

    @staticmethod
    def _telemetry_paths(batch_dir: Path) -> CaseTelemetryPathSet:
        return CaseTelemetryPathSet(
            events=str(batch_dir / "telemetry_events.jsonl"),
            snapshots=str(batch_dir / "case_snapshots.jsonl"),
            timelines=str(batch_dir / "case_timelines.jsonl"),
            metrics=str(batch_dir / "case_metrics.jsonl"),
            summary=str(batch_dir / "telemetry_summary.json"),
            audit_report=str(batch_dir / "audit_report.md"),
        )

    @staticmethod
    def _benchmark_summary(rows: list[CaseReportRow]) -> dict[str, int]:
        passed = 0
        failed = 0
        error = 0
        for row in rows:
            if row.benchmark_passed is True:
                passed += 1
            elif row.benchmark_passed is False:
                failed += 1
            elif row.benchmark_error:
                error += 1
        return {"passed": passed, "failed": failed, "error": error}

    @staticmethod
    def _primary_post_run(state: BatchTaskState | None):
        if state is None or not state.post_run_results:
            return None
        return state.post_run_results[0]

    @classmethod
    def _benchmark_score(cls, state: BatchTaskState | None) -> float:
        result = cls._primary_post_run(state)
        return float(result.score) if result is not None and result.score is not None else 0.0

    @classmethod
    def _benchmark_hook_id(cls, state: BatchTaskState | None) -> str:
        result = cls._primary_post_run(state)
        return result.hook_id if result is not None else ""

    @classmethod
    def _benchmark_status(cls, state: BatchTaskState | None) -> str:
        result = cls._primary_post_run(state)
        return result.status.value if result is not None else ""

    @classmethod
    def _benchmark_passed(cls, state: BatchTaskState | None) -> bool | None:
        result = cls._primary_post_run(state)
        return result.passed if result is not None else None

    @classmethod
    def _benchmark_error(cls, state: BatchTaskState | None) -> str:
        result = cls._primary_post_run(state)
        return result.error if result is not None else ""

    @classmethod
    def _reward_artifact_path(cls, state: BatchTaskState | None) -> str:
        result = cls._primary_post_run(state)
        if result is None or not result.artifacts:
            return ""
        return next(iter(result.artifacts.values()))

    @staticmethod
    def _case_metrics(batch_dir: Path) -> dict[str, dict[str, object]]:
        metrics_path = batch_dir / "case_metrics.jsonl"
        if not metrics_path.exists():
            return {}
        rows: dict[str, dict[str, object]] = {}
        for line in metrics_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            case_id = payload.get("case_id")
            if isinstance(case_id, str) and case_id:
                rows[case_id] = payload
        return rows

    @staticmethod
    def _batch_metrics(batch_dir: Path) -> dict[str, object]:
        summary_path = batch_dir / "batch_summary.json"
        if not summary_path.exists():
            return {}
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        metrics = payload.get("metrics")
        return metrics if isinstance(metrics, dict) else {}

    @staticmethod
    def _int_map(value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): int(item) for key, item in value.items() if isinstance(item, int) and not isinstance(item, bool)
        }

    @staticmethod
    def _quality_gate_failure_category(
        artifact_result: CaseArtifactResult | None,
        *,
        is_runnable: bool,
    ) -> FailureCategory | None:
        if not is_runnable:
            return None
        if artifact_result is not None and artifact_result.passed:
            return None
        if artifact_result is None:
            return FailureCategory.ARTIFACT_MISSING
        if any(error.startswith("evaluator overall_verdict is not success") for error in artifact_result.errors):
            return FailureCategory.EVALUATOR
        if any(error.startswith("batch task did not succeed") for error in artifact_result.errors):
            return FailureCategory.MAIN_AGENT
        return FailureCategory.ARTIFACT_MISSING

    @staticmethod
    def _int_metric(value: object) -> int:
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _artifact_totals(rows: list[CaseReportRow]) -> dict[str, int]:
        return {
            "artifact_count": sum(row.artifact_count for row in rows),
            "artifact_bytes": sum(row.artifact_bytes for row in rows),
            "screenshot_count": sum(row.screenshot_count for row in rows),
        }

    @staticmethod
    def _evaluator_verdict_breakdown(rows: list[CaseReportRow]) -> dict[str, int]:
        breakdown: dict[str, int] = {}
        for row in rows:
            verdict = row.evaluator_verdict or "missing"
            breakdown[verdict] = breakdown.get(verdict, 0) + 1
        return dict(sorted(breakdown.items()))

    @staticmethod
    def _app_group_summary(rows: list[CaseReportRow]) -> dict[str, object]:
        summary: dict[str, object] = {}
        for app in sorted({app.value for row in rows for app in row.involved_app_profiles}):
            group = [row for row in rows if app in {item.value for item in row.involved_app_profiles}]
            summary[app] = _row_group_stats(group)
        return summary

    @staticmethod
    def _operation_group_summary(rows: list[CaseReportRow]) -> dict[str, object]:
        return {
            key: _row_group_stats([row for row in rows if (row.operation_family or "unknown") == key])
            for key in sorted({row.operation_family or "unknown" for row in rows})
        }

    @staticmethod
    def _feasibility_group_summary(rows: list[CaseReportRow]) -> dict[str, object]:
        return {
            key: _row_group_stats([row for row in rows if row.expected_feasibility.value == key])
            for key in sorted({row.expected_feasibility.value for row in rows})
        }


def _row_group_stats(rows: list[CaseReportRow]) -> dict[str, object]:
    total = len(rows)
    runnable_rows = [row for row in rows if row.disposition.is_runnable]
    runnable = len(runnable_rows)
    status_counts = Counter(_status_value(row.status) for row in rows)
    succeeded = status_counts.get("succeeded", 0)
    return {
        "total": total,
        "runnable": runnable,
        "blocked": total - runnable,
        "succeeded": succeeded,
        "success_rate": round(succeeded / runnable, 4) if runnable else 0.0,
        "avg_tool_calls": (round(sum(row.tool_call_count for row in runnable_rows) / runnable, 4) if runnable else 0.0),
        "avg_commands": round(sum(row.command_count for row in runnable_rows) / runnable, 4) if runnable else 0.0,
        "status_counts": dict(sorted(status_counts.items())),
    }


def _terminal_counts(rows: list[CaseReportRow]) -> tuple[int, int, int, int]:
    statuses = [_status_value(row.status) for row in rows]
    succeeded = statuses.count(BatchTaskStatus.SUCCEEDED.value)
    failed = sum(status in {BatchTaskStatus.FAILED.value, BatchTaskStatus.TIMED_OUT.value} for status in statuses)
    canceled = statuses.count(BatchTaskStatus.CANCELED.value)
    return succeeded, failed, canceled, len(rows) - succeeded - failed - canceled


def _status_value(status: BatchTaskStatus | str) -> str:
    if isinstance(status, BatchTaskStatus):
        return status.value
    return status
