"""Prompt and image attachment helpers for desktop round evaluation."""

from __future__ import annotations

import json
import logging
from base64 import b64encode
from dataclasses import dataclass
from io import BytesIO
from mimetypes import guess_type
from pathlib import Path

from nexau.core.messages import DiscriminatedBlock, ImageBlock, Message, Role, TextBlock

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator import ArtifactPathResolver
from northcua.agentkit.evaluator.projection import EvaluatorEvidenceProjector
from northcua.app.logging import log_runtime_event
from northcua.core.config import EvaluationPromptBudgetConfig

from .protocol import EvaluationCriterion, EvaluationEvidenceManifest, EvaluationEvidenceRef, RoundEvaluationBundle
from .rendering import EvaluationTemplateRenderer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DesktopEvaluatorPromptBuildResult:
    """Evaluator prompt plus budget/projection telemetry."""

    messages: list[Message]
    projection_level: str
    estimated_tokens: int
    target_tokens: int
    hard_tokens: int


class EvaluatorPromptBudgetExceeded(RuntimeError):
    """Raised when even the tight evaluator projection exceeds its hard budget."""

    def __init__(self, *, estimated_tokens: int, hard_tokens: int, projection_level: str) -> None:
        super().__init__(
            f"Evaluator prompt projection {projection_level} exceeds hard budget "
            f"({estimated_tokens}>{hard_tokens} tokens)"
        )
        self.estimated_tokens = estimated_tokens
        self.hard_tokens = hard_tokens
        self.projection_level = projection_level


