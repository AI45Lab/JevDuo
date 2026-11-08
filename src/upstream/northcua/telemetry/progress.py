"""Controller progress events shared by CLI and remote subscribers."""

from enum import StrEnum


class RuntimeProgressEventType(StrEnum):
    DESKTOP_STEP = "desktop_step"
    EVALUATION_STATUS = "evaluation_status"
    EVALUATION_REPORT = "evaluation_report"


class EvaluationTelemetryState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
