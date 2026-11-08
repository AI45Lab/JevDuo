"""Phase timing recorder used by case telemetry."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from northcua.telemetry.contracts import (
    CasePhase,
    CaseStatus,
    FailureCategory,
    PhaseTiming,
    TelemetryEntityKind,
    TelemetryEvent,
    TelemetryEventType,
    utc_now_iso,
)
from northcua.telemetry.failure_categories import failure_category_from_message
from northcua.telemetry.redaction import redact_message
from northcua.telemetry.sinks import TelemetrySink

logger = logging.getLogger(__name__)


class CasePhaseTimer:
    """Emit phase start, finish, and failure telemetry for one batch."""

    def __init__(self, *, sink: TelemetrySink, batch_id: str, experiment_id: str) -> None:
        self._sink = sink
        self._batch_id = batch_id
        self._experiment_id = experiment_id

    @contextmanager
    def measure(
        self,
        *,
        case_id: str,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int = 0,
        entity_kind: TelemetryEntityKind = TelemetryEntityKind.CASE,
        entity_id: str = "",
    ) -> Iterator[None]:
        """Measure one phase and emit start/finish/failure telemetry.

        Args:
            case_id: Manifest task/case id.
            phase: Case phase being measured.
            status: Status associated with the phase.
            attempt: One-based attempt number.
            entity_kind: Entity kind that owns the phase.
            entity_id: Entity id; defaults to ``case_id``.

        Returns:
            Context manager that records phase telemetry around the measured
            block.

        Raises:
            Exception: Re-raises any exception from the measured block after
                recording a failure event and failed phase timing.
        """

        start_monotonic = time.monotonic()
        started_at = utc_now_iso()
        entity_id = entity_id or case_id
        self._record_phase_start(
            case_id=case_id,
            phase=phase,
            status=status,
            attempt=attempt,
            entity_kind=entity_kind,
            entity_id=entity_id,
        )
        try:
            yield
        except Exception as exc:
            duration = time.monotonic() - start_monotonic
            logger.warning(
                "case phase failed",
                extra={
                    "batch_id": self._batch_id,
                    "case_id": case_id,
                    "phase": phase.value,
                    "attempt": attempt,
                },
                exc_info=True,
            )
            self._record_phase_failure(
                case_id=case_id,
                phase=phase,
                attempt=attempt,
                entity_kind=entity_kind,
                entity_id=entity_id,
                started_at=started_at,
                duration_seconds=duration,
                error=exc,
            )
            raise
        self._record_phase_success(
            case_id=case_id,
            phase=phase,
            status=status,
            attempt=attempt,
            entity_kind=entity_kind,
            entity_id=entity_id,
            started_at=started_at,
            duration_seconds=time.monotonic() - start_monotonic,
        )

    def _record_phase_start(
        self,
        *,
        case_id: str,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int,
        entity_kind: TelemetryEntityKind,
        entity_id: str,
    ) -> None:
        self._sink.append_event(
            TelemetryEvent(
                experiment_id=self._experiment_id,
                batch_id=self._batch_id,
                case_id=case_id,
                entity_kind=entity_kind,
                entity_id=entity_id,
                event_type=TelemetryEventType.PHASE_STARTED,
                phase=phase,
                status=status,
                attempt=attempt,
            )
        )

    def _record_phase_success(
        self,
        *,
        case_id: str,
        phase: CasePhase,
        status: CaseStatus,
        attempt: int,
        entity_kind: TelemetryEntityKind,
        entity_id: str,
        started_at: str,
        duration_seconds: float,
    ) -> None:
        self._sink.record_phase(
            PhaseTiming(
                experiment_id=self._experiment_id,
                batch_id=self._batch_id,
                case_id=case_id,
                attempt=attempt,
                phase=phase,
                status=status,
                started_at=started_at,
                finished_at=utc_now_iso(),
                duration_seconds=duration_seconds,
            )
        )
        self._sink.append_event(
            TelemetryEvent(
                experiment_id=self._experiment_id,
                batch_id=self._batch_id,
                case_id=case_id,
                entity_kind=entity_kind,
                entity_id=entity_id,
                event_type=TelemetryEventType.PHASE_FINISHED,
                phase=phase,
                status=status,
                attempt=attempt,
                duration_seconds=duration_seconds,
            )
        )

    def _record_phase_failure(
        self,
        *,
        case_id: str,
        phase: CasePhase,
        attempt: int,
        entity_kind: TelemetryEntityKind,
        entity_id: str,
        started_at: str,
        duration_seconds: float,
        error: Exception,
    ) -> None:
        status = CaseStatus.TIMED_OUT if isinstance(error, TimeoutError) else CaseStatus.FAILED
        timing = PhaseTiming(
            experiment_id=self._experiment_id,
            batch_id=self._batch_id,
            case_id=case_id,
            attempt=attempt,
            phase=phase,
            status=status,
            started_at=started_at,
            finished_at=utc_now_iso(),
            duration_seconds=duration_seconds,
            failure_category=failure_category_for_phase(phase, error),
            error=redact_message(str(error)),
        )
        self._sink.record_phase(timing)
        self._sink.append_event(
            TelemetryEvent(
                experiment_id=self._experiment_id,
                batch_id=self._batch_id,
                case_id=case_id,
                entity_kind=entity_kind,
                entity_id=entity_id,
                event_type=TelemetryEventType.FAILURE_RECORDED,
                phase=phase,
                status=timing.status,
                attempt=attempt,
                failure_category=timing.failure_category,
                message=redact_message(timing.error),
                duration_seconds=duration_seconds,
            )
        )


def failure_category_for_phase(phase: CasePhase, exc: Exception) -> FailureCategory:
    """Return the normalized failure bucket for a failed phase.

    Args:
        phase: Case phase that raised.
        exc: Exception raised by the measured block.

    Returns:
        Failure category used by summaries and dashboards.
    """

    message = str(exc).lower()
    if "readiness" in message or "did not become ready" in message or "not ready" in message:
        return FailureCategory.READINESS_TIMEOUT
    message_category = failure_category_from_message(message)
    if message_category is not None:
        return message_category
    if "bootstrap" in message or "device worker" in message:
        return FailureCategory.DEVICE_BOOTSTRAP
    if "startup command" in message:
        return FailureCategory.STARTUP_COMMAND
    if "upload" in message:
        return FailureCategory.UPLOAD
    if isinstance(exc, TimeoutError):
        return FailureCategory.TIMEOUT
    if phase in {CasePhase.PREPARED_SANDBOX, CasePhase.SANDBOX_CREATE}:
        return FailureCategory.SANDBOX_CREATE
    if phase == CasePhase.FILE_UPLOAD:
        return FailureCategory.UPLOAD
    if phase == CasePhase.STARTUP_COMMANDS:
        return FailureCategory.STARTUP_COMMAND
    if phase in {CasePhase.WORKER_BOOTSTRAP, CasePhase.WORKER_READY}:
        return FailureCategory.DEVICE_BOOTSTRAP
    if phase == CasePhase.CONTROLLER_SUBMIT:
        return FailureCategory.CONTROLLER_SUBMIT
    if phase == CasePhase.MAIN_AGENT:
        return FailureCategory.MAIN_AGENT
    if phase == CasePhase.EVALUATOR:
        return FailureCategory.EVALUATOR
    if phase == CasePhase.CLEANUP:
        return FailureCategory.CLEANUP
    return FailureCategory.UNKNOWN
