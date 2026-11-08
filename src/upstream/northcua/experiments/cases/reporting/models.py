"""Typed case-suite report projections."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from northcua.batch.contracts import BatchTaskStatus
from northcua.experiments.cases.contracts import (
    AppProfileId,
    CaseDispositionSpec,
    CaseExpectedFeasibility,
    CaseRunMode,
    CaseSetupPhase,
    LargeArtifactDeliveryEvidence,
    RecipeKind,
    StrictCaseModel,
    TemplateProfileId,
)
from northcua.telemetry.contracts import FailureCategory
from northcua_protocol.capabilities import JsonObject


class CaseTelemetryPathSet(StrictCaseModel):
    """Stable file paths emitted by batch telemetry for one run."""

    events: str = ""
    snapshots: str = ""
    timelines: str = ""
    metrics: str = ""
    summary: str = ""
    audit_report: str = ""


class CaseSetupPhaseReport(StrictCaseModel):
    """Serializable recipe and command projection for one setup phase."""

    phase: CaseSetupPhase
    recipe_kinds: list[RecipeKind] = Field(default_factory=list)
    command_labels: list[str] = Field(default_factory=list)


class CaseReportRow(StrictCaseModel):
    """One case row in the reusable suite report."""

    task_id: str
    query: str
    run_mode: CaseRunMode
    dry_run: bool = False
    app_profile: AppProfileId
    template_profile: TemplateProfileId | None = None
    template_alias: str
    involved_app_profiles: list[AppProfileId] = Field(default_factory=list)
    involved_app_count: int = 0
    expected_feasibility: CaseExpectedFeasibility = CaseExpectedFeasibility.FEASIBLE
    disposition: CaseDispositionSpec = Field(default_factory=CaseDispositionSpec)
    setup_contract: Literal["typed", "legacy"] = "legacy"
    setup_phases: list[CaseSetupPhaseReport] = Field(default_factory=list)
    operation_family: str = ""
    status: BatchTaskStatus | str
    terminal_state: str = ""
    terminal_decision: JsonObject = Field(default_factory=dict)
    cleanup_status: str = ""
    last_failure_category: FailureCategory | None = None
    quality_gate_failure_category: FailureCategory | None = None
    attempt_failure_category_breakdown: dict[str, int] = Field(default_factory=dict)
    last_reconciled_status: str = ""
    attempts: int = Field(default=0, ge=0)
    controller_task_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    sandbox_id: str = ""
    device_id: str = ""
    error: str = ""
    guest_capability_evidence: list[JsonObject] = Field(default_factory=list)
    fixture_remote_paths: list[str] = Field(default_factory=list)
    fixture_sha256: dict[str, str] = Field(default_factory=dict)
    large_artifact_delivery_evidence: list[LargeArtifactDeliveryEvidence] = Field(default_factory=list)
    artifact_count: int = 0
    screenshot_count: int = 0
    artifact_bytes: int = 0
    command_count: int = 0
    tool_call_count: int = 0
    sandbox_log_dir: str = ""
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
    benchmark_hook_id: str = ""
    benchmark_status: str = ""
    benchmark_score: float = 0.0
    benchmark_passed: bool | None = None
    benchmark_error: str = ""
    reward_artifact_path: str = ""
    post_run_results: list[JsonObject] = Field(default_factory=list)
    passed_trace_checks: bool = False
    trace_errors: list[str] = Field(default_factory=list)
    telemetry_paths: CaseTelemetryPathSet = Field(default_factory=CaseTelemetryPathSet)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_disposition_status(self) -> "CaseReportRow":
        status = self.status.value if isinstance(self.status, BatchTaskStatus) else self.status
        if self.disposition.is_runnable and status == "blocked":
            raise ValueError("runnable case report row cannot have blocked status")
        if not self.disposition.is_runnable and status != "blocked":
            raise ValueError("blocked case report row must have blocked status")
        blocked_runtime_state = (
            self.attempts,
            self.terminal_state,
            self.terminal_decision,
            self.cleanup_status,
            self.last_failure_category,
            self.quality_gate_failure_category,
            self.attempt_failure_category_breakdown,
            self.last_reconciled_status,
            self.controller_task_id,
            self.trace_id,
            self.session_id,
            self.sandbox_id,
            self.device_id,
            self.error,
            self.guest_capability_evidence,
            self.sandbox_log_dir,
            self.post_run_results,
            self.passed_trace_checks,
            self.trace_errors,
        )
        if not self.disposition.is_runnable and any(blocked_runtime_state):
            raise ValueError("blocked case report row cannot carry runtime task state")
        return self


class CaseSuiteReport(StrictCaseModel):
    """Report written after a case-suite run or dry-run."""

    version: str = "v1"
    suite_id: str
    run_id: str
    run_mode: CaseRunMode
    dry_run: bool = False
    batch_id: str
    total: int = Field(ge=0)
    runnable: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    canceled: int = Field(ge=0)
    incomplete: int = Field(default=0, ge=0)
    trace_checks_passed: int = Field(default=0, ge=0)
    trace_checks_failed: int = Field(default=0, ge=0)
    evaluator_verdict_breakdown: dict[str, int] = Field(default_factory=dict)
    benchmark_passed: int = Field(default=0, ge=0)
    benchmark_failed: int = Field(default=0, ge=0)
    benchmark_error_count: int = Field(default=0, ge=0)
    app_group_summary: JsonObject = Field(default_factory=dict)
    operation_group_summary: JsonObject = Field(default_factory=dict)
    feasibility_group_summary: JsonObject = Field(default_factory=dict)
    batch_metrics: JsonObject = Field(default_factory=dict)
    output_dir: str
    batch_output_dir: str
    sqlite_state_path: str
    audit_jsonl_path: str
    tasks_jsonl_path: str = ""
    telemetry_paths: CaseTelemetryPathSet
    cases: list[CaseReportRow]

    @model_validator(mode="after")
    def _validate_disposition_counts(self) -> "CaseSuiteReport":
        if self.runnable == 0 and self.blocked == 0:
            self.runnable = self.total
        if self.runnable + self.blocked != self.total:
            raise ValueError("case report total must equal runnable plus blocked")
        if self.succeeded + self.failed + self.canceled + self.incomplete != self.runnable:
            raise ValueError("case report terminal counts must equal runnable cases")
        if len(self.cases) != self.total:
            raise ValueError("case report total must equal the number of case rows")
        runnable_rows = [row for row in self.cases if row.disposition.is_runnable]
        if len(runnable_rows) != self.runnable:
            raise ValueError("case report runnable count must match runnable case rows")
        statuses = [
            row.status.value if isinstance(row.status, BatchTaskStatus) else row.status for row in runnable_rows
        ]
        row_counts = (
            statuses.count(BatchTaskStatus.SUCCEEDED.value),
            sum(status in {BatchTaskStatus.FAILED.value, BatchTaskStatus.TIMED_OUT.value} for status in statuses),
            statuses.count(BatchTaskStatus.CANCELED.value),
        )
        expected_incomplete = self.runnable - sum(row_counts)
        if (*row_counts, expected_incomplete) != (self.succeeded, self.failed, self.canceled, self.incomplete):
            raise ValueError("case report terminal counts must match runnable case rows")
        if self.trace_checks_passed + self.trace_checks_failed != self.runnable:
            raise ValueError("case report trace counts must equal runnable cases")
        return self
