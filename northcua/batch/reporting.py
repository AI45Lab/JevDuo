"""Batch report writing helpers."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

from northcua.batch.contracts import BatchRunResult
from northcua.telemetry.reporting import TelemetryReportWriter

if TYPE_CHECKING:
    from northcua.batch.state import BatchStateRepository


class BatchReportWriter:
    """Persist machine-readable batch reports."""

    def __init__(self, output_dir: Path, *, state_repository: "BatchStateRepository | None" = None) -> None:
        self.output_dir = output_dir
        self.state_repository = state_repository
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_summary(self, result: BatchRunResult, metrics: dict[str, float | int | dict[str, int]]) -> None:
        """Write final summary JSON files.

        Args:
            result: Batch result summary.
            metrics: Aggregate metrics summary.
        """

        telemetry_summary = TelemetryReportWriter(self.output_dir, state_repository=self.state_repository).write()
        merged_metrics = _merge_persisted_prepared_metrics(self.output_dir, metrics)
        # Keep the desktop/E2B monitoring artifact contract stable even when a
        # run does not enable the prepared sandbox queue. This must happen
        # after prepared metric reconstruction so legacy telemetry-only runs can
        # still fall back to telemetry_events.jsonl.
        (self.output_dir / "prepared_sandbox_events.jsonl").touch(exist_ok=True)
        payload = result.model_dump(mode="json")
        payload["metrics"] = merged_metrics
        payload["post_run"] = _post_run_summary(result)
        payload["telemetry"] = {
            "summary_path": str(self.output_dir / "telemetry_summary.json"),
            "case_metrics_path": str(self.output_dir / "case_metrics.jsonl"),
            "case_timelines_path": str(self.output_dir / "case_timelines.jsonl"),
            "failure_breakdown_path": str(self.output_dir / "failure_breakdown.json"),
            "audit_report_path": str(self.output_dir / "audit_report.md"),
            "summary": telemetry_summary.model_dump(mode="json"),
        }
        (self.output_dir / "batch_summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        task_lines = [json.dumps(state.model_dump(mode="json"), ensure_ascii=False) for state in result.task_states]
        (self.output_dir / "tasks.jsonl").write_text(
            "\n".join(task_lines) + ("\n" if task_lines else ""), encoding="utf-8"
        )


def _merge_persisted_prepared_metrics(
    output_dir: Path,
    metrics: dict[str, float | int | dict[str, int]],
) -> dict[str, float | int | dict[str, int]]:
    """Merge resume-safe prepared metrics from append-only telemetry files.

    Args:
        output_dir: Batch output directory containing telemetry JSONL files.
        metrics: In-memory metrics from the current process.

    Returns:
        Metrics with prepared sandbox counters reconstructed from persisted
        telemetry when those events exist.
    """

    events_path = output_dir / "prepared_sandbox_events.jsonl"
    if not events_path.exists():
        events_path = output_dir / "telemetry_events.jsonl"
    if not events_path.exists():
        return metrics
    hits = 0
    misses = 0
    failures = 0
    cleanup_failures = 0
    miss_breakdown: dict[str, int] = {}
    status_breakdown: dict[str, int] = {}
    durations: list[float] = []
    claim_waits: list[float] = []
    estimated_saved: list[float] = []
    for row in _read_jsonl(events_path):
        message = str(row.get("message") or "")
        if message == "prepared_sandbox_assigned":
            hits += 1
            details = row.get("details")
            prepare_seconds = _float_value(details.get("prepare_seconds") if isinstance(details, dict) else None)
            claim_wait_seconds = _float_value(details.get("claim_wait_seconds") if isinstance(details, dict) else None)
            if claim_wait_seconds > 0:
                claim_waits.append(claim_wait_seconds)
            if prepare_seconds > 0:
                estimated_saved.append(max(prepare_seconds - claim_wait_seconds, 0.0))
        elif message == "prepared_sandbox_fallback":
            details = row.get("details")
            reason = str(details.get("reason") if isinstance(details, dict) else "")
            if reason != "disabled":
                misses += 1
                if reason:
                    miss_breakdown[reason] = miss_breakdown.get(reason, 0) + 1
        elif message == "prepared_sandbox_failed":
            failures += 1
        elif message == "prepared_sandbox_cleanup_failed":
            cleanup_failures += 1
            details = row.get("details")
            if isinstance(details, dict) and details.get("prepare_failed") is True:
                failures += 1
        if str(row.get("message") or "") != "prepared_sandbox_ready":
            details = row.get("details")
            if isinstance(details, dict):
                raw_counts = details.get("prepared_status_counts")
                if isinstance(raw_counts, dict):
                    status_breakdown = {
                        str(key): int(value) for key, value in raw_counts.items() if isinstance(value, int)
                    }
            continue
        details = row.get("details")
        if isinstance(details, dict):
            raw_counts = details.get("prepared_status_counts")
            if isinstance(raw_counts, dict):
                status_breakdown = {str(key): int(value) for key, value in raw_counts.items() if isinstance(value, int)}
        duration = _float_value(details.get("prepare_seconds") if isinstance(details, dict) else None)
        if duration > 0:
            durations.append(duration)
    if hits + misses + failures + cleanup_failures == 0 and not durations:
        return metrics
    merged = dict(metrics)
    merged["prepared_sandbox_hits"] = hits
    merged["prepared_sandbox_misses"] = misses
    merged["prepared_sandbox_failures"] = failures
    merged["prepared_sandbox_cleanup_failures"] = cleanup_failures
    merged["prepared_sandbox_miss_breakdown"] = dict(sorted(miss_breakdown.items()))
    merged["prepared_sandbox_status_breakdown"] = dict(sorted(status_breakdown.items()))
    merged["prepared_sandbox_hit_rate"] = float(hits / (hits + misses)) if hits + misses else 0.0
    merged["prepared_sandbox_estimated_saved_seconds"] = sum(estimated_saved)
    merged["prepared_sandbox_avg_estimated_saved_seconds"] = (
        sum(estimated_saved) / len(estimated_saved) if estimated_saved else 0.0
    )
    if durations:
        merged["prepared_sandbox_p50_seconds"] = _p50(durations)
    if estimated_saved:
        merged["prepared_sandbox_p50_estimated_saved_seconds"] = _p50(estimated_saved)
    if claim_waits:
        merged["prepared_sandbox_p50_claim_wait_seconds"] = _p50(claim_waits)
    return merged


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _p50(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _float_value(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _post_run_summary(result: BatchRunResult) -> dict[str, object]:
    total = 0
    passed = 0
    failed = 0
    errors = 0
    scores: list[float] = []
    for state in result.task_states:
        for hook_result in state.post_run_results:
            total += 1
            if hook_result.passed is True:
                passed += 1
            elif hook_result.passed is False:
                failed += 1
            else:
                errors += 1
            if hook_result.score is not None:
                scores.append(hook_result.score)
    return {
        "hook_results": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": float(passed / total) if total else 0.0,
        "avg_score": float(sum(scores) / len(scores)) if scores else 0.0,
    }
