"""Typed telemetry contracts for batch, sandbox, and experiment monitoring."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from northcua_protocol.capabilities import JsonObject


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp for persisted telemetry rows.

    Returns:
        Timezone-aware UTC timestamp encoded as an ISO-8601 string.
    """

    return datetime.now(timezone.utc).isoformat()


class TelemetryEntityKind(str, Enum):
    """Entity kinds that can own telemetry events."""

    EXPERIMENT = "experiment"
    BATCH = "batch"
    CASE = "case"
    SANDBOX = "sandbox"
    DEVICE = "device"
    TASK = "task"
    SESSION = "session"
    COMMAND = "command"
    EVALUATOR = "evaluator"
    ARTIFACT = "artifact"


class CasePhase(str, Enum):
    """High-level case phases used by dashboards and audit reports."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    PREPARED_SANDBOX = "prepared_sandbox"
    SANDBOX_CREATE = "sandbox_create"
    FILE_UPLOAD = "file_upload"
    STARTUP_COMMANDS = "startup_commands"
    WORKER_BOOTSTRAP = "worker_bootstrap"
    WORKER_READY = "worker_ready"
    CONTROLLER_SUBMIT = "controller_submit"
    MAIN_AGENT = "main_agent"
    EVALUATOR = "evaluator"
    ARTIFACT_VERIFICATION = "artifact_verification"
    CLEANUP = "cleanup"
    SANDBOX_TERMINATE = "sandbox_terminate"
    COMPLETED = "completed"


class CaseStatus(str, Enum):
    """Dashboard status for a single experiment or batch case."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    BOOTSTRAPPING = "bootstrapping"
    RUNNING_AGENT = "running_agent"
    EVALUATING = "evaluating"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"


class FailureCategory(str, Enum):
    """Normalized failure buckets used in telemetry summaries."""

    PREFLIGHT = "preflight"
    SANDBOX_CREATE = "sandbox_create"
    UPLOAD = "upload"
    STARTUP_COMMAND = "startup_command"
    DEVICE_BOOTSTRAP = "device_bootstrap"
    DEVICE_COMMAND_TIMEOUT = "device_command_timeout"
    DEVICE_TRANSPORT = "device_transport"
    READINESS_TIMEOUT = "readiness_timeout"
    CONTROLLER_SUBMIT = "controller_submit"
    MAIN_AGENT = "main_agent"
    EVALUATOR = "evaluator"
    ACCEPTED_INFEASIBLE = "accepted_infeasible"
    ARTIFACT_MISSING = "artifact_missing"
    TIMEOUT = "timeout"
    CLEANUP = "cleanup"
    UNKNOWN = "unknown"


class CaseArtifactKind(str, Enum):
    """Artifact classes included in per-case telemetry accounting."""

    MAIN_STEPS = "main_steps"
    MAIN_LLM_TRACE = "main_llm_trace"
    EVALUATOR_REPORT = "evaluator_report"
    EVALUATOR_EVIDENCE = "evaluator_evidence"
    EVALUATOR_LLM_TRACE = "evaluator_llm_trace"
    SCREENSHOT = "screenshot"


class TelemetryEventType(str, Enum):
    """Append-only telemetry event types."""

    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"
    STATUS_CHANGED = "status_changed"
    METRIC_OBSERVED = "metric_observed"
    ARTIFACT_RECORDED = "artifact_recorded"
    FAILURE_RECORDED = "failure_recorded"
    HEARTBEAT = "heartbeat"
    SUMMARY_UPDATED = "summary_updated"


class PhaseTiming(BaseModel):
    """One measured phase span for a case attempt.

    Attributes:
        experiment_id: Experiment namespace when available.
        batch_id: Batch namespace.
        case_id: Manifest task/case id.
        attempt: One-based attempt number.
        phase: Measured phase.
        status: Current case status when the phase was recorded.
        started_at: UTC start timestamp.
        finished_at: UTC finish timestamp.
        duration_seconds: Measured duration in seconds.
        failure_category: Normalized failure category when the phase failed.
        error: Redacted failure summary.
    """

    experiment_id: str = ""
    batch_id: str
    case_id: str
    attempt: int = 0
    phase: CasePhase
    status: CaseStatus
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    failure_category: FailureCategory | None = None
    error: str = ""


