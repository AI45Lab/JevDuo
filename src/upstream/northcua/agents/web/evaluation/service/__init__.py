"""Web evaluator service layer."""

from .round_evaluator import WebRoundEvaluatorService
from .spec import build_web_round_evaluator_spec

__all__ = ["WebRoundEvaluatorService", "build_web_round_evaluator_spec"]
