"""Artifact, trace usage, and evaluator report metrics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from northcua.telemetry.contracts import CaseArtifactKind, CaseArtifactTelemetry
from northcua.telemetry.report_store import TelemetryJsonlReader
from northcua.telemetry.report_values import MetricValue


@dataclass(frozen=True)
class ArtifactMetricValues:
    """Computed artifact and trace metrics for one case.

    Attributes:
        artifact_count: Number of known artifact files that exist on disk.
        screenshot_count: Number of screenshot files among the known artifacts.
        artifact_bytes: Total byte size of known artifact files.
        command_count: Main-agent device/tool execution count.
        tool_call_count: Main-agent plus evaluator model tool-call count.
        prompt_tokens: Total prompt/input tokens across main and evaluator traces.
        completion_tokens: Total completion/output tokens across traces.
        evaluator_seconds: Evaluator wall-clock runtime when recorded.
    """

    artifact_count: int = 0
    screenshot_count: int = 0
    artifact_bytes: int = 0
    command_count: int = 0
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    evaluator_seconds: float = 0.0


class CaseArtifactMetricBuilder:
    """Compute stable artifact, usage, and evaluator metrics from known paths."""

    def compute(self, artifact: CaseArtifactTelemetry | None) -> ArtifactMetricValues:
        """Compute all artifact-derived metrics for one case.

        Args:
            artifact: Optional artifact path contract for a case.

        Returns:
            Numeric artifact, token, tool, and evaluator timing metrics.
        """

        if artifact is None:
            return ArtifactMetricValues()
        paths_by_kind = self._paths_by_kind(artifact)
        existing_paths = self._dedupe_paths(
            [path for paths in paths_by_kind.values() for path in paths if path.exists()]
        )
        main_usage = self._main_llm_trace_metrics(
            Path(artifact.main_llm_trace_path) if artifact.main_llm_trace_path else None
        )
        evaluator_usage = self._evaluator_llm_trace_metrics(
            Path(artifact.evaluator_llm_trace_path) if artifact.evaluator_llm_trace_path else None
        )
        evaluator_runtime = self._evaluator_runtime_seconds(
            Path(artifact.evaluator_llm_trace_path).with_name("runtime.jsonl")
            if artifact.evaluator_llm_trace_path
            else None
        )
        screenshot_paths = self._dedupe_paths(
            [path for path in paths_by_kind[CaseArtifactKind.SCREENSHOT] if path.exists()]
        )
        return ArtifactMetricValues(
            artifact_count=len(existing_paths),
            screenshot_count=len(screenshot_paths),
            artifact_bytes=sum(path.stat().st_size for path in existing_paths),
            command_count=main_usage["command_count"],
            tool_call_count=main_usage["tool_call_count"] + evaluator_usage["tool_call_count"],
            prompt_tokens=main_usage["prompt_tokens"] + evaluator_usage["prompt_tokens"],
            completion_tokens=main_usage["completion_tokens"] + evaluator_usage["completion_tokens"],
            evaluator_seconds=evaluator_runtime,
        )

    @staticmethod
    def _paths_by_kind(artifact: CaseArtifactTelemetry) -> dict[CaseArtifactKind, list[Path]]:
        return {
            CaseArtifactKind.MAIN_STEPS: CaseArtifactMetricBuilder._path_candidates([artifact.main_steps_path]),
            CaseArtifactKind.MAIN_LLM_TRACE: CaseArtifactMetricBuilder._path_candidates([artifact.main_llm_trace_path]),
            CaseArtifactKind.EVALUATOR_REPORT: CaseArtifactMetricBuilder._path_candidates(
                [artifact.evaluator_report_path]
            ),
            CaseArtifactKind.EVALUATOR_EVIDENCE: CaseArtifactMetricBuilder._path_candidates(
                [artifact.evaluator_evidence_manifest_path]
            ),
            CaseArtifactKind.EVALUATOR_LLM_TRACE: CaseArtifactMetricBuilder._path_candidates(
                [artifact.evaluator_llm_trace_path]
            ),
            CaseArtifactKind.SCREENSHOT: CaseArtifactMetricBuilder._path_candidates(artifact.screenshot_paths),
        }

    @staticmethod
    def _path_candidates(paths: list[str]) -> list[Path]:
        return [Path(path) for path in paths if path]

    @staticmethod
    def _dedupe_paths(paths: list[Path]) -> list[Path]:
        return list({path.resolve(): path for path in paths}.values())

    @staticmethod
    def _main_llm_trace_metrics(path: Path | None) -> dict[str, int]:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "tool_call_count": 0, "command_count": 0}
        for row in TelemetryJsonlReader.read_path(path):
            trace = row.get("llm_trace") if isinstance(row.get("llm_trace"), dict) else row
            if not isinstance(trace, dict):
                continue
            trace_usage = trace.get("usage")
            if isinstance(trace_usage, dict):
                usage["prompt_tokens"] += _prompt_token_count(trace_usage)
                usage["completion_tokens"] += _completion_token_count(trace_usage)
            tool_calls = trace.get("assistant_tool_calls")
            if isinstance(tool_calls, list):
                usage["tool_call_count"] += len(tool_calls)
            tool_executions = trace.get("tool_executions")
            if isinstance(tool_executions, list):
                usage["command_count"] += len(tool_executions)
        return usage

    @staticmethod
    def _evaluator_llm_trace_metrics(path: Path | None) -> dict[str, int]:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "tool_call_count": 0}
        for row in TelemetryJsonlReader.read_path(path):
            trace_usage = row.get("usage")
            if isinstance(trace_usage, dict):
                usage["prompt_tokens"] += _prompt_token_count(trace_usage)
                usage["completion_tokens"] += _completion_token_count(trace_usage)
            tool_checks = row.get("tool_checks")
            if isinstance(tool_checks, list):
                usage["tool_call_count"] += len(tool_checks)
        return usage

    @staticmethod
    def _evaluator_runtime_seconds(path: Path | None) -> float:
        for row in reversed(TelemetryJsonlReader.read_path(path)):
            if row.get("event") != "agentkit_final_report_completed":
                continue
            duration_ms = row.get("duration_ms")
            if isinstance(duration_ms, int | float) and duration_ms >= 0:
                return round(float(duration_ms) / 1000.0, 6)
        return 0.0


def _prompt_token_count(usage: dict[object, object]) -> int:
    return _usage_alias_count(usage, "input_tokens", "prompt_tokens")


def _completion_token_count(usage: dict[object, object]) -> int:
    return _usage_alias_count(usage, "completion_tokens", "output_tokens")


def _usage_alias_count(usage: dict[object, object], primary: str, fallback: str) -> int:
    if primary in usage:
        return MetricValue.as_int(usage.get(primary))
    return MetricValue.as_int(usage.get(fallback))


class EvaluatorAssessmentMetricBuilder:
    """Extract safety, efficiency, and diagnosis fields from final reports."""

    def compute(self, report_path: str) -> dict[str, object]:
        """Extract evaluator assessment metrics from a final report.

        Args:
            report_path: Path to evaluator ``final_report.json``.

        Returns:
            Flat metric payload used by ``CaseMetricBuilder``.
        """

        if not report_path:
            return {}
        path = Path(report_path)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        safety = payload.get("safety")
        efficiency = payload.get("efficiency")
        diagnoses = payload.get("diagnoses")
        safety = safety if isinstance(safety, dict) else {}
        efficiency = efficiency if isinstance(efficiency, dict) else {}
        diagnoses = diagnoses if isinstance(diagnoses, list) else []
        findings = safety.get("findings")
        findings = findings if isinstance(findings, list) else []
        return {
            "evaluator_safety_risk_level": str(safety.get("risk_level") or ""),
            "evaluator_safety_finding_count": len(findings),
            "evaluator_efficiency_rating": MetricValue.as_int(efficiency.get("rating")),
            "evaluator_diagnosis_category_breakdown": self._diagnosis_category_breakdown(diagnoses),
            "evaluator_unnecessary_step_count": MetricValue.as_int(efficiency.get("unnecessary_step_count")),
            "evaluator_repeated_action_count": MetricValue.as_int(efficiency.get("repeated_action_count")),
            "evaluator_detour_count": MetricValue.as_int(efficiency.get("detour_count")),
        }

    @staticmethod
    def _diagnosis_category_breakdown(diagnoses: list[object]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for diagnosis in diagnoses:
            if not isinstance(diagnosis, dict):
                continue
            category = str(diagnosis.get("category") or "")
            if category:
                counts[category] = counts.get(category, 0) + 1
        return counts
