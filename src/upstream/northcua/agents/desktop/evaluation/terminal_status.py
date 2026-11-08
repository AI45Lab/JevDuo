"""Shared helpers for terminal evaluator status metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from northcua.agentkernel.json_types import JsonObject
from northcua.core.models import StepLogRecord

EVALUATION_METADATA_KEY = "evaluation"
EVALUATION_STATUS_OK = "ok"
EVALUATION_STATUS_PENDING = "pending"
EVALUATION_PROJECTION_STANDARD = "standard"
EVALUATION_PROJECTION_TIGHT = "tight"


def evaluation_metadata(
    *,
    status: str,
    attempts: int,
    projection_level: str,
    error: str = "",
    error_type: str = "",
) -> JsonObject:
    """Return compact evaluator lifecycle metadata for terminal decisions."""

    payload: JsonObject = {
        "status": status,
        "attempts": max(0, attempts),
        "projection_level": projection_level,
    }
    if error:
        payload["error"] = _truncate(error)
    if error_type:
        payload["error_type"] = error_type
    return payload


def agent_terminal_payload(step_log: StepLogRecord | None) -> JsonObject:
    """Return a compact terminal summary for the main agent."""

    if step_log is None:
        return {}
    return {
        "session_step": step_log.session_step,
        "round_step": step_log.round_step,
        "status": step_log.status.value,
        "result": _truncate(step_log.result, limit=1_000),
        "terminal_decision": dict(step_log.terminal_decision),
    }


def attach_evaluation_metadata_to_step(
    step_log: StepLogRecord,
    metadata: Mapping[str, Any],
) -> StepLogRecord:
    """Attach evaluator lifecycle metadata to a terminal step decision."""

    decision = dict(step_log.terminal_decision or {})
    decision_metadata = dict(decision.get("metadata") or {})
    decision_metadata[EVALUATION_METADATA_KEY] = {str(key): value for key, value in metadata.items()}
    decision["metadata"] = decision_metadata
    step_log.terminal_decision = decision
    return step_log


def _truncate(value: object, *, limit: int = 500) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


__all__ = [
    "EVALUATION_METADATA_KEY",
    "EVALUATION_PROJECTION_STANDARD",
    "EVALUATION_PROJECTION_TIGHT",
    "EVALUATION_STATUS_OK",
    "EVALUATION_STATUS_PENDING",
    "agent_terminal_payload",
    "attach_evaluation_metadata_to_step",
    "evaluation_metadata",
]