class CaseTelemetrySnapshot(BaseModel):
    """Latest dashboard projection for one case.

    Attributes:
        experiment_id: Experiment namespace when available.
        batch_id: Batch namespace.
        case_id: Manifest task/case id.
        status: Current case status.
        phase: Most recent case phase.
        attempt: Current or terminal attempt count.
        controller_task_id: Controller task id when submitted.
        trace_id: Controller trace id.
        session_id: Controller session id.
        sandbox_id: Provider sandbox id.
        device_id: Controller device id.
        started_at: First telemetry timestamp for the case.
        updated_at: Latest telemetry timestamp for the case.
        finished_at: Terminal timestamp.
        total_seconds: Elapsed case time when known.
        failure_category: Normalized terminal failure category.
        attempt_failure_category_breakdown: Counts by normalized failure
            category across failed attempts, retained even if a later retry
            succeeds.
        error: Redacted terminal error summary.
        recovery_action: Latest recovery action selected for the case.
        retry_budget_remaining: Whole-case retry budget remaining.
        last_reconciled_status: Controller status observed during resume.
        cleanup_status: Cleanup/janitor outcome for terminal cases.
        metrics: Small numeric or string metrics accumulated for the case.
    """

    experiment_id: str = ""
    batch_id: str
    case_id: str
    status: CaseStatus = CaseStatus.PENDING
    phase: CasePhase = CasePhase.QUEUED
    attempt: int = 0
    controller_task_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    sandbox_id: str = ""
    device_id: str = ""
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    total_seconds: float = 0.0
    failure_category: FailureCategory | None = None
    attempt_failure_category_breakdown: dict[str, int] = Field(default_factory=dict)
    error: str = ""
    recovery_action: str = ""
    retry_budget_remaining: int = 0
    last_reconciled_status: str = ""
    cleanup_status: str = ""
    metrics: JsonObject = Field(default_factory=dict)


class CaseArtifactTelemetry(BaseModel):
    """Artifact paths used to enrich one case's derived telemetry metrics.

    Attributes:
        case_id: Manifest task/case id.
        main_trace_dir: Directory containing main-agent trace artifacts.
        main_steps_path: Main-agent structured step trace path.
        main_llm_trace_path: Main-agent LLM trace JSONL path.
        evaluator_trace_dir: Directory containing evaluator trace artifacts.
        evaluator_report_path: Evaluator final report JSON path.
        evaluator_evidence_manifest_path: Evaluator evidence manifest JSON path.
        evaluator_llm_trace_path: Evaluator LLM trace JSONL path.
        sandbox_log_dir: Directory containing sandbox/device logs.
        screenshot_paths: Screenshot artifact paths associated with the case.
        evaluator_safety_risk_level: Verifier-parsed evaluator safety risk level.
        evaluator_efficiency_rating: Verifier-parsed evaluator efficiency rating.
        evaluator_diagnosis_category_breakdown: Verifier-parsed evaluator diagnosis counts.
    """

    case_id: str
    main_trace_dir: str = ""
    main_steps_path: str = ""
    main_llm_trace_path: str = ""
    evaluator_trace_dir: str = ""
    evaluator_report_path: str = ""
    evaluator_evidence_manifest_path: str = ""
    evaluator_llm_trace_path: str = ""
    sandbox_log_dir: str = ""
    screenshot_paths: list[str] = Field(default_factory=list)
    evaluator_safety_risk_level: str = ""
    evaluator_efficiency_rating: int = 0
    evaluator_diagnosis_category_breakdown: dict[str, int] = Field(default_factory=dict)


