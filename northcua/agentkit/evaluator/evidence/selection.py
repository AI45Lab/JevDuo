"""Reusable evidence selection and pinning policies for evaluators."""

from __future__ import annotations

import re

from northcua.core.enums import EvaluationEvidenceKind

from ..protocol import (
    EvaluationCriterion,
    EvaluationEvidenceManifest,
    EvaluationEvidenceRef,
    EvidenceSelectionReason,
    RoundEvaluationBundle,
)
from ..spec import EvidencePolicy

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


class EvidenceSelector:
    """Select and pin evidence according to an evaluator evidence policy."""

    def __init__(self, policy: EvidencePolicy) -> None:
        self.policy = policy

    def select(
        self,
        *,
        bundle: RoundEvaluationBundle,
        criteria: list[EvaluationCriterion] | None = None,
    ) -> EvaluationEvidenceManifest:
        """Return a bounded evidence manifest for final judging.

        Args:
            bundle: Candidate evidence bundle.
            criteria: Optional rubric criteria used for relevance ranking.

        Returns:
            Manifest containing pinned evidence plus ranked selections.
        """

        criteria = list(criteria or [])
        image_refs = [ref for ref in bundle.screenshot_refs if ref.kind == EvaluationEvidenceKind.IMAGE]
        non_image_refs = (
            [*bundle.a11y_refs, *bundle.dom_refs, *bundle.runtime_event_refs]
            if self.policy.include_non_image_evidence
            else []
        )

        pinned_images = self._pinned_images(image_refs)
        selected_images = self._ranked_images(criteria, image_refs, pinned_images)
        return EvaluationEvidenceManifest(refs=[*pinned_images, *selected_images, *non_image_refs])

    def _pinned_images(self, image_refs: list[EvaluationEvidenceRef]) -> list[EvaluationEvidenceRef]:
        pinned_by_id: dict[str, EvaluationEvidenceRef] = {}
        if self.policy.include_initial_screenshot and image_refs:
            ref = self._best_endpoint_image(image_refs, first=True)
            pinned_by_id[ref.id] = ref.model_copy(
                update={
                    "pinned": True,
                    "selection_reason": EvidenceSelectionReason.PINNED_INITIAL_SCREENSHOT.value,
                }
            )
        if self.policy.include_last_screenshot and image_refs:
            ref = self._best_endpoint_image(image_refs, first=False)
            pinned_by_id[ref.id] = ref.model_copy(
                update={
                    "pinned": True,
                    "selection_reason": EvidenceSelectionReason.PINNED_LAST_SCREENSHOT.value,
                }
            )
        if self.policy.include_pinned_evidence:
            for ref in image_refs:
                if not bool(ref.metadata.get("pinned")):
                    continue
                pinned_by_id[ref.id] = ref.model_copy(
                    update={
                        "pinned": True,
                        "selection_reason": str(
                            ref.metadata.get("selection_reason")
                            or ref.metadata.get("pinned_reason")
                            or EvidenceSelectionReason.PINNED_REQUIRED_EVIDENCE.value
                        ),
                    }
                )
        return list(pinned_by_id.values())

    def _ranked_images(
        self,
        criteria: list[EvaluationCriterion],
        image_refs: list[EvaluationEvidenceRef],
        pinned_images: list[EvaluationEvidenceRef],
    ) -> list[EvaluationEvidenceRef]:
        pinned_ids = {ref.id for ref in pinned_images}
        pinned_steps = {(ref.session_step, ref.round_step) for ref in pinned_images}
        remaining = [ref for ref in image_refs if ref.id not in pinned_ids]
        budget = max(0, self.policy.screenshot_max - len(pinned_images))
        if budget <= 0:
            return []
        ranked = sorted(
            remaining,
            key=lambda ref: (
                self._image_variant_priority(ref),
                self._relevance_score(criteria, ref),
                ref.session_step,
                ref.round_step,
            ),
            reverse=True,
        )
        selected: list[EvaluationEvidenceRef] = []
        selected_steps: set[tuple[int, int]] = set()
        for ref in ranked:
            step_key = (ref.session_step, ref.round_step)
            if step_key in pinned_steps or step_key in selected_steps:
                continue
            selected.append(
                ref.model_copy(update={"selection_reason": EvidenceSelectionReason.SELECTED_BY_POLICY.value})
            )
            selected_steps.add(step_key)
            if len(selected) >= budget:
                break
        return selected

    def _relevance_score(self, criteria: list[EvaluationCriterion], ref: EvaluationEvidenceRef) -> int:
        if not criteria:
            return 0
        ref_tokens = set(self._tokenize(f"{ref.label} {ref.summary}"))
        score = 0
        for criterion in criteria:
            criterion_tokens = set(self._tokenize(f"{criterion.name} {criterion.description}"))
            score = max(score, len(ref_tokens & criterion_tokens))
        return score

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return _TOKEN_RE.findall(text.lower())

    @staticmethod
    def _image_variant_priority(ref: EvaluationEvidenceRef) -> int:
        metadata = ref.metadata
        variant = str(metadata.get("variant") or metadata.get("selected_image_kind") or "").lower()
        if variant == "terminal_verification":
            return 6
        if variant == "initial_state":
            return 5
        if variant in {"raw", "clean", "clean_screenshot", "current"}:
            return 4
        if variant in {"action_overlay", "desktop_overlay"}:
            return 3
        if variant in {"overlay", "som", "set_of_mark"}:
            return 2
        if metadata.get("tool_name"):
            return 1
        return 0

    @classmethod
    def _best_endpoint_image(cls, image_refs: list[EvaluationEvidenceRef], *, first: bool) -> EvaluationEvidenceRef:
        """Choose a stable endpoint screenshot without depending on raw ordering quirks."""

        endpoint_step = min if first else max
        step_key = endpoint_step((ref.session_step, ref.round_step) for ref in image_refs)
        candidates = [ref for ref in image_refs if (ref.session_step, ref.round_step) == step_key]
        if not first:
            terminal = [ref for ref in candidates if cls._image_variant_priority(ref) >= 5]
            return terminal[-1] if terminal else candidates[-1]
        initial = [ref for ref in candidates if _is_initial_state_image(ref)]
        if initial:
            return initial[0]
        return max(candidates, key=cls._image_variant_priority)


def _is_initial_state_image(ref: EvaluationEvidenceRef) -> bool:
    metadata = ref.metadata
    variant = str(metadata.get("variant") or metadata.get("selected_image_kind") or "").lower()
    return bool(metadata.get("initial_state")) or variant == "initial_state"


__all__ = ["EvidenceSelector"]
