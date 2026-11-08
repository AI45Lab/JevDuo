"""Judge pipeline for round-level evaluation."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, cast

from nexau.core.messages import DiscriminatedBlock, ImageBlock, Message, Role, TextBlock

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator import ArtifactPathResolver

from .protocol import (
    EvaluationCriterion,
    EvaluationEvidenceManifest,
    EvaluationEvidenceRef,
    EvaluationToolCheck,
    JudgeResponse,
    RoundEvaluationBundle,
)
from .rendering import EvaluationTemplateRenderer, parse_json_model


class TraceGroundingPass:
    """Text-first grounding pass that ignores multimodal evidence."""

    def __init__(self, renderer: EvaluationTemplateRenderer) -> None:
        """Initialize the text-only judge pass.

        Args:
            renderer: Shared evaluator template renderer.
        """

        self.renderer = renderer

    def build_messages(
        self,
        *,
        bundle: RoundEvaluationBundle,
        criteria_payload: list[JsonObject],
    ) -> list[Message]:
        """Build messages for the trace-grounding evaluator phase.

        Args:
            bundle: Round bundle containing step summaries and trace text.
            criteria_payload: JSON-serializable rubric criteria.

        Returns:
            System and user messages for the text-only evaluator pass.
        """

        system_prompt = self.renderer.render("evaluator_judge_system.j2", phase_name="trace_grounding")
        user_prompt = self.renderer.render(
            "evaluator_judge_user.j2",
            phase_name="trace_grounding",
            round_request=bundle.round_request,
            session_request=bundle.session_request,
            criteria=criteria_payload,
            step_summaries=bundle.step_summaries,
            tool_checks=[],
            evidence_manifest=[],
            trace_grounding_hint="",
        )
        return [
            Message(role=Role.SYSTEM, content=[TextBlock(text=system_prompt)]),
            Message(role=Role.USER, content=[TextBlock(text=user_prompt)]),
        ]


class ArtifactAwareScoringPass:
    """Final pass that scores the round against selected multimodal evidence."""

    _MAX_IMAGE_BYTES = 10_000_000

    def __init__(
        self,
        renderer: EvaluationTemplateRenderer,
        *,
        artifact_root: Path,
        max_image_bytes: int = _MAX_IMAGE_BYTES,
    ) -> None:
        """Initialize the artifact-aware judge pass.

        Args:
            renderer: Shared evaluator template renderer.
            artifact_root: Session artifact root used to resolve evidence paths.
            max_image_bytes: Maximum image bytes embedded in the model prompt.
        """

        self.renderer = renderer
        self.path_resolver = ArtifactPathResolver(artifact_root)
        self.max_image_bytes = max_image_bytes

    def build_messages(
        self,
        *,
        bundle: RoundEvaluationBundle,
        criteria_payload: list[JsonObject],
        evidence_manifest: EvaluationEvidenceManifest,
        trace_response: JudgeResponse,
        tool_checks: list[EvaluationToolCheck],
    ) -> list[Message]:
        """Build messages for the artifact-aware evaluator phase.

        Args:
            bundle: Round bundle containing trace and artifact references.
            criteria_payload: JSON-serializable rubric criteria.
            evidence_manifest: Selected evidence refs to attach or describe.
            trace_response: Text-only grounding response used as a hint.
            tool_checks: Tool checks from prior evaluator phases.

        Returns:
            System and user messages, with selected image evidence appended.
        """

        system_prompt = self.renderer.render("evaluator_judge_system.j2", phase_name="artifact_aware")
        user_blocks: list[DiscriminatedBlock] = [
            TextBlock(
                text=self.renderer.render(
                    "evaluator_judge_user.j2",
                    phase_name="artifact_aware",
                    round_request=bundle.round_request,
                    session_request=bundle.session_request,
                    criteria=criteria_payload,
                    step_summaries=bundle.step_summaries,
                    tool_checks=[tool_check.model_dump(mode="json") for tool_check in tool_checks],
                    evidence_manifest=[ref.model_dump(mode="json") for ref in evidence_manifest.refs],
                    trace_grounding_hint=trace_response.model_dump(mode="json"),
                )
            )
        ]
        for ref in evidence_manifest.refs:
            if ref.kind.value != "image" or not ref.path:
                continue
            # Only selected image refs are embedded; non-image evidence stays in
            # the manifest text so the evaluator protocol remains explicit.
            image_url = self._path_to_data_url(ref.path)
            if image_url:
                user_blocks.append(ImageBlock(url=image_url, detail="high"))
            else:
                user_blocks.append(
                    TextBlock(
                        text=(
                            f"Image evidence {ref.id} ({ref.label}) could not be attached from the artifact store; "
                            "treat that image as unavailable unless inspected through an evaluator tool."
                        )
                    )
                )
        return [
            Message(role=Role.SYSTEM, content=[TextBlock(text=system_prompt)]),
            Message(role=Role.USER, content=user_blocks),
        ]

    def _path_to_data_url(self, path: str) -> str | None:
        """Encode a local image artifact as a data URL.

        Args:
            path: Local image path from an evidence reference.

        Returns:
            Data URL when the file exists, otherwise ``None``.
        """

        from base64 import b64encode
        from mimetypes import guess_type

        file_path = self.path_resolver.existing_file(path)
        if file_path is None:
            return None
        if file_path.stat().st_size > self.max_image_bytes:
            return None
        mime_type = guess_type(file_path.name)[0] or "image/png"
        if not mime_type.startswith("image/"):
            return None
        return f"data:{mime_type};base64,{b64encode(file_path.read_bytes()).decode('ascii')}"


class JudgePipeline:
    """Own trace/text and artifact-aware judge phases."""

    def __init__(self, template_root: Path, *, artifact_root: Path | None = None) -> None:
        """Initialize both evaluator judge phases.

        Args:
            template_root: Directory containing evaluator Jinja templates.
            artifact_root: Session artifact root used for image evidence.
        """

        renderer = EvaluationTemplateRenderer(template_root)
        self.trace_pass = TraceGroundingPass(renderer)
        self.artifact_pass = ArtifactAwareScoringPass(renderer, artifact_root=artifact_root or template_root)

    def parse_response(self, payload: str) -> JudgeResponse:
        """Parse final judge JSON into the typed response model.

        Args:
            payload: Raw evaluator response text.

        Returns:
            Validated judge response.
        """

        return parse_json_model(payload, JudgeResponse)

    @staticmethod
    def criteria_payload(criteria: Iterable[EvaluationCriterion]) -> list[JsonObject]:
        """Serialize rubric criteria for evaluator templates.

        Args:
            criteria: Pydantic-like criteria objects.

        Returns:
            JSON-serializable criteria dictionaries.
        """

        return [cast(JsonObject, criterion.model_dump(mode="json")) for criterion in criteria]
