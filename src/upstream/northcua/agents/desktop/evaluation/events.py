"""Typed desktop evaluator progress events for operator presentation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from northcua.agentkernel.json_types import JsonObject


@dataclass(frozen=True, slots=True)
class DesktopEvaluationEvent:
    """One evaluator progress event emitted while a round is evaluated.

    Attributes:
        name: Stable event name, such as ``bundle_built``.
        session_id: Source desktop session id.
        round_num: Evaluated round number.
        payload: Structured event attributes suitable for logs or panels.
    """

    name: str
    session_id: str
    round_num: int
    payload: JsonObject = field(default_factory=dict)


DesktopEvaluationEventCallback = Callable[[DesktopEvaluationEvent], None]


__all__ = ["DesktopEvaluationEvent", "DesktopEvaluationEventCallback"]