class CaseMetrics(BaseModel):
    """Final case-level metrics emitted into audit reports.

    Attributes:
        experiment_id: Experiment namespace when available.
        batch_id: Batch namespace.
        case_id: Manifest task/case id.
        status: Terminal or latest status.
        total_seconds: Total elapsed seconds.
        sandbox_seconds: Sandbox provisioning duration.
        prepared_sandbox_seconds: Background prepared-sandbox setup duration.
        prepared_sandbox_claim_wait_seconds: Foreground time spent waiting for a
            prepared sandbox during task claim.
        prepared_sandbox_estimated_saved_seconds: Estimated foreground
            sandbox/setup time hidden by background preparation.
        prepared_sandbox_hit: Whether this case used prepared capacity.
        prepared_sandbox_miss: Whether this case fell back from prepared capacity.
        worker_ready_seconds: Worker bootstrap/readiness duration.
        main_agent_seconds: Controller submission through terminal task duration.
        evaluator_seconds: Evaluator duration when available.
        cleanup_seconds: Cleanup and sandbox termination duration.
        retry_count: Number of retries after the first attempt.
        artifact_count: Number of artifacts observed.
        screenshot_count: Number of screenshot artifacts observed.
        artifact_bytes: Total artifact bytes observed.
        command_count: Number of controller/device commands observed.
        tool_call_count: Number of model tool calls observed when known.
        prompt_tokens: Prompt tokens observed in LLM traces when known.
        completion_tokens: Completion tokens observed in LLM traces when known.
        failure_category: Normalized terminal failure category.
        attempt_failure_category_breakdown: Counts by normalized failure
            category across failed attempts, retained even if a later retry
            succeeds.
        error: Redacted terminal error summary.
        recovery_action: Latest recovery action selected for the case.
        retry_budget_remaining: Whole-case retry budget remaining.
        last_reconciled_status: Controller status observed during resume.
        cleanup_status: Cleanup/janitor outcome for terminal cases.
        terminal_action: Shared terminal decision action when available.
        terminal_reason: Shared terminal decision reason when available.
        terminal_retryable: Whether the terminal decision allows whole-case
            retry.
        terminal_scope: Max-step scope when the terminal reason is max_steps.
        terminal_limit: Max-step limit when available.
        terminal_current: Counter value that exhausted the max-step budget.
        evaluator_safety_risk_level: Evaluator-assigned safety risk level.
        evaluator_safety_finding_count: Number of safety findings in the evaluator report.
        evaluator_efficiency_rating: Evaluator-assigned efficiency rating.
        evaluator_diagnosis_category_breakdown: Counts by evaluator diagnosis
            category for the case.
        evaluator_unnecessary_step_count: Avoidable step count from the evaluator report.
        evaluator_repeated_action_count: Repeated ineffective action count.
        evaluator_detour_count: Clear detour count.
        benchmark_hook_id: Primary post-run hook id projected as benchmark data.
        benchmark_status: Primary post-run hook status.
        benchmark_score: Primary post-run benchmark score when present.
        benchmark_passed: Whether the primary post-run hook passed.
        benchmark_error: Error from the primary post-run hook.
        reward_artifact_path: Local benchmark artifact path collected by the hook.
        main_trace_dir: Main-agent session trace directory when known.
        main_steps_path: Main-agent steps JSONL path when known.
        main_llm_trace_path: Main-agent LLM trace path when known.
        evaluator_trace_dir: Evaluator trace/report directory when known.
        evaluator_report_path: Evaluator final report path when known.
        evaluator_evidence_manifest_path: Evaluator evidence manifest path when known.
        evaluator_llm_trace_path: Evaluator LLM trace path when known.
        sandbox_log_dir: Sandbox worker log directory for this case attempt.
    """

    experiment_id: str = ""
    batch_id: str
    case_id: str
    app_profile: str = ""
    involved_app_profiles: list[str] = Field(default_factory=list)
    involved_app_count: int = 0
    operation_family: str = ""
    expected_feasibility: str = "feasible"
    status: CaseStatus
    total_seconds: float = 0.0
    sandbox_seconds: float = 0.0
    prepared_sandbox_seconds: float = 0.0
    prepared_sandbox_claim_wait_seconds: float = 0.0
    prepared_sandbox_estimated_saved_seconds: float = 0.0
    prepared_sandbox_hit: bool = False
    prepared_sandbox_miss: bool = False
    worker_ready_seconds: float = 0.0
    main_agent_seconds: float = 0.0
    evaluator_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    retry_count: int = 0
    artifact_count: int = 0
    screenshot_count: int = 0
    artifact_bytes: int = 0
    command_count: int = 0
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failure_category: FailureCategory | None = None
    attempt_failure_category_breakdown: dict[str, int] = Field(default_factory=dict)
    error: str = ""
    recovery_action: str = ""
    retry_budget_remaining: int = 0
    last_reconciled_status: str = ""
    cleanup_status: str = ""
    terminal_action: str = ""
    terminal_reason: str = ""
    terminal_retryable: bool = False
    terminal_scope: str = ""
    terminal_limit: int = 0
    terminal_current: int = 0
    evaluator_safety_risk_level: str = ""
    evaluator_safety_finding_count: int = 0
    evaluator_efficiency_rating: int = 0
    evaluator_diagnosis_category_breakdown: dict[str, int] = Field(default_factory=dict)
    evaluator_unnecessary_step_count: int = 0
    evaluator_repeated_action_count: int = 0
    evaluator_detour_count: int = 0
    benchmark_hook_id: str = ""
    benchmark_status: str = ""
    benchmark_score: float = 0.0
    benchmark_passed: bool | None = None
    benchmark_error: str = ""
    reward_artifact_path: str = ""
    main_trace_dir: str = ""
    main_steps_path: str = ""
    main_llm_trace_path: str = ""
    evaluator_trace_dir: str = ""
    evaluator_report_path: str = ""
    evaluator_evidence_manifest_path: str = ""
    evaluator_llm_trace_path: str = ""
    sandbox_log_dir: str = ""


