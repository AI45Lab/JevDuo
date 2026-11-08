"""Structured process-executor metric events."""

from __future__ import annotations

import logging
from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ProcessExecutorMetricName(str, Enum):
    """Metric names emitted by the controller process executor."""

    WORKER_CONNECT = "worker_connect"
    WORKER_READY = "worker_ready"
    WORKER_COMMAND_ROUNDTRIP = "worker_command_roundtrip"


class ProcessExecutorMetric(BaseModel):
    """One process-executor metric sample.

    Attributes:
        metric_name: Stable metric name used by health-report readers.
        job_id: Worker job that emitted the metric.
        task_id: Controller task associated with the worker job.
        attempt_id: Batch/controller attempt represented by the metric.
        value_ms: Metric value in milliseconds.
        metadata: Small JSON-safe dimensions or summary counters.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    metric_name: ProcessExecutorMetricName
    job_id: str
    task_id: str
    attempt_id: str
    value_ms: float
    metadata: dict[str, object] = Field(default_factory=dict)


def log_process_metric(logger: logging.Logger, metric: ProcessExecutorMetric) -> None:
    """Emit one structured process-executor metric sample.

    Args:
        logger: Module logger that receives the structured event.
        metric: Typed process-executor metric to attach to the log record.
    """

    try:
        logger.info(
            "controller process executor metric",
            extra={"process_executor_metric": metric.model_dump(mode="json")},
        )
    except Exception:
        logger.debug("failed to emit process executor metric", exc_info=True)
