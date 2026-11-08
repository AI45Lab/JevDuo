"""Terminal renderer for desktop evaluator replay and progress."""

from __future__ import annotations

from pathlib import Path

from northcua.agents.desktop.evaluation.events import DesktopEvaluationEvent
from northcua.agents.desktop.evaluation.offline import OfflineEvaluationResult

from .base import AgentTerminalRendererBase


class NorthCUAEvaluatorTerminalRenderer(AgentTerminalRendererBase):
    """Render evaluator progress with the same panel language as agents."""

    def render_header(
        self,
        *,
        app_config_path: Path | str,
        artifact_dir: Path | str,
        round_num: int,
        rollup: bool,
    ) -> str:
        """Render the top-level header for an artifact-based evaluator run.

        Args:
            app_config_path: App config used to bootstrap the evaluator.
            artifact_dir: Source artifact directory being replayed.
            round_num: Requested round number, where ``0`` means latest.
            rollup: Whether session rollup output is enabled.

        Returns:
            Formatted terminal panel text.
        """

        selected_round = "latest" if round_num <= 0 else str(round_num)
        return self._panel(
            title="NorthCUA Evaluator",
            lines=self._section_lines(
                "Replay",
                [
                    f"Artifact Dir: {Path(str(artifact_dir))}",
                    f"Round: {selected_round}",
                    f"App Config: {Path(str(app_config_path))}",
                    f"Session Rollup: {'enabled' if rollup else 'disabled'}",
                ],
                color=self.blue,
            ),
        )

    def render_event(self, event: DesktopEvaluationEvent) -> str:
        """Render one evaluator progress event.

        Args:
            event: Structured evaluator progress event.

        Returns:
            Formatted terminal panel text.
        """

        if event.name == "round_started":
            return self._render_round_started(event)
        if event.name == "bundle_built":
            return self._render_bundle(event)
        if event.name == "evidence_selected":
            return self._render_evidence(event)
        if event.name == "agentkit_final_report_completed":
            return self._render_finalizer(event)
        return self._panel(
            title=f"Evaluator Round {event.round_num}",
            lines=self._section_lines(
                event.name,
                [f"{key}: {value}" for key, value in sorted(event.payload.items())],
                color=self.cyan,
            ),
        )

    def render_footer(self, *, result: OfflineEvaluationResult) -> str:
        """Render a final evaluator summary.

        Args:
            result: Offline evaluation result and artifact paths.

        Returns:
            Formatted terminal panel text.
        """

        record = result.record
        if record is None:
            summary = ["Evaluator: disabled or skipped", f"Round: {result.round_num}", f"Steps: {result.step_count}"]
        else:
            summary = [
                f"Verdict: {record.overall_verdict.value}",
                f"Outcome: {record.outcome_label.value}",
                f"Ratings: process {record.process_rating}/5, completion {record.completion_rating}/5, confidence {record.confidence_rating}/5",
                f"Safety: {record.safety.risk_level.value}",
                f"Safety Findings: {len(record.safety.findings)}",
                f"Efficiency: {self._efficiency_summary(record.efficiency)}",
                f"Diagnoses: {self._diagnosis_summary(record.diagnoses)}",
                f"First Divergence Step: {record.first_divergence_step or 'none'}",
                f"Needs More Evidence: {self._bool_text(record.judge_metadata.needs_more_evidence)}",
                f"Reason: {record.reason}",
            ]
            if record.efficiency.reason:
                summary.append(f"Efficiency Reason: {record.efficiency.reason}")
        lines = self._section_lines("Result", summary, color=self.green if record is not None else self.yellow)
        lines.append("")
        lines.extend(
            self._path_lines(
                "Artifacts",
                [
                    ("Trace Dir", result.artifact_dir),
                    ("Structured Round", result.structured_round_dir),
                    ("Final Report", result.final_report_path),
                    ("Legacy Result", result.legacy_result_path),
                    ("Round Log", result.round_log_path),
                    ("Session Rollup", result.session_rollup_path),
                ],
                color=self.blue,
            )
        )
        return self._panel(title="Evaluator Summary", lines=lines)

    def _render_round_started(self, event: DesktopEvaluationEvent) -> str:
        return self._panel(
            title=f"Evaluator Round {event.round_num} [running...]",
            lines=self._section_lines(
                "Input",
                [
                    f"Session: {event.session_id}",
                    f"Steps: {event.payload.get('step_count', 0)}",
                    f"Request: {event.payload.get('round_request', '')}",
                ],
                color=self.magenta,
            ),
        )

    def _render_bundle(self, event: DesktopEvaluationEvent) -> str:
        return self._panel(
            title=f"Evaluator Round {event.round_num} [bundle]",
            lines=self._section_lines(
                "Bundle",
                [
                    f"Steps: {event.payload.get('step_count', 0)}",
                    f"Screenshots: {event.payload.get('screenshot_ref_count', 0)}",
                    f"Accessibility: {event.payload.get('a11y_ref_count', 0)}",
                    f"Runtime Events: {event.payload.get('runtime_ref_count', 0)}",
                ],
                color=self.cyan,
            ),
        )

    def _render_evidence(self, event: DesktopEvaluationEvent) -> str:
        selected_refs = ", ".join(self._as_str_list(event.payload.get("selected_refs"))[:12])
        pinned_refs = ", ".join(self._as_str_list(event.payload.get("pinned_refs"))[:12]) or "none"
        return self._panel(
            title=f"Evaluator Round {event.round_num} [evidence]",
            lines=self._section_lines(
                "Selected Evidence",
                [
                    f"Refs: {event.payload.get('selected_ref_count', 0)}",
                    f"Images: {event.payload.get('image_ref_count', 0)}",
                    f"Pinned: {event.payload.get('pinned_ref_count', 0)} ({pinned_refs})",
                    f"Visible Ref IDs: {selected_refs}",
                ],
                color=self.yellow,
            ),
        )

    def _render_finalizer(self, event: DesktopEvaluationEvent) -> str:
        divergence = event.payload.get("first_divergence_step") or "none"
        efficiency = self._event_efficiency_summary(event.payload)
        diagnoses = self._event_diagnosis_summary(event.payload)
        return self._panel(
            title=f"Evaluator Round {event.round_num} [final_report]",
            lines=self._section_lines(
                "Tool",
                [
                    "final_report submitted",
                    f"Verdict: {event.payload.get('verdict', 'unknown')}",
                    f"Outcome: {event.payload.get('outcome_label', 'unknown')}",
                    f"Controllability: {event.payload.get('controllability', 'unknown')}",
                    f"Ratings: process {event.payload.get('process_rating', '?')}/5, completion {event.payload.get('completion_rating', '?')}/5, confidence {event.payload.get('confidence_rating', '?')}/5",
                    f"Safety: {event.payload.get('safety_risk_level', 'unknown')} ({event.payload.get('safety_finding_count', 0)} finding(s))",
                    f"Efficiency: {efficiency}",
                    f"Diagnoses: {diagnoses}",
                    f"First Divergence Step: {divergence}",
                    f"Needs More Evidence: {self._bool_text(event.payload.get('needs_more_evidence', False))}",
                    f"Tools Exposed: {event.payload.get('tool_count', 0)}",
                    f"Tool Checks: {event.payload.get('tool_check_count', 0)}",
                    f"Duration: {event.payload.get('duration_ms', 0)} ms",
                ],
                color=self.green,
            ),
        )

    @staticmethod
    def _as_str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    @staticmethod
    def _diagnosis_summary(diagnoses: object) -> str:
        if not isinstance(diagnoses, list) or not diagnoses:
            return "none"
        counts: dict[str, int] = {}
        for diagnosis in diagnoses:
            category = getattr(getattr(diagnosis, "category", ""), "value", getattr(diagnosis, "category", ""))
            category_text = str(category or "unknown")
            counts[category_text] = counts.get(category_text, 0) + 1
        return ", ".join(f"{category}={count}" for category, count in sorted(counts.items()))

    @staticmethod
    def _event_diagnosis_summary(payload: dict[str, object]) -> str:
        count = payload.get("diagnosis_count", 0)
        breakdown = payload.get("diagnosis_category_breakdown")
        if not count:
            return "none"
        if isinstance(breakdown, dict) and breakdown:
            return ", ".join(f"{key}={value}" for key, value in sorted(breakdown.items()))
        return f"{count} finding(s)"

    @staticmethod
    def _efficiency_summary(efficiency: object) -> str:
        rating = getattr(efficiency, "rating", "?")
        unnecessary = getattr(efficiency, "unnecessary_step_count", 0)
        repeated = getattr(efficiency, "repeated_action_count", 0)
        detours = getattr(efficiency, "detour_count", 0)
        direct = getattr(efficiency, "direct_path_available", False)
        return f"{rating}/5; avoidable={unnecessary}, repeated={repeated}, detours={detours}, direct_path={NorthCUAEvaluatorTerminalRenderer._bool_text(direct)}"

    @staticmethod
    def _event_efficiency_summary(payload: dict[str, object]) -> str:
        rating = payload.get("efficiency_rating", "?")
        unnecessary = payload.get("efficiency_unnecessary_step_count", 0)
        repeated = payload.get("efficiency_repeated_action_count", 0)
        detours = payload.get("efficiency_detour_count", 0)
        direct = payload.get("efficiency_direct_path_available", False)
        return f"{rating}/5; avoidable={unnecessary}, repeated={repeated}, detours={detours}, direct_path={NorthCUAEvaluatorTerminalRenderer._bool_text(direct)}"

    @staticmethod
    def _bool_text(value: object) -> str:
        return "yes" if bool(value) else "no"


__all__ = ["NorthCUAEvaluatorTerminalRenderer"]