class TelemetryGroupStats(BaseModel):
    """Aggregate metrics for one app, operation, or feasibility group.

    Attributes:
        total: Number of cases in the group.
        succeeded: Cases whose final status is succeeded.
        failed: Cases whose final status is failed.
        timed_out: Cases whose final status is timed out.
        canceled: Cases whose final status is canceled.
        success_rate: Succeeded divided by total.
        p50_total_seconds: Median total runtime for the group.
        p95_total_seconds: P95 total runtime for the group.
        avg_tool_calls: Mean tool-call count across cases.
        avg_commands: Mean shell/GUI command count across cases.
        avg_main_agent_seconds: Mean main-agent runtime in seconds.
    """

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    canceled: int = 0
    success_rate: float = 0.0
    p50_total_seconds: float = 0.0
    p95_total_seconds: float = 0.0
    avg_tool_calls: float = 0.0
    avg_commands: float = 0.0
    avg_main_agent_seconds: float = 0.0


class ExperimentTelemetrySummary(BaseModel):
    """Aggregate experiment metrics used by reports and future dashboards.

    Attributes:
        experiment_id: Experiment namespace when available.
        batch_id: Batch namespace.
        total_cases: Number of known cases.
        succeeded: Number of successful cases.
        failed: Number of failed cases.
        timed_out: Number of timed-out cases.
        canceled: Number of canceled cases.
        running: Number of non-terminal cases.
        success_rate: Fraction of cases that succeeded.
        elapsed_seconds: Total run duration when known.
        throughput_cases_per_hour: Completed case throughput.
        p50_total_seconds: Median total case duration.
        p95_total_seconds: P95 total case duration.
        failure_breakdown: Counts by normalized failure category.
        attempt_failure_breakdown: Counts by normalized failure category
            across failed attempts, including transient failures that later
            retried successfully.
        artifact_count: Total artifacts observed.
        artifact_bytes: Total artifact bytes observed.
        retry_summary: Counts of recovery actions selected across cases.
        cleanup_failures: Number of cases that succeeded with cleanup issues.
        safety_risk_breakdown: Counts by evaluator safety risk level.
        diagnosis_category_breakdown: Counts by evaluator diagnosis category.
        average_efficiency_rating: Mean evaluator efficiency rating when available.
        prepared_sandbox_hits: Cases assigned a prepared sandbox.
        prepared_sandbox_misses: Cases that fell back from prepared mode to normal acquisition.
        prepared_sandbox_miss_breakdown: Prepared fallback counts keyed by reason.
        prepared_sandbox_failures: Background prepared sandbox failures.
        prepared_sandbox_cleanup_failures: Prepared sandbox cleanup failures.
        prepared_sandbox_status_breakdown: Latest observed prepared row statuses
            from lifecycle telemetry.
        prepared_sandbox_hit_rate: Prepared hits divided by hits plus misses.
        prepared_sandbox_estimated_saved_seconds: Total estimated foreground
            sandbox/setup time hidden by background preparation.
        prepared_sandbox_p50_estimated_saved_seconds: Median per-hit estimated savings.
        prepared_sandbox_p95_estimated_saved_seconds: P95 per-hit estimated savings.
        prepared_sandbox_p50_claim_wait_seconds: Median foreground wait for prepared assignment.
        benchmark_passed: Count of primary post-run hooks that passed.
        benchmark_failed: Count of primary post-run hooks that failed benchmark criteria.
        benchmark_errors: Count of primary post-run hooks that errored or timed out.
        benchmark_avg_score: Mean benchmark score for hooks that emitted scores.
        by_primary_app: Aggregate case metrics keyed by primary app profile.
        by_involved_app: Aggregate case metrics keyed by involved app profile.
        by_app_count: Aggregate case metrics keyed by involved app count.
        by_operation_family: Aggregate case metrics keyed by operation family.
        by_expected_feasibility: Aggregate case metrics keyed by expected feasibility.
    """

    experiment_id: str = ""
    batch_id: str = ""
    total_cases: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    canceled: int = 0
    running: int = 0
    success_rate: float = 0.0
    elapsed_seconds: float = 0.0
    throughput_cases_per_hour: float = 0.0
    p50_total_seconds: float = 0.0
    p95_total_seconds: float = 0.0
    failure_breakdown: dict[str, int] = Field(default_factory=dict)
    attempt_failure_breakdown: dict[str, int] = Field(default_factory=dict)
    artifact_count: int = 0
    artifact_bytes: int = 0
    retry_summary: dict[str, int] = Field(default_factory=dict)
    cleanup_failures: int = 0
    safety_risk_breakdown: dict[str, int] = Field(default_factory=dict)
    diagnosis_category_breakdown: dict[str, int] = Field(default_factory=dict)
    average_efficiency_rating: float = 0.0
    prepared_sandbox_hits: int = 0
    prepared_sandbox_misses: int = 0
    prepared_sandbox_miss_breakdown: dict[str, int] = Field(default_factory=dict)
    prepared_sandbox_failures: int = 0
    prepared_sandbox_cleanup_failures: int = 0
    prepared_sandbox_status_breakdown: dict[str, int] = Field(default_factory=dict)
    prepared_sandbox_hit_rate: float = 0.0
    prepared_sandbox_estimated_saved_seconds: float = 0.0
    prepared_sandbox_p50_estimated_saved_seconds: float = 0.0
    prepared_sandbox_p95_estimated_saved_seconds: float = 0.0
    prepared_sandbox_p50_claim_wait_seconds: float = 0.0
    benchmark_passed: int = 0
    benchmark_failed: int = 0
    benchmark_errors: int = 0
    benchmark_avg_score: float = 0.0
    by_primary_app: dict[str, TelemetryGroupStats] = Field(default_factory=dict)
    by_involved_app: dict[str, TelemetryGroupStats] = Field(default_factory=dict)
    by_app_count: dict[str, TelemetryGroupStats] = Field(default_factory=dict)
    by_operation_family: dict[str, TelemetryGroupStats] = Field(default_factory=dict)
    by_expected_feasibility: dict[str, TelemetryGroupStats] = Field(default_factory=dict)


