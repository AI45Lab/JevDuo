"""Failure-category helpers for terminal decisions and telemetry."""

from __future__ import annotations

from northcua.telemetry.contracts import FailureCategory

_DEVICE_COMMAND_TIMEOUT_MARKERS = (
    "command_timed_out",
    "command timed out",
    "command dispatch timed out",
    "device command timed out",
    "timed out waiting for command",
)
_DEVICE_TRANSPORT_MARKERS = (
    "websocket disconnected",
    "device websocket disconnected",
    "device websocket closed",
    "device transport",
    "device connection closed",
    "device disconnected",
)
_CONTROLLER_SUBMIT_MARKERS = (
    "opening handshake",
    "controller task failed before terminal result",
    "controller submit",
    "queued task lost its submitter mapping",
    "task_accepted",
    "0 bytes read on a total of",
    "connect call failed",
    "connectionrefusederror",
)
_EVALUATOR_MARKERS = (
    "evaluator round",
    "evaluator runtime",
    "evaluator report",
    "final evaluator report",
)


def failure_category_from_message(message: object) -> FailureCategory | None:
    """Infer a normalized device failure bucket from free-form text.

    This intentionally recognizes only stable transport/command-timeout
    signatures. Broad timeout handling remains phase/status based so readiness
    and controller-submit failures do not drift into device buckets.

    Args:
        message: Free-form error, summary, or terminal text.

    Returns:
        Normalized failure category when a stable marker is present.
    """

    text = str(message or "").lower()
    if not text:
        return None
    if any(marker in text for marker in _DEVICE_COMMAND_TIMEOUT_MARKERS):
        return FailureCategory.DEVICE_COMMAND_TIMEOUT
    if any(marker in text for marker in _DEVICE_TRANSPORT_MARKERS):
        return FailureCategory.DEVICE_TRANSPORT
    if any(marker in text for marker in _CONTROLLER_SUBMIT_MARKERS):
        return FailureCategory.CONTROLLER_SUBMIT
    if any(marker in text for marker in _EVALUATOR_MARKERS):
        return FailureCategory.EVALUATOR
    return None


def failure_category_from_terminal_decision(decision: object) -> FailureCategory | None:
    """Return an explicit terminal failure category when one was provided.

    Args:
        decision: Terminal decision payload or another object from the task
            result path.

    Returns:
        Parsed failure category when the payload explicitly carries one;
        otherwise ``None``.
    """

    if not isinstance(decision, dict):
        return None
    metadata = decision.get("metadata")
    candidates: list[object] = []
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("failure_category"),
                metadata.get("last_failure_category"),
                metadata.get("failureCategory"),
            ]
        )
    candidates.append(decision.get("failure_category"))
    for raw in candidates:
        if isinstance(raw, FailureCategory):
            return raw
        if isinstance(raw, str):
            try:
                return FailureCategory(raw)
            except ValueError:
                continue
    message_category = failure_category_from_message(
        " ".join(str(decision.get(key) or "") for key in ("error", "final_answer", "summary", "reason"))
    )
    if message_category is not None:
        return message_category
    return None


__all__ = ["failure_category_from_message", "failure_category_from_terminal_decision"]