class DesktopEvaluatorPromptBuilder:
    """Build AgentKit evaluator messages and bounded image prompt blocks."""

    _PROMPT_IMAGE_MAX_EDGE = 1600
    _PROMPT_IMAGE_QUALITY = 85
    _PROMPT_IMAGE_MAX_BYTES = 10_000_000

    def __init__(
        self,
        *,
        artifact_root: Path,
        template_root: Path | None = None,
        prompt_budget: EvaluationPromptBudgetConfig | None = None,
    ) -> None:
        """Create a prompt builder rooted at a session artifact directory.

        Args:
            artifact_root: Root directory used to resolve relative evidence
                paths before attaching images to the evaluator prompt.
            template_root: Directory containing evaluator prompt templates.
                Defaults to the package ``templates`` directory.
        """

        self.artifact_root = artifact_root
        self.renderer = EvaluationTemplateRenderer(template_root or Path(__file__).parent / "templates")
        self.prompt_budget = prompt_budget or EvaluationPromptBudgetConfig()

    def build_messages(
        self,
        *,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        criteria: list[EvaluationCriterion],
    ) -> list[Message]:
        """Build the single-pass AgentKit evaluator prompt.

        Args:
            bundle: Round bundle containing compact trace and evidence refs.
            evidence_manifest: Selected evidence allowed for citation.
            criteria: Evaluation criteria generated for this round.

        Returns:
            System and user messages ready for the shared model step executor.
        """

        return self.build_prompt(
            bundle=bundle,
            evidence_manifest=evidence_manifest,
            criteria=criteria,
        ).messages

    def build_prompt(
        self,
        *,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        criteria: list[EvaluationCriterion],
        projection_level: str = "standard",
    ) -> DesktopEvaluatorPromptBuildResult:
        """Build an evaluator prompt within the configured independent budget."""

        level = "tight" if projection_level == "tight" else "standard"
        result = self._build_projected_prompt(
            bundle=bundle,
            evidence_manifest=evidence_manifest,
            criteria=criteria,
            projection_level=level,
        )
        if result.estimated_tokens <= self.prompt_budget.target_tokens or level == "tight":
            if result.estimated_tokens > self.prompt_budget.hard_tokens:
                raise EvaluatorPromptBudgetExceeded(
                    estimated_tokens=result.estimated_tokens,
                    hard_tokens=self.prompt_budget.hard_tokens,
                    projection_level=result.projection_level,
                )
            return result
        tighter = self._build_projected_prompt(
            bundle=bundle,
            evidence_manifest=evidence_manifest,
            criteria=criteria,
            projection_level="tight",
        )
        log_runtime_event(
            logger,
            "round_evaluation_prompt_projection_tightened",
            round_num=bundle.round_num,
            session_id=bundle.session_id,
            standard_estimated_tokens=result.estimated_tokens,
            tight_estimated_tokens=tighter.estimated_tokens,
            target_tokens=self.prompt_budget.target_tokens,
            hard_tokens=self.prompt_budget.hard_tokens,
        )
        if tighter.estimated_tokens > self.prompt_budget.hard_tokens:
            raise EvaluatorPromptBudgetExceeded(
                estimated_tokens=tighter.estimated_tokens,
                hard_tokens=self.prompt_budget.hard_tokens,
                projection_level=tighter.projection_level,
            )
        return tighter

    def _build_projected_prompt(
        self,
        *,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        criteria: list[EvaluationCriterion],
        projection_level: str,
    ) -> DesktopEvaluatorPromptBuildResult:
        projector = EvaluatorEvidenceProjector(bundle=bundle, evidence_manifest=evidence_manifest)
        payload: JsonObject = projector.compact_bundle(
            projection_level=projection_level,
            final_full_steps=self.prompt_budget.final_full_steps,
        )
        payload["criteria"] = [criterion.model_dump(mode="json") for criterion in criteria]
        system_prompt = self.renderer.render("agentkit_final_report_system.j2")
        user_prompt = self.renderer.render(
            "agentkit_final_report_user.j2",
            round_request=bundle.round_request,
            payload_json=json.dumps(payload, ensure_ascii=False, indent=2),
        )
        user_blocks: list[DiscriminatedBlock] = [TextBlock(text=user_prompt)]
        user_blocks.extend(self.selected_image_blocks(evidence_manifest))
        messages = [
            Message(role=Role.SYSTEM, content=[TextBlock(text=system_prompt)]),
            Message(role=Role.USER, content=user_blocks),
        ]
        return DesktopEvaluatorPromptBuildResult(
            messages=messages,
            projection_level=projection_level,
            estimated_tokens=self._estimate_prompt_tokens(messages),
            target_tokens=self.prompt_budget.target_tokens,
            hard_tokens=self.prompt_budget.hard_tokens,
        )

    def _estimate_prompt_tokens(self, messages: list[Message]) -> int:
        total_chars = 0
        image_count = 0
        for message in messages:
            for block in message.content:
                if isinstance(block, TextBlock):
                    total_chars += len(block.text)
                elif isinstance(block, ImageBlock):
                    image_count += 1
        text_tokens = int(total_chars / self.prompt_budget.chars_per_token)
        return text_tokens + image_count * 1024

    def selected_image_blocks(self, evidence_manifest: EvaluationEvidenceManifest) -> list[DiscriminatedBlock]:
        """Attach selected image evidence as provider image blocks when possible.

        Args:
            evidence_manifest: Selected evidence manifest.

        Returns:
            Text and image blocks appended to the evaluator user message.
        """

        blocks: list[DiscriminatedBlock] = []
        resolver = ArtifactPathResolver(self.artifact_root)
        for ref in evidence_manifest.refs:
            if ref.kind.value != "image" or not ref.path:
                continue
            file_path = resolver.existing_file(ref.path)
            if file_path is None:
                blocks.append(TextBlock(text=f"Image evidence {ref.id} ({ref.label}) is unavailable on disk."))
                continue
            if file_path.stat().st_size > self._PROMPT_IMAGE_MAX_BYTES:
                blocks.append(TextBlock(text=f"Image evidence {ref.id} ({ref.label}) is too large to attach."))
                continue
            mime_type = guess_type(file_path.name)[0] or "image/png"
            if not mime_type.startswith("image/"):
                blocks.append(TextBlock(text=f"Evidence {ref.id} ({ref.label}) is not an image file."))
                continue
            data_url = self.prompt_image_data_url(file_path, mime_type=mime_type)
            blocks.append(TextBlock(text=self._image_evidence_note(ref)))
            blocks.append(ImageBlock(url=data_url, detail="high"))
        return blocks

    def _image_evidence_note(self, ref: EvaluationEvidenceRef) -> str:
        """Return the text block that explains one attached image.

        Args:
            ref: Selected image evidence ref.

        Returns:
            Human-readable attachment note, including special semantics for
            terminal verification and overlay variants.
        """

        variant = str(ref.metadata.get("variant") or ref.metadata.get("selected_image_kind") or "").lower()
        return self.renderer.render(
            "agentkit_image_evidence_note.j2",
            evidence_id=ref.id,
            label=ref.label,
            variant=variant,
        )

    @classmethod
    def prompt_image_data_url(cls, file_path: Path, *, mime_type: str) -> str:
        """Return a bounded-size image data URL for evaluator prompts.

        Args:
            file_path: Image path to encode.
            mime_type: Original file MIME type used by the fallback encoder.

        Returns:
            Data URL suitable for a provider image block.
        """

        try:
            from PIL import Image

            with Image.open(file_path) as image:
                image = image.convert("RGB")
                image.thumbnail((cls._PROMPT_IMAGE_MAX_EDGE, cls._PROMPT_IMAGE_MAX_EDGE))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=cls._PROMPT_IMAGE_QUALITY, optimize=True)
                return f"data:image/jpeg;base64,{b64encode(buffer.getvalue()).decode('ascii')}"
        except Exception as exc:
            log_runtime_event(
                logger,
                "round_evaluation_prompt_image_resize_failed",
                path=str(file_path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return f"data:{mime_type};base64,{b64encode(file_path.read_bytes()).decode('ascii')}"


__all__ = [
    "DesktopEvaluatorPromptBuilder",
    "DesktopEvaluatorPromptBuildResult",
    "EvaluatorPromptBudgetExceeded",
]
