"""Case-level telemetry recorder and phase timing helpers."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING

from northcua.telemetry.contracts import (
    CasePhase,
    CaseStatus,
    CaseTelemetrySnapshot,
    FailureCategory,
    PhaseTiming,
    TelemetryEntityKind,
    TelemetryEvent,
    TelemetryEventType,
    utc_now_iso,
)
from northcua.telemetry.failure_categories import failure_category_from_terminal_decision
from northcua.telemetry.phase import CasePhaseTimer
from northcua.telemetry.redaction import redact_details, redact_message
from northcua.telemetry.sinks import TelemetrySink

if TYPE_CHECKING:
    from northcua.batch.contracts import BatchTaskState

_STATUS_BY_BATCH_STATUS: dict[str, CaseStatus] = {
    "pending": CaseStatus.PENDING,
    "starting_sandbox": CaseStatus.PROVISIONING,
    "bootstrapping_device": CaseStatus.BOOTSTRAPPING,
    "waiting_device": CaseStatus.BOOTSTRAPPING,
    "submitted": CaseStatus.RUNNING_AGENT,
    "running": CaseStatus.RUNNING_AGENT,
    "succeeded": CaseStatus.SUCCEEDED,
    "failed": CaseStatus.FAILED,
    "timed_out": CaseStatus.TIMED_OUT,
    "canceled": CaseStatus.CANCELED,
}

_PHASE_BY_BATCH_STATUS: dict[str, CasePhase] = {
    "pending": CasePhase.QUEUED,
    "starting_sandbox": CasePhase.SANDBOX_CREATE,
    "bootstrapping_device": CasePhase.WORKER_BOOTSTRAP,
    "waiting_device": CasePhase.WORKER_READY,
    "submitted": CasePhase.CONTROLLER_SUBMIT,
    "running": CasePhase.MAIN_AGENT,
    "succeeded": CasePhase.COMPLETED,
    "failed": CasePhase.COMPLETED,
    "timed_out": CasePhase.COMPLETED,
    "canceled": CasePhase.COMPLETED,
}

_TERMINAL_STATUSES = {CaseStatus.SUCCEEDED, CaseStatus.FAILED, CaseStatus.TIMED_OUT, CaseStatus.CANCELED}
_ATTEMPT_FAILURE_CATEGORIES_KEY = "attempt_failure_categories"
_ATTEMPT_FAILURE_BREAKDOWN_KEY = "attempt_failure_category_breakdown"


class CaseTelemetryRecorder:
    """Record typed telemetry for all cases in one batch."""

    def __init__(self, *, sink: TelemetrySink, batch_id: str, experiment_id: str = "") -> None:
        self._sink = sink
        self._batch_id = batch_id
        self._experiment_id = experiment_id or batch_id
        self._snapshots: dict[str, CaseTelemetrySnapshot] = {}
        self._phase_timer = CasePhaseTimer(sink=sink, batch_id=batch_id, experiment_id=self._experiment_id)

    def record_batch_state(self, state: "BatchTaskState") -> None:
        """Project a batch task state into telemetry.

        Args:
            state: Local batch task state persisted by the scheduler.
        """

        now = utc_now_iso()
        previous = self._snapshots.get(state.task_id)
        snapshot = snapshot_from_batch_state(
            state,
            batch_id=self._batch_id,
            experiment_id=self._experiment_id,
            previous=previous,
            observed_at=now,
        )
        self._snapshots[state.task_id] = snapshot
        self._sink.append_event(
            TelemetryEvent(
                experiment_id=self._experiment_id,
                batch_id=self._batch_id,
                case_id=state.task_id,
                entity_kind=TelemetryEntityKind.CASE,
                entity_id=state.task_id,
                event_type=TelemetryEventType.STATUS_CHANGED,
                phase=snapshot.phase,
                status=snapshot.status,
                attempt=state.attempts,
                trace_id=state.trace_id,
                controller_task_id=state.controller_task_id,
                session_id=state.session_id,
                sandbox_id=state.sandbox_id,
                device_id=state.device_id,
                failure_category=snapshot.failure_category,
                message=redact_message(state.error),
            )
        )
        self._sink.upsert_snapshot(snapshot)

    def seed_batch_state(self, state: "BatchTaskState") -> None:
        """Seed in-memory retry context from authoritative state.

        This does not emit events or write snapshots; it only restores the
        previous attempt map so resumed runners and task-scoped event writers
        do not forget transient failure categories before a later success.

        Args:
            state: Authoritative batch task state loaded from SQLite.
        """

        previous = self._snapshots.get(state.task_id)
        self._snapshots[state.task_id] = snapshot_from_batch_state(
            state,
            batch_id=self._batch_id,
            experiment_id=self._experiment_id,
            previous=previous,
            observed_at=state.last_reconciled_at,
        )

    def record_case_event(
        self,
        *,
        case_id: str,
        entity_kind: TelemetryEntityKind,
        entity_id: str,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int = 0,
        message: str = "",
        details: dict[str, object] | None = None,
        sandbox_id: str = "",
        device_id: str = "",
    ) -> None:
        """Record a generic case-scoped event and update its snapshot.

        Args:
            case_id: Manifest task/case id.
            entity_kind: Entity kind that emitted the event.
            entity_id: Entity id.
            phase: Current case phase.
            status: Current case status.
            attempt: One-based attempt count when known.
            message: Short diagnostic message.
            details: Redacted structured diagnostics.
            sandbox_id: Provider sandbox id when available.
            device_id: Controller device id when available.
        """

        now = utc_now_iso()
        previous = self._snapshots.get(case_id)
        snapshot = (previous or CaseTelemetrySnapshot(batch_id=self._batch_id, case_id=case_id)).model_copy(
            update={
                "experiment_id": self._experiment_id,
                "batch_id": self._batch_id,
                "case_id": case_id,
                "status": status,
                "phase": phase,
                "sandbox_id": sandbox_id or (previous.sandbox_id if previous is not None else ""),
                "device_id": device_id or (previous.device_id if previous is not None else ""),
                "started_at": previous.started_at if previous is not None and previous.started_at else now,
                "updated_at": now,
            }
        )
        self._snapshots[case_id] = snapshot
        self._sink.append_event(
            TelemetryEvent(
                experiment_id=self._experiment_id,
                batch_id=self._batch_id,
                case_id=case_id,
                entity_kind=entity_kind,
                entity_id=entity_id,
                event_type=TelemetryEventType.STATUS_CHANGED,
                phase=phase,
                status=status,
                attempt=attempt,
                sandbox_id=sandbox_id,
                device_id=device_id,
                message=redact_message(message),
                details=redact_details(details or {}),
            )
        )
        self._sink.upsert_snapshot(snapshot)

    def phase_timer(
        self,
        *,
        case_id: str,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int = 0,
        entity_kind: TelemetryEntityKind = TelemetryEntityKind.CASE,
        entity_id: str = "",
    ) -> AbstractContextManager[None]:
        """Return a phase timing context manager.

        Args:
            case_id: Manifest task/case id.
            phase: Case phase being measured.
            status: Status associated with the phase.
            attempt: One-based attempt number.
            entity_kind: Entity kind that owns the phase.
            entity_id: Entity id; defaults to ``case_id``.

        Returns:
            Context manager that emits phase start, finish, and failure events.
        """

        return self._phase_timer.measure(
            case_id=case_id,
            phase=phase,
            status=status,
            attempt=attempt,
            entity_kind=entity_kind,
            entity_id=entity_id,
        )


def _failure_category_for_state(state: "BatchTaskState") -> FailureCategory | None:
    if state.status.value not in {"failed", "timed_out"}:
        return None
    if state.last_failure_category is not None:
        return state.last_failure_category
    terminal_failure_category = failure_category_from_terminal_decision(state.terminal_decision)
    if terminal_failure_category is not None:
        return terminal_failure_category
    if state.status.value == "timed_out":
        return FailureCategory.TIMEOUT
    if state.status.value == "failed":
        return FailureCategory.MAIN_AGENT if state.controller_task_id else FailureCategory.UNKNOWN
    return None


def _terminal_decision_metrics(state: "BatchTaskState") -> dict[str, object]:
    """Return compact terminal-decision fields for telemetry snapshots.

    Args:
        state: Batch task state that may include a controller terminal decision.

    Returns:
        Small metrics payload suitable for dashboards and audit reports.
    """

    decision = state.terminal_decision
    if not isinstance(decision, dict) or not decision:
        return {}
    metrics: dict[str, object] = {
        "terminal_action": decision.get("action", ""),
        "terminal_reason": decision.get("reason", ""),
        "terminal_retryable": decision.get("retryable", False),
    }
    metadata = decision.get("metadata")
    if isinstance(metadata, dict):
        for key in ("scope", "limit", "current"):
            if key in metadata:
                metrics[f"terminal_{key}"] = metadata[key]
    return metrics


def _attempt_failure_metrics(
    state: "BatchTaskState",
    *,
    previous: CaseTelemetrySnapshot | None,
) -> dict[str, object]:
    """Carry per-attempt failure categories across retry snapshots.

    Args:
        state: Current authoritative batch state.
        previous: Previous telemetry snapshot for this case, if any.

    Returns:
        Snapshot metric updates containing both the attempt map and an
        aggregate breakdown. The attempt map prevents duplicate counting if a
        terminal state is projected more than once.
    """

    categories: dict[str, str] = {}
    if previous is not None:
        existing = previous.metrics.get(_ATTEMPT_FAILURE_CATEGORIES_KEY)
        if isinstance(existing, dict):
            categories = {str(key): str(value) for key, value in existing.items() if value}
    failure_category = _failure_category_for_state(state)
    if failure_category is not None and state.status.value in {"failed", "timed_out"}:
        categories[str(max(state.attempts, 1))] = failure_category.value
    breakdown: dict[str, int] = {}
    for category in categories.values():
        breakdown[category] = breakdown.get(category, 0) + 1
    if not categories:
        return {}
    return {
        _ATTEMPT_FAILURE_CATEGORIES_KEY: categories,
        _ATTEMPT_FAILURE_BREAKDOWN_KEY: breakdown,
    }


def _post_run_metrics(state: "BatchTaskState") -> dict[str, object]:
    """Return compact primary benchmark fields from post-run hook results."""

    if not state.post_run_results:
        return {}
    primary = state.post_run_results[0]
    reward_artifact_path = ""
    if primary.artifacts:
        reward_artifact_path = next(iter(primary.artifacts.values()))
    return {
        "benchmark_hook_id": primary.hook_id,
        "benchmark_status": primary.status.value,
        "benchmark_score": primary.score,
        "benchmark_passed": primary.passed,
        "benchmark_error": primary.error,
        "reward_artifact_path": reward_artifact_path,
        "post_run_results": [result.model_dump(mode="json") for result in state.post_run_results],
    }


def snapshot_from_batch_state(
    state: "BatchTaskState",
    *,
    batch_id: str,
    experiment_id: str = "",
    previous: CaseTelemetrySnapshot | None = None,
    observed_at: str = "",
) -> CaseTelemetrySnapshot:
    """Project authoritative batch state into a telemetry snapshot.

    Args:
        state: Authoritative SQLite task state row.
        batch_id: Batch namespace for the snapshot.
        experiment_id: Optional experiment namespace; defaults to ``batch_id``.
        previous: Previous telemetry projection used to preserve stable timestamps.
        observed_at: Optional observation timestamp, usually from telemetry state.

    Returns:
        A normalized telemetry snapshot derived from the control-plane state.
    """

    status_value = state.status.value
    status = _STATUS_BY_BATCH_STATUS[status_value]
    phase = _PHASE_BY_BATCH_STATUS[status_value]
    unobserved_pending = (
        status == CaseStatus.PENDING
        and previous is None
        and not observed_at
        and state.attempts <= 0
        and not state.controller_task_id
    )
    now = "" if unobserved_pending else observed_at or utc_now_iso()
    started_at = previous.started_at if previous is not None and previous.started_at else now
    finished_at = previous.finished_at if previous is not None and previous.finished_at else ""
    if status in _TERMINAL_STATUSES and not finished_at:
        finished_at = now
    if status not in _TERMINAL_STATUSES:
        finished_at = ""
    metrics: dict[str, object] = {}
    if previous is not None:
        metrics.update(previous.metrics)
    attempt_failure_metrics = _attempt_failure_metrics(state, previous=previous)
    metrics.update(attempt_failure_metrics)
    metrics.update(_terminal_decision_metrics(state))
    metrics.update(_post_run_metrics(state))
    attempt_failure_breakdown = attempt_failure_metrics.get(_ATTEMPT_FAILURE_BREAKDOWN_KEY)
    if not isinstance(attempt_failure_breakdown, dict) and previous is not None:
        previous_breakdown = previous.metrics.get(_ATTEMPT_FAILURE_BREAKDOWN_KEY)
        if isinstance(previous_breakdown, dict):
            attempt_failure_breakdown = previous_breakdown
    total_seconds = _snapshot_total_seconds(started_at=started_at, finished_at=finished_at, previous=previous)
    return CaseTelemetrySnapshot(
        experiment_id=experiment_id or batch_id,
        batch_id=batch_id,
        case_id=state.task_id,
        status=status,
        phase=phase,
        attempt=state.attempts,
        controller_task_id=state.controller_task_id,
        trace_id=state.trace_id,
        session_id=state.session_id,
        sandbox_id=state.sandbox_id,
        device_id=state.device_id,
        started_at=started_at,
        updated_at=now,
        finished_at=finished_at,
        total_seconds=total_seconds,
        failure_category=_failure_category_for_state(state),
        attempt_failure_category_breakdown=(
            {str(key): int(value) for key, value in attempt_failure_breakdown.items()}
            if isinstance(attempt_failure_breakdown, dict)
            else {}
        ),
        error=redact_message(state.error),
        recovery_action=state.recovery_action.value if state.recovery_action is not None else "",
        retry_budget_remaining=state.retry_budget_remaining,
        last_reconciled_status=state.last_reconciled_status,
        cleanup_status=state.cleanup_status,
        metrics=metrics,
    )


def _snapshot_total_seconds(
    *,
    started_at: str,
    finished_at: str,
    previous: CaseTelemetrySnapshot | None,
) -> float:
    if previous is not None and previous.total_seconds > 0 and not finished_at:
        return previous.total_seconds
    if not started_at or not finished_at:
        return previous.total_seconds if previous is not None and previous.total_seconds > 0 else 0.0
    started = _parse_iso_datetime(started_at)
    finished = _parse_iso_datetime(finished_at)
    if started is None or finished is None:
        return previous.total_seconds if previous is not None and previous.total_seconds > 0 else 0.0
    return max((finished - started).total_seconds(), 0.0)


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
