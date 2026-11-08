"""Prompt construction for Web round evaluators."""

from __future__ import annotations

import logging
from base64 import b64encode
from io import BytesIO
from mimetypes import guess_type
from pathlib import Path

from nexau.core.messages import DiscriminatedBlock, ImageBlock, Message, Role, TextBlock

from northcua.agentkit.evaluator.artifacts import ArtifactPathResolver
from northcua.agentkit.evaluator.protocol import (
    EvaluationEvidenceManifest,
    EvaluationEvidenceRef,
    RoundEvaluationBundle,
)
from northcua.app.logging import log_runtime_event

from ..domain import default_web_evaluation_criteria

logger = logging.getLogger(__name__)


class WebEvaluatorPromptBuilder:
    """Build evidence-grounded Web evaluator prompts."""

    _PROMPT_IMAGE_MAX_EDGE = 1600
    _PROMPT_IMAGE_QUALITY = 85
    _PROMPT_IMAGE_MAX_BYTES = 10_000_000

    def build_messages(
        self,
        *,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        artifact_root: Path,
    ) -> list[Message]:
        """Return system/user messages for a Web round evaluator run."""

        criteria = default_web_evaluation_criteria()
        user_blocks: list[DiscriminatedBlock] = [
            TextBlock(
                text=(
                    "Evaluate this Web round.\n\n"
                    f"Task: {bundle.round_request}\n\n"
                    "Rubric criteria:\n"
                    f"{self._criteria_text(criteria)}\n\n"
                    "Selected evidence index (path-free, compact; use evaluator tools for details):\n"
                    f"{self._model_visible_manifest_text(evidence_manifest)}\n\n"
                    "Start from this compact evidence index, then use targeted tools only for missing facts. "
                    "For the evaluated trajectory, call read_round_bundle(section='trace', detail_level='standard'); "
                    "section='trajectory' is accepted as an alias. "
                    "Use query_dom_snapshot for DOM/form/page text; its visible_text_candidates field is the "
                    "fast path for answer matching. Use query_web_page_state for URL/title/final "
                    "state, query_tool_results for action outputs, and query_web_terminal_verification for final "
                    "verification. When filling final_url and final_title, copy the verified terminal/page-state "
                    "values exactly; do not normalize, pluralize, or rewrite query parameter names. "
                    "Use query_som_annotation for ref/SoM grounding; request geometry only for "
                    "specific refs when localizing a click/targeting error. Avoid detail_level='debug' and raw/all/full "
                    "bundle reads unless standard evidence is insufficient. For answer-only information "
                    "lookup tasks, the assistant final output is the answer under evaluation: do not require the "
                    "answer text to appear in the terminal page DOM. Instead, compare each material final-output "
                    "claim against DOM, screenshot, terminal page, and tool-result evidence. "
                    "query_web_page_state.final_output_verification is only an exact/partial page-text shortcut; "
                    "status='missing_dom' is not a failure when the final answer is independently supported by "
                    "earlier or terminal page evidence. Do not spend repeated steps debugging SoM/ref geometry when "
                    "the final page state and task answer can already be judged from DOM, screenshot, tool results, "
                    "or terminal verification; submit final_report instead. "
                    "Report first_divergence_step "
                    "when the evidence shows the first avoidable wrong action."
                )
            )
        ]
        user_blocks.extend(self.selected_image_blocks(evidence_manifest, artifact_root=artifact_root))
        return [
            Message(
                role=Role.SYSTEM,
                content=[
                    TextBlock(
                        text=(
                            "You are a strict Web agent evaluator. Judge only from supplied trace, DOM, clean "
                            "screenshot, action-overlay screenshot, Set-of-Mark screenshot, tool-result, terminal "
                            "verification, and explicitly bound read-only live-probe evidence. "
                            "Step current screenshot/DOM is the state before the model chose that step's action. "
                            "Clean screenshots show page pixels; action-overlay screenshots are historical action "
                            "evidence showing where an action was attempted; Set-of-Mark screenshots are ref-grounding "
                            "evidence and their labels/boxes are not page content. Tool-result images are post-action "
                            "evidence. Terminal verification is the highest-priority "
                            "final-state evidence. Treat assistant final text as the output being graded, not "
                            "page-state proof. For answer-only Web research tasks, verify the final answer's claims "
                            "against DOM, screenshot, terminal page, and tool-result evidence; the final answer "
                            "does not need to be rendered in the browser DOM. A final_output_candidate that exactly "
                            "matches visible terminal DOM text is valid completion evidence, but missing DOM text is "
                            "not a failure when the final output is otherwise fully supported by evidence. "
                            "For final_url/final_title fields, copy verified terminal/page-state strings exactly, "
                            "including query parameter spelling and order when available. "
                            "Artifact tools return compact model-facing evidence by default; prefer targeted calls and "
                            "avoid raw/debug/full bundle reads unless a necessary detail is missing. "
                            "Do not loop on DOM/SoM/ref inspection after you have enough evidence to judge completion; "
                            "the runtime may switch to a finalizer-only repair turn after repeated evidence steps. "
                            "If evidence is insufficient, use read-only evaluator tools when available or set "
                            "needs_more_evidence. Finish by calling final_report with the WebRoundEvaluationReport schema."
                        )
                    )
                ],
            ),
            Message(
                role=Role.USER,
                content=user_blocks,
            ),
        ]

    def selected_image_blocks(
        self, evidence_manifest: EvaluationEvidenceManifest, *, artifact_root: Path
    ) -> list[DiscriminatedBlock]:
        """Attach selected image evidence as provider image blocks."""

        blocks: list[DiscriminatedBlock] = []
        resolver = ArtifactPathResolver(artifact_root)
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
            blocks.append(TextBlock(text=self._image_evidence_note(ref)))
            blocks.append(ImageBlock(url=self.prompt_image_data_url(file_path, mime_type=mime_type), detail="high"))
        return blocks

    @staticmethod
    def _image_evidence_note(ref: EvaluationEvidenceRef) -> str:
        variant = str(ref.metadata.get("variant") or ref.metadata.get("selected_image_kind") or "").lower()
        if variant == "initial_state":
            return (
                f"Attached image evidence {ref.id}: {ref.label}. This is the earliest available Web screenshot "
                "after setup; use it as starting-state evidence, not final-state proof."
            )
        if variant == "terminal_verification":
            return (
                f"Attached image evidence {ref.id}: {ref.label}. This is a terminal verification screenshot "
                "captured after the Web round stopped; use it as highest-priority final-state visual evidence."
            )
        if variant in {"raw", "clean", "clean_screenshot", "current"}:
            return (
                f"Attached image evidence {ref.id}: {ref.label}. This is a clean page screenshot without "
                "ref/action annotations; use it for visual page-state evidence."
            )
        if variant in {"action_overlay", "desktop_overlay"}:
            return (
                f"Attached image evidence {ref.id}: {ref.label}. This is an action-overlay history screenshot; "
                "boxes/arrows show where the agent attempted an action. It is not current page state, and its "
                "visual labels are not page content."
            )
        if variant in {"som", "set_of_mark"}:
            return (
                f"Attached image evidence {ref.id}: {ref.label}. This is a Set-of-Mark screenshot; "
                "labels and boxes explain refs and are not page content."
            )
        if variant == "overlay":
            return (
                f"Attached image evidence {ref.id}: {ref.label}. This is an annotated overlay screenshot; "
                "treat labels and boxes as evaluator aids rather than page content."
            )
        return f"Attached image evidence {ref.id}: {ref.label}."

    @classmethod
    def _criteria_text(cls, criteria: list[object]) -> str:
        lines: list[str] = []
        for criterion in criteria:
            criterion_id = getattr(criterion, "id", "")
            name = getattr(criterion, "name", criterion_id)
            weight = getattr(criterion, "weight", None)
            description = getattr(criterion, "description", "")
            prefix = f"- {criterion_id}"
            if name and name != criterion_id:
                prefix += f" ({name})"
            if weight is not None:
                prefix += f", weight={weight}"
            line = prefix
            if description:
                line += f": {cls._compact_inline(description, 220)}"
            lines.append(line)
        return "\n".join(lines) if lines else "- (no rubric criteria)"

    @classmethod
    def _model_visible_manifest_text(cls, evidence_manifest: EvaluationEvidenceManifest) -> str:
        """Return a compact path-free evidence index for prompt text.

        The full evidence manifest may contain SoM mark arrays, bbox metadata,
        and debug paths. Those belong in artifacts/tools, not the evaluator's
        initial prompt. The prompt only needs stable ids and a short reason for
        why each ref is useful.
        """

        lines: list[str] = []
        for ref in evidence_manifest.refs:
            parts = [
                f"- {ref.id}",
                f"kind={ref.kind.value}",
                f"step={ref.session_step or ref.round_step or '-'}",
                f"label={cls._compact_inline(ref.label, 90)}",
            ]
            if ref.summary:
                parts.append(f"summary={cls._compact_inline(ref.summary, 180)}")
            if ref.selection_reason:
                parts.append(f"reason={cls._compact_inline(ref.selection_reason, 80)}")
            if ref.pinned:
                parts.append("pinned=true")
            metadata = cls._compact_metadata(ref.metadata)
            if metadata:
                parts.append(
                    "meta=" + ", ".join(f"{key}={cls._compact_inline(value, 80)}" for key, value in metadata.items())
                )
            lines.append(" | ".join(parts))
        return "\n".join(lines) if lines else "- (no selected evidence)"

    @staticmethod
    def _compact_metadata(metadata: dict[str, object]) -> dict[str, object]:
        compact: dict[str, object] = {}
        for key in (
            "variant",
            "tool_name",
            "action",
            "selected_image_kind",
            "source",
            "event",
            "terminal_verification",
            "initial_state",
        ):
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                compact[key] = value
        som_metadata = metadata.get("som_metadata")
        if isinstance(som_metadata, dict):
            marks = som_metadata.get("marks")
            compact["som_mark_count"] = len(marks) if isinstance(marks, list) else 0
            for key in ("omitted_count", "truncated", "max_marks"):
                value = som_metadata.get(key)
                if isinstance(value, (str, int, float, bool)):
                    compact[f"som_{key}"] = value
        return compact

    @staticmethod
    def _compact_inline(value: object, limit: int) -> str:
        text = str(value).replace("\n", " ").strip()
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 1)]}…"

    @classmethod
    def prompt_image_data_url(cls, file_path: Path, *, mime_type: str) -> str:
        """Return a bounded-size image data URL for evaluator prompts."""

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
                "web_evaluation_prompt_image_resize_failed",
                path=str(file_path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return f"data:{mime_type};base64,{b64encode(file_path.read_bytes()).decode('ascii')}"
