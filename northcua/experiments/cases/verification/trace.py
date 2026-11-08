"""Trace and evaluator artifact verification for case suites."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from northcua.batch.contracts import BatchRunResult, BatchTaskState, BatchTaskStatus
from northcua.experiments.cases.contracts import (
    AppProfileId,
    CaseExpectedFeasibility,
    CaseSpec,
    CaseSuiteSpec,
    StrictCaseModel,
)
from northcua.experiments.verification.controller_artifacts import ControllerArtifactMaterializer
from northcua.experiments.verification.trace_helpers import ExperimentTraceVerifierHelpers


class CaseArtifactResult(StrictCaseModel):
    """Verifier-discovered trace and evaluator artifacts for one case.

    Fields:
        task_id: Case id from the suite.
        app_profile: App profile used by the case.
        status: Batch task status projected into the artifact row.
        controller_task_id: Controller-side task id when available.
        trace_id: Main agent trace id when available.
        paths: Trace, screenshot, evaluator, and report paths discovered by verification.
    """

    task_id: str
    app_profile: AppProfileId
    status: str
    controller_task_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    sandbox_id: str = ""
    query: str = ""
    fixture_remote_paths: list[str] = Field(default_factory=list)
    fixture_sha256: dict[str, str] = Field(default_factory=dict)
    main_trace_dir: str = ""
    main_steps_path: str = ""
    main_llm_trace_path: str = ""
    screenshot_paths: list[str] = Field(default_factory=list)
    final_verify_screenshot_path: str = ""
    evaluator_trace_dir: str = ""
    evaluator_report_path: str = ""
    evaluator_evidence_manifest_path: str = ""
    evaluator_llm_trace_path: str = ""
    evaluator_verdict: str = ""
    evaluator_safety_risk_level: str = ""
    evaluator_efficiency_rating: int = 0
    evaluator_diagnosis_category_breakdown: dict[str, int] = Field(default_factory=dict)
    passed: bool = False
    errors: list[str] = Field(default_factory=list)


class CaseTraceVerifier(ExperimentTraceVerifierHelpers):
    """Verify main-agent and evaluator artifacts for reusable case suites."""

    def __init__(self, *, controller_artifacts: ControllerArtifactMaterializer | None = None) -> None:
        self._controller_artifacts = controller_artifacts or ControllerArtifactMaterializer()

    def verify(
        self,
        *,
        output_dir: Path,
        spec: CaseSuiteSpec,
        batch_result: BatchRunResult,
        extra_roots: list[Path] | None = None,
        materialize_controller_artifacts: bool = True,
    ) -> list[CaseArtifactResult]:
        """Verify all case traces and return artifact rows.

        Args:
            output_dir: Case run output directory.
            spec: Case suite spec with trace expectations.
            batch_result: Authoritative batch result and task states.
            extra_roots: Additional artifact roots to search.
            materialize_controller_artifacts: Whether blobs may be projected to disk.

        Returns:
            Artifact verification rows for every runnable case in the suite.
        """

        canonical_roots = self._dedupe_roots([output_dir / "agent-artifacts", *(extra_roots or [])])
        materialized = self._controller_artifacts.materialize(
            output_dir=output_dir,
            states=batch_result.task_states,
            evaluator_kind=spec.trace_expectations.evaluator_kind,
            db_path=spec.controller.db_path,
            canonical_roots=canonical_roots,
            blob_roots=extra_roots,
            allow_writes=materialize_controller_artifacts,
        )
        roots = self._dedupe_roots([*canonical_roots, output_dir])
        if materialized is not None:
            roots = self._dedupe_roots([*canonical_roots, materialized, output_dir])
        states_by_id = {state.task_id: state for state in batch_result.task_states}
        return [
            self._verify_case(
                case=case,
                state=states_by_id.get(case.task_id),
                spec=spec,
                roots=roots,
            )
            for case in spec.tasks
            if case.disposition.is_runnable
        ]

    def _verify_case(
        self,
        *,
        case: CaseSpec,
        state: BatchTaskState | None,
        spec: CaseSuiteSpec,
        roots: list[Path],
    ) -> CaseArtifactResult:
        if state is None:
            return CaseArtifactResult(
                task_id=case.task_id,
                app_profile=case.app_profile,
                status="missing",
                query=case.query,
                fixture_remote_paths=[fixture.remote_path for fixture in case.fixtures],
                fixture_sha256={fixture.remote_path: fixture.sha256 for fixture in case.fixtures if fixture.sha256},
                errors=["batch task state missing"],
            )
        if state.status == BatchTaskStatus.PENDING and state.attempts <= 0 and not state.controller_task_id:
            return CaseArtifactResult(
                task_id=case.task_id,
                app_profile=case.app_profile,
                status=state.status.value,
                query=case.query,
                fixture_remote_paths=[fixture.remote_path for fixture in case.fixtures],
                fixture_sha256={fixture.remote_path: fixture.sha256 for fixture in case.fixtures if fixture.sha256},
                errors=["batch task not attempted"],
            )
        expectations = spec.trace_expectations
        errors: list[str] = []
        expected_infeasible = case.expected_feasibility == CaseExpectedFeasibility.INFEASIBLE
        terminal_reason = _terminal_reason(state)
        if expected_infeasible and terminal_reason != "infeasible":
            errors.append(f"expected infeasible terminal reason, got: {terminal_reason or 'missing'}")
        if not expected_infeasible and terminal_reason == "infeasible":
            errors.append("feasible case ended as infeasible")
        if state.status != BatchTaskStatus.SUCCEEDED and not (expected_infeasible and terminal_reason == "infeasible"):
            errors.append(f"batch task did not succeed: {state.status.value}")
        main_steps = self._find_main_step_trace(agent_kind=case.agent_kind, state=state, roots=roots)
        main_llm = self._find_first("llm_trace.jsonl", state=state, roots=roots, exclude_evaluations=True)
        screenshots = self._find_screenshots(state=state, roots=roots)
        valid_screenshots = [path for path in screenshots if self._is_valid_image(path)]
        terminal_screenshots = self._terminal_verification_screenshots(main_steps, valid_screenshots)
        evaluator_report = self._find_evaluator_file(
            "final_report.json",
            evaluator_kind=expectations.evaluator_kind,
            state=state,
            roots=roots,
        )
        evaluator_evidence = self._find_evaluator_file(
            "evidence_manifest.json",
            evaluator_kind=expectations.evaluator_kind,
            state=state,
            roots=roots,
        )
        evaluator_llm = self._find_evaluator_file(
            "llm_trace.jsonl",
            evaluator_kind=expectations.evaluator_kind,
            state=state,
            roots=roots,
        )
        evaluator_payload = self._evaluator_report_payload(evaluator_report)
        evaluator_verdict = self._evaluator_verdict(evaluator_payload)
        if expectations.require_main_steps and not self._non_empty(main_steps):
            errors.append(_main_steps_missing_error(case.agent_kind))
        if expectations.require_main_llm_trace and not self._non_empty(main_llm):
            errors.append("main-agent llm_trace.jsonl missing or empty")
        if expectations.require_screenshot and not valid_screenshots:
            errors.append("main-agent screenshot missing")
        if expectations.require_screenshot and not terminal_screenshots:
            errors.append("terminal verification screenshot missing")
        if expectations.require_evaluator_report and not self._non_empty(evaluator_report):
            errors.append("evaluator final_report.json missing or empty")
        if self._non_empty(evaluator_report) and evaluator_verdict != "success" and not expected_infeasible:
            errors.append(f"evaluator overall_verdict is not success: {evaluator_verdict or 'missing'}")
        if expectations.require_evaluator_report and not self._non_empty(evaluator_evidence):
            errors.append("evaluator evidence_manifest.json missing or empty")
        if expectations.require_evaluator_llm_trace and not self._non_empty(evaluator_llm):
            errors.append("evaluator llm_trace.jsonl missing or empty")
        return CaseArtifactResult(
            task_id=case.task_id,
            app_profile=case.app_profile,
            status=state.status.value,
            controller_task_id=state.controller_task_id,
            trace_id=state.trace_id,
            session_id=state.session_id,
            sandbox_id=state.sandbox_id,
            query=case.query,
            fixture_remote_paths=[fixture.remote_path for fixture in case.fixtures],
            fixture_sha256={fixture.remote_path: fixture.sha256 for fixture in case.fixtures if fixture.sha256},
            main_trace_dir=self._artifact_parent(main_steps) or self._artifact_parent(main_llm),
            main_steps_path=str(main_steps) if main_steps else "",
            main_llm_trace_path=str(main_llm) if main_llm else "",
            screenshot_paths=[str(path) for path in valid_screenshots],
            final_verify_screenshot_path=str(terminal_screenshots[-1]) if terminal_screenshots else "",
            evaluator_trace_dir=self._artifact_parent(evaluator_report) or self._artifact_parent(evaluator_llm),
            evaluator_report_path=str(evaluator_report) if evaluator_report else "",
            evaluator_evidence_manifest_path=str(evaluator_evidence) if evaluator_evidence else "",
            evaluator_llm_trace_path=str(evaluator_llm) if evaluator_llm else "",
            evaluator_verdict=evaluator_verdict,
            evaluator_safety_risk_level=self._evaluator_safety_risk_level(evaluator_payload),
            evaluator_efficiency_rating=self._evaluator_efficiency_rating(evaluator_payload),
            evaluator_diagnosis_category_breakdown=self._evaluator_diagnosis_category_breakdown(evaluator_payload),
            passed=not errors,
            errors=errors,
        )

    @staticmethod
    def _terminal_verification_screenshots(main_steps: Path | None, screenshots: list[Path]) -> list[Path]:
        terminal = _terminal_verification_expectation(main_steps)
        if terminal.path is not None:
            expected_resolved = {terminal.path.expanduser().resolve()}
            exact_matches = [path for path in screenshots if path.resolve() in expected_resolved]
            if exact_matches:
                return exact_matches
            expected_name = terminal.path.name.lower()
            expected_step = f"action_step{terminal.latest_step}_"
            return [
                path
                for path in screenshots
                if path.name.lower() == expected_name and expected_step in path.name.lower()
            ]
        if terminal.latest_step is not None:
            return []
        return [path for path in screenshots if CaseTraceVerifier._is_terminal_verification_screenshot(path)]

    def _find_main_step_trace(
        self,
        *,
        agent_kind: str,
        state: BatchTaskState,
        roots: list[Path],
    ) -> Path | None:
        if agent_kind == "web":
            web_steps = self._find_first("web_agent_trace.jsonl", state=state, roots=roots)
            if web_steps is not None:
                return web_steps
        return self._find_first("steps.jsonl", state=state, roots=roots)

    @staticmethod
    def _is_terminal_verification_screenshot(path: Path) -> bool:
        name = path.name.lower()
        text = str(path).lower()
        return name.endswith("_verify.png") or "round_end_verify" in text or "final_verify" in text


class _TerminalVerificationExpectation(StrictCaseModel):
    """Expected final verification artifact derived from the latest step row."""

    path: Path | None = None
    latest_step: int | None = None


def _terminal_verification_expectation(main_steps: Path | None) -> _TerminalVerificationExpectation:
    if main_steps is None or not main_steps.exists():
        return _TerminalVerificationExpectation()
    import json

    latest_step: int | None = None
    latest_path: Path | None = None
    for line in main_steps.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        session_step = row.get("session_step")
        is_latest = False
        if isinstance(session_step, int) and (latest_step is None or session_step >= latest_step):
            latest_step = session_step
            latest_path = None
            is_latest = True
        metadata = row.get("observation_metadata")
        if not isinstance(metadata, dict) or metadata.get("round_end_verification_status") != "success":
            continue
        screenshot_path = metadata.get("round_end_verification_screenshot_path")
        if is_latest and isinstance(screenshot_path, str) and screenshot_path:
            latest_path = Path(screenshot_path)
    return _TerminalVerificationExpectation(path=latest_path, latest_step=latest_step)


def _main_steps_missing_error(agent_kind: str) -> str:
    if agent_kind == "web":
        return "main-agent web_agent_trace.jsonl missing or empty"
    return "main-agent steps.jsonl missing or empty"


def _terminal_reason(state: BatchTaskState) -> str:
    reason = state.terminal_decision.get("reason")
    if isinstance(reason, str):
        return reason
    metadata = state.terminal_decision.get("metadata")
    if isinstance(metadata, dict):
        metadata_reason = metadata.get("reason") or metadata.get("terminal_reason")
        if isinstance(metadata_reason, str):
            return metadata_reason
    return ""