class TelemetryEvent(BaseModel):
    """Append-only telemetry event.

    Attributes:
        event_id: Optional backend event id after persistence.
        experiment_id: Experiment namespace when available.
        batch_id: Batch namespace.
        case_id: Manifest task/case id.
        entity_kind: Entity category that emitted the event.
        entity_id: Entity identifier.
        event_type: Stable event type.
        phase: Case phase associated with this event.
        status: Case status associated with this event.
        attempt: One-based case attempt.
        trace_id: Controller trace id when known.
        controller_task_id: Controller task id when known.
        session_id: Controller session id when known.
        sandbox_id: Provider sandbox id when known.
        device_id: Controller device id when known.
        failure_category: Normalized failure bucket.
        message: Short redacted event message.
        duration_seconds: Duration measured by the emitter.
        metrics: Small metric values observed with the event.
        details: Redacted structured details for operators.
        created_at: UTC event timestamp.
    """

    event_id: int = 0
    experiment_id: str = ""
    batch_id: str = ""
    case_id: str = ""
    entity_kind: TelemetryEntityKind
    entity_id: str
    event_type: TelemetryEventType
    phase: CasePhase | None = None
    status: CaseStatus | None = None
    attempt: int = 0
    trace_id: str = ""
    controller_task_id: str = ""
    session_id: str = ""
    sandbox_id: str = ""
    device_id: str = ""
    failure_category: FailureCategory | None = None
    message: str = ""
    duration_seconds: float = 0.0
    metrics: JsonObject = Field(default_factory=dict)
    details: JsonObject = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
