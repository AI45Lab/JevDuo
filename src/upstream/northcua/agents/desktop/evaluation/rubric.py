"""Rubric generation for round-level evaluation."""

from __future__ import annotations

from pathlib import Path

from nexau.core.messages import Message, Role, TextBlock

from .protocol import EvaluationCriterion, RoundEvaluationBundle, RubricGenerationResponse
from .rendering import EvaluationTemplateRenderer, parse_json_model


class RubricGenerator:
    """Generate round-specific criteria before evidence-aware scoring."""

    def __init__(self, template_root: Path) -> None:
        """Initialize the rubric prompt renderer.

        Args:
            template_root: Directory containing evaluator prompt templates.
        """

        self.renderer = EvaluationTemplateRenderer(template_root)

    def build_messages(self, bundle: RoundEvaluationBundle) -> list[Message]:
        """Construct exact UMP messages for rubric generation.

        Args:
            bundle: Round bundle used to seed criteria generation.

        Returns:
            System and user messages for the rubric phase.
        """

        system_prompt = self.renderer.render("evaluator_rubric_system.j2")
        user_prompt = self.renderer.render(
            "evaluator_rubric_user.j2",
            session_request=bundle.session_request,
            round_request=bundle.round_request,
            prior_round_summaries=[summary.model_dump(mode="json") for summary in bundle.prior_round_summaries],
        )
        return [
            Message(role=Role.SYSTEM, content=[TextBlock(text=system_prompt)]),
            Message(role=Role.USER, content=[TextBlock(text=user_prompt)]),
        ]

    def parse_response(self, payload: str) -> RubricGenerationResponse:
        """Parse rubric JSON emitted by the evaluator LLM.

        Args:
            payload: Raw evaluator response text.

        Returns:
            Validated rubric-generation response.
        """

        return parse_json_model(payload, RubricGenerationResponse)

    @staticmethod
    def fallback_criterion(bundle_request: str) -> EvaluationCriterion:
        """Build a safe default criterion when rubric generation is empty.

        Args:
            bundle_request: Round request text used as criterion description.

        Returns:
            A single task-progress criterion.
        """

        description = bundle_request.strip() or "The round should make meaningful progress on the requested subtask."
        return EvaluationCriterion(
            id="criterion_1",
            name="Task Progress",
            description=description,
            weight=1.0,
            applicable=True,
        )
