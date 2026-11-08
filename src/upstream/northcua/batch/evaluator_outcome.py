"""Helpers for projecting evaluator verdicts into batch task status."""

from __future__ import annotations

import json
from typing import Any

JsonDict = dict[str, Any]
_EVALUATION_METADATA_KEY = "evaluation"
_EVALUATION_PENDING_STATUSES = {"pending", "error"}


def evaluator_report_from_result(result: object) -> JsonDict | None:
    """Return an evaluator report embedded in a controller result payload.

    Args:
        result: Controller result object, usually decoded JSON.

    Returns:
        Normalized evaluator report mapping when one is present.
    """

    if not isinstance(result, dict):
        return None
    for key in ("evaluation_report", "round_evaluation", "final_report", "evaluation", "record"):
        report = result.get(key)
        if isinstance(report, dict):
            return {str(item_key): item for item_key, item in report.items()}
    nested = result.get("result")
    if isinstance(nested, dict):
        return evaluator_report_from_result(nested)
    return None


def evaluator_report_from_controller_result_json(result_json: object) -> JsonDict | None:
    """Return an evaluator report from a serialized controller ``result_json`` row.

    Args:
        result_json: Serialized JSON value or already-decoded mapping from batch state.

    Returns:
        Normalized evaluator report mapping when one can be decoded.
    """

    if not result_json:
        return None
    if isinstance(result_json, dict):
        return evaluator_report_from_result(result_json)
    try:
        payload = json.loads(str(result_json))
    except (TypeError, ValueError):
        return None
    return evaluator_report_from_result(payload)


def evaluator_verdict_is_success(result: object) -> bool:
    """Return whether the evaluator accepted the round as successful.

    Args:
        result: Controller result payload to inspect.

    Returns:
        ``True`` when the embedded evaluator verdict is ``success``.
    """

    report = evaluator_report_from_result(result)
    return _report_value(report, "overall_verdict") == "success"


def evaluator_report_is_present(result: object) -> bool:
    """Return whether a controller result includes an evaluator report."""

    return evaluator_report_from_result(result) is not None


def evaluator_terminal_metadata(terminal_decision: object) -> JsonDict:
    """Return evaluator lifecycle metadata embedded in terminal decision."""

    if not isinstance(terminal_decision, dict):
        return {}
    metadata = terminal_decision.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    evaluation = metadata.get(_EVALUATION_METADATA_KEY)
    if not isinstance(evaluation, dict):
        return {}
    return {str(key): value for key, value in evaluation.items()}


def evaluator_is_pending_from_terminal(terminal_decision: object) -> bool:
    """Return whether terminal metadata says evaluator infra is pending/error."""

    status = str(evaluator_terminal_metadata(terminal_decision).get("status") or "").strip().lower()
    return status in _EVALUATION_PENDING_STATUSES


def evaluator_accepts_uncontrollable_result(result: object) -> bool:
    """Return whether an evaluator accepted an otherwise uncontrollable round.

    Args:
        result: Controller result payload to inspect.

    Returns:
        ``True`` when the evaluator marks the round successful and uncontrollable.
    """

    report = evaluator_report_from_result(result)
    if _report_value(report, "overall_verdict") != "success":
        return False
    return _report_value(report, "controllability") == "uncontrollable"


def controller_result_json_accepts_uncontrollable(result_json: object) -> bool:
    """Return whether serialized controller result JSON contains accepted infeasible verdict.

    Args:
        result_json: Serialized JSON value or already-decoded mapping from batch state.

    Returns:
        ``True`` when the serialized evaluator verdict accepts uncontrollability.
    """

    report = evaluator_report_from_controller_result_json(result_json)
    if _report_value(report, "overall_verdict") != "success":
        return False
    return _report_value(report, "controllability") == "uncontrollable"


def _report_value(report: JsonDict | None, key: str) -> str:
    if not isinstance(report, dict):
        return ""
    return str(report.get(key) or "").strip().lower()
