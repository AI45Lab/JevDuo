"""Typed contracts for Web Agent observation recovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WebObservationRecoveryMode(str, Enum):
    """How the runner should obtain the observation for the next model step."""

    USE_POST_ACTION = "use_post_action"
    REUSE_CURRENT = "reuse_current"
    REFRESH_AND_ATTACH = "refresh_and_attach"


class WebObservationRecoveryStatus(str, Enum):
    """Stable recovery outcomes persisted in real tool results."""

    RECOVERED = "recovered"
    FAILED = "failed"


class WebObservationRecoveryInvariantError(RuntimeError):
    """Raised when recovery cannot be attributed to its declared tool call."""


class WebObservationRecoveryReason(str, Enum):
    """Stable reasons used by recovery policy, telemetry, and tests."""

    POST_ACTION_AVAILABLE = "post_action_available"
    PAGE_STATE_UNCHANGED = "page_state_unchanged"
    STALE_OR_MISSING_REF = "stale_or_missing_ref"
    ACTION_OUTCOME_UNCERTAIN = "action_outcome_uncertain"
    POST_ACTION_OBSERVATION_FAILED = "post_action_observation_failed"
    MUTATION_WITHOUT_OBSERVATION = "mutation_without_observation"


@dataclass(slots=True, frozen=True)
class WebObservationRecoveryPlan:
    """Policy result for one completed Web Agent command batch."""

    mode: WebObservationRecoveryMode
    reason: WebObservationRecoveryReason
    target_tool_call_id: str = ""
    trigger_code: str = ""


@dataclass(slots=True, frozen=True)
class WebObservationRecoveryResult:
    """Result of applying one observation recovery plan."""

    plan: WebObservationRecoveryPlan
    recovered: bool = False
    error: str = ""
    refs_invalidated: bool = False
