"""Evaluator-specific prompt history projection."""

from __future__ import annotations

from collections.abc import Mapping

from northcua.agentkit.prompt_history import ToolResultContentProjector

from .model_tool_output import EvaluatorModelToolOutputProjector


class EvaluatorToolResultContentProjector(ToolResultContentProjector):
    """Preserve bounded evaluator tool JSON in model-visible history.

    Generic agent tools often need only a short text summary in prompt history.
    Evaluator artifact tools are different: their returned JSON is the evidence
    the evaluator needs for the next reasoning step. The tool implementations
    already bound payload sizes before returning, so replay should keep that
    structured content instead of collapsing successful outputs to
    ``Tool result [success]``.
    """

    _EVALUATOR_EVIDENCE_KEYS = frozenset(
        {
            "artifacts",
            "dom",
            "evidence_ref",
            "final_output_candidates",
            "final_output_verification",
            "matches",
            "page",
            "refs",
            "result",
            "section",
            "snapshot",
            "terminal_verification",
            "text_preview",
            "trace",
            "truncated",
            "value",
            "visible_text_candidates",
        }
    )
    image_payload_keys = ToolResultContentProjector.image_payload_keys | frozenset(
        {
            "overlay_image_url",
            "raw_image_url",
            "som_image_url",
            "selected_image_url",
            "tool_image_url",
        }
    )
    _model_output_projector = EvaluatorModelToolOutputProjector()

    def _should_preserve_full_json(self, value: object) -> bool:
        if super()._should_preserve_full_json(value):
            return True
        if not isinstance(value, Mapping):
            return False
        return any(key in value for key in self._EVALUATOR_EVIDENCE_KEYS)

    def _strip_image_payloads(self, value: object) -> object:
        return self._model_output_projector.project(value)
