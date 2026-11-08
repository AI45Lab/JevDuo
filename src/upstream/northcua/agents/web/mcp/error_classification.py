"""Stable error classification for web-agent action tools."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ..backend import (
    WebActionBackendError,
    WebActionBackendOutputError,
    WebActionBackendTimeoutError,
    WebActionBackendUnavailableError,
    WebActionCommandError,
)
from ..domain.actionability import classify_actionability_message, concise_log_summary
from ..domain.actions import WebAgentActionType


@dataclass(slots=True, frozen=True)
class WebToolErrorClassification:
    """Model-facing web tool error metadata."""

    code: str
    retryable: bool
    hint: str = ""
    reason_code: str = ""
    concise_log_summary: str = ""
    feedback_source: str = ""
    received_targets: tuple[str, ...] | None = None


_RETRYABLE_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "stale_ref",
        ("stale ref", "stale element", "reference is stale", "unknown or stale web ref"),
        "Capture a fresh snapshot and use a current numeric ref.",
    ),
    (
        "missing_ref",
        ("element not found", "no element found", "not found", "unknown ref", "ref not found", "locator resolved to 0"),
        "Capture a fresh snapshot, scroll if needed, then retry with a visible ref.",
    ),
    (
        "point_out_of_bounds",
        ("outside viewport", "out of bounds", "coordinates outside", "point outside"),
        "Use viewport CSS coordinates from the current viewport screenshot.",
    ),
    (
        "tab_not_found",
        ("tab not found", "unknown tab", "no such tab", "no tab with label", "no tab with id"),
        "Call web_list_tabs and retry with one of the returned tab refs.",
    ),
    (
        "element_not_interactable",
        ("not interactable", "not visible", "not enabled", "disabled", "element is hidden"),
        "Wait, scroll the element into view, or choose a more reliable action.",
    ),
    (
        "wrong_action_for_element",
        (
            "not a checkbox",
            "not a radio button",
            "not an editable element",
            "element is not editable",
            "element is not an <input>",
            "element is not an input",
            "element is not a <textarea>",
            "element is not a textarea",
            "element is not contenteditable",
            "not a <select> element",
            "not a select element",
        ),
        "Use the latest observation to pick an action that matches the element role, such as fill_ref for inputs or click_ref for links/buttons.",
    ),
)


def classify_web_tool_error(exc: Exception) -> WebToolErrorClassification:
    """Classify one action exception into stable model-facing metadata."""

    original_message = str(exc)
    message = original_message.lower()
    wait_targets = _wait_target_validation_targets(exc)
    if wait_targets is not None:
        received = ", ".join(wait_targets)
        if received:
            hint = (
                f"web_wait received multiple wait targets: {received}. Choose exactly one and omit the others. "
                "No target takes priority. Use duration_ms only for a timed pause; otherwise choose one page condition."
            )
            summary = f"web_wait received conflicting targets: {received}"
        else:
            hint = (
                "web_wait received no wait target. Provide exactly one of duration_ms, load_state, text, "
                "url_pattern, or selector."
            )
            summary = "web_wait received no wait target"
        return WebToolErrorClassification(
            code="invalid_wait_target_combination",
            retryable=True,
            hint=hint,
            reason_code="invalid_wait_target_combination",
            concise_log_summary=summary,
            feedback_source="contract_validator",
            received_targets=wait_targets,
        )
    if isinstance(exc, WebActionCommandError) and exc.code:
        return WebToolErrorClassification(
            code=exc.code,
            retryable=True,
            hint=exc.hint,
            reason_code=exc.reason_code or exc.code,
            concise_log_summary=exc.concise_log_summary or concise_log_summary(original_message, (exc.code,)),
            feedback_source=exc.feedback_source or "backend",
        )
    if _is_descriptor_parameter_validation_error(message):
        return WebToolErrorClassification(
            code="invalid_params",
            retryable=True,
            hint=(
                "Fix the tool arguments according to the schema and retry. For web_wait, provide exactly one of "
                "duration_ms, load_state, text, url_pattern, or selector, and omit every unused field including "
                "null placeholders."
            ),
            reason_code="invalid_params",
            concise_log_summary="Tool arguments did not match the required schema.",
            feedback_source="controller_contract_validator",
        )
    if "file:// urls are disabled" in message or "localhost urls are disabled" in message:
        return WebToolErrorClassification(code="policy_or_artifact_error", retryable=False)
    if "url scheme is not allowed" in message or "scheme is not allowed" in message:
        return WebToolErrorClassification(
            code="invalid_url_scheme",
            retryable=True,
            hint=(
                "Use an allowed http(s) URL or interact with the current page. Browser-internal schemes such as "
                "view-source:, file:, and javascript: are not available to web agent tools."
            ),
        )
    if "validation error" in message or "expects exactly one wait target" in message:
        return WebToolErrorClassification(
            code="invalid_params",
            retryable=True,
            hint=(
                "Fix the tool arguments according to the schema and retry. Omit unused optional fields instead of "
                "sending null placeholders; for web_wait, provide exactly one wait target."
            ),
        )
    actionability_feedback = classify_actionability_message(original_message, source="playwright")
    if actionability_feedback is not None:
        return WebToolErrorClassification(
            code=actionability_feedback.code,
            retryable=True,
            hint=actionability_feedback.hint,
            reason_code=actionability_feedback.code,
            concise_log_summary=actionability_feedback.concise_log_summary,
            feedback_source=actionability_feedback.feedback_source,
        )
    for code, patterns, hint in _RETRYABLE_PATTERNS:
        if any(pattern in message for pattern in patterns):
            return WebToolErrorClassification(
                code=code,
                retryable=True,
                hint=hint,
                reason_code=code,
                concise_log_summary=concise_log_summary(original_message, patterns),
                feedback_source="classifier",
            )
    if "timed out" in message or "timeout" in message:
        return WebToolErrorClassification(
            code="browser_timeout",
            retryable=True,
            hint=(
                "The browser command timed out. Use the fresh snapshot if present, wait briefly, or retry with a "
                "lighter inspection tool."
            ),
            reason_code="browser_timeout",
            concise_log_summary=concise_log_summary(original_message, ("timed out", "timeout")),
            feedback_source="playwright",
        )
    if isinstance(exc, WebActionCommandError):
        return WebToolErrorClassification(
            code="browser_action_failed",
            retryable=False,
            hint="Inspect browser state before retrying.",
            reason_code="browser_action_failed",
            concise_log_summary=concise_log_summary(original_message),
            feedback_source="backend",
        )
    if isinstance(exc, WebActionBackendUnavailableError):
        return WebToolErrorClassification(code="browser_unavailable", retryable=False)
    if isinstance(exc, WebActionBackendTimeoutError):
        return WebToolErrorClassification(
            code="browser_timeout",
            retryable=True,
            hint="The browser command timed out. Use the fresh snapshot if present, wait briefly, or retry with a lighter inspection tool.",
            reason_code="browser_timeout",
            concise_log_summary=concise_log_summary(original_message, ("timed out", "timeout")),
            feedback_source="backend",
        )
    if isinstance(exc, WebActionBackendOutputError):
        return WebToolErrorClassification(code="browser_output_invalid", retryable=False)
    if isinstance(exc, WebActionBackendError):
        return WebToolErrorClassification(code="browser_action_failed", retryable=False)
    if isinstance(exc, LookupError):
        return WebToolErrorClassification(code="session_missing", retryable=False)
    if isinstance(exc, ValidationError):
        return WebToolErrorClassification(
            code="invalid_params",
            retryable=True,
            hint=(
                "Fix the tool arguments according to the schema and retry. Omit unused optional fields instead of "
                "sending null placeholders; for web_wait, provide exactly one wait target."
            ),
        )
    if isinstance(exc, ValueError):
        if "url" in message or "scheme" in message or "network" in message or "screenshot" in message:
            return WebToolErrorClassification(code="policy_or_artifact_error", retryable=False)
        return WebToolErrorClassification(
            code="invalid_request",
            retryable=True,
            hint="Revise the request or parameters using the tool description, then retry.",
        )
    return WebToolErrorClassification(code="web_tool_error", retryable=False)


def _is_descriptor_parameter_validation_error(message: str) -> bool:
    """Return whether a controller descriptor rejected model tool arguments."""

    return any(
        marker in message
        for marker in (
            "must match exactly one schema",
            "does not match any allowed schema",
            "unexpected parameters at parameters",
            "missing required parameters at parameters",
            "does not match schema type",
            "is not in the allowed enum values",
        )
    )


def build_web_tool_error_payload(action: WebAgentActionType, exc: Exception) -> dict[str, object]:
    """Build the stable model-facing error payload shared by local and remote Web tools."""

    classification = classify_web_tool_error(exc)
    payload: dict[str, object] = {
        "status": "error",
        "action": action.value,
        "code": classification.code,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "retryable": classification.retryable,
    }
    if classification.hint:
        payload["hint"] = classification.hint
    if classification.reason_code:
        payload["reason_code"] = classification.reason_code
    if classification.concise_log_summary:
        payload["concise_log_summary"] = classification.concise_log_summary
    if classification.feedback_source:
        payload["feedback_source"] = classification.feedback_source
    if classification.received_targets is not None:
        payload["received_targets"] = list(classification.received_targets)
    return payload


def _wait_target_validation_targets(exc: Exception) -> tuple[str, ...] | None:
    if not isinstance(exc, ValidationError):
        return None
    for error in exc.errors(include_url=False):
        if error.get("type") != "web_wait_target_combination":
            continue
        context = error.get("ctx")
        raw_targets = context.get("received_targets") if isinstance(context, dict) else None
        if not isinstance(raw_targets, (list, tuple)):
            return ()
        return tuple(str(target) for target in raw_targets)
    return None
