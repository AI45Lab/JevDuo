"""Evidence selection policies for round evaluation."""

from __future__ import annotations

import re

from northcua.agentkit.evaluator import EvidencePolicy, EvidenceSelectionReason
from northcua.core.config import EvaluationSelectorConfig
from northcua.core.enums import EvaluationEvidenceKind, EvaluationSelectorStrategy

from .protocol import EvaluationCriterion, EvaluationEvidenceManifest, EvaluationEvidenceRef, RoundEvaluationBundle

_TOKEN_RE = re.compile("[a-z0-9_]+")


class EvidenceSelector:
    """Select evidence refs according to the configured strategy."""

    def __init__(self, config: EvaluationSelectorConfig, policy: EvidencePolicy | None = None) -> None:
        """Initialize evidence selection policy.

        Args:
            config: Selector configuration controlling strategy and limits.
            policy: AgentKit evidence policy controlling pinned evidence.
        """
        self.config = config
        self.policy = policy or EvidencePolicy(screenshot_max=config.max_images)
        self.max_images = self.policy.screenshot_max
        self.max_non_image_refs = config.max_non_image_refs

    def select(
        self, *, bundle: RoundEvaluationBundle, criteria: list[EvaluationCriterion]
    ) -> EvaluationEvidenceManifest:
        """Return the evidence manifest to pass into the judge prompt.

        Args:
            bundle: Round bundle containing all candidate evidence.
            criteria: Generated rubric criteria used for relevance ranking.

        Returns:
            Evidence manifest selected for artifact-aware judging.
        """
        refs = [*bundle.screenshot_refs, *bundle.a11y_refs, *bundle.runtime_event_refs]
        if self.config.strategy == EvaluationSelectorStrategy.ALL:
            return EvaluationEvidenceManifest(refs=self._with_pinned_evidence(bundle, refs))
        if self.config.strategy == EvaluationSelectorStrategy.HEAD_TAIL:
            return EvaluationEvidenceManifest(refs=self._with_pinned_evidence(bundle, self._head_tail(refs)))
        if self.config.strategy == EvaluationSelectorStrategy.RELEVANCE_TOPK:
            return EvaluationEvidenceManifest(
                refs=self._with_pinned_evidence(bundle, self._relevance_topk(bundle, criteria))
            )
        return EvaluationEvidenceManifest(refs=self._with_pinned_evidence(bundle, self._all_or_topk(bundle, criteria)))

    def _all_or_topk(
        self, bundle: RoundEvaluationBundle, criteria: list[EvaluationCriterion]
    ) -> list[EvaluationEvidenceRef]:
        """Return all evidence unless image volume exceeds configured limits.

        Args:
            bundle: Round bundle containing candidate evidence refs.
            criteria: Rubric criteria used to rank images when limiting.

        Returns:
            Selected evidence refs with non-image refs preserved.
        """
        image_refs = [ref for ref in bundle.screenshot_refs if ref.kind == EvaluationEvidenceKind.IMAGE]
        non_image_refs = [*bundle.a11y_refs, *bundle.runtime_event_refs]
        if len(image_refs) <= self.max_images:
            return [*_one_image_per_step(image_refs), *non_image_refs]
        selected_images = self._rank_by_criteria(criteria, image_refs)
        return [*selected_images, *non_image_refs]

    def _head_tail(self, refs: list[EvaluationEvidenceRef]) -> list[EvaluationEvidenceRef]:
        """Select leading and trailing refs for broad timeline coverage.

        Args:
            refs: Candidate evidence refs in bundle order.

        Returns:
            Head/tail refs capped by the configured image limit.
        """
        if len(refs) <= self.max_images:
            return refs
        head_count = max(self.max_images // 2, 1)
        tail_count = max(self.max_images - head_count, 1)
        return [*refs[:head_count], *refs[-tail_count:]]

    def _relevance_topk(
        self, bundle: RoundEvaluationBundle, criteria: list[EvaluationCriterion]
    ) -> list[EvaluationEvidenceRef]:
        """Select top-ranked image and non-image refs by rubric relevance.

        Args:
            bundle: Round bundle containing candidate evidence refs.
            criteria: Rubric criteria used for relevance scoring.

        Returns:
            Ranked evidence refs selected for artifact-aware judging.
        """
        ranked_images = self._rank_by_criteria(criteria, bundle.screenshot_refs)
        ranked_other = self._rank_by_criteria(criteria, [*bundle.a11y_refs, *bundle.runtime_event_refs])
        return [*ranked_images, *ranked_other]

    def _rank_by_criteria(
        self, criteria: list[EvaluationCriterion], refs: list[EvaluationEvidenceRef]
    ) -> list[EvaluationEvidenceRef]:
        """Rank candidate evidence refs against evaluator criteria.

        Args:
            criteria: Rubric criteria used for token-overlap scoring.
            refs: Candidate evidence refs to rank.

        Returns:
            Stable top-ranked refs, capped for image evidence.
        """
        if not refs:
            return []
        if not criteria:
            return _one_image_per_step(sorted(refs, key=_sort_key, reverse=True))[: self.max_images]
        chosen: dict[str, EvaluationEvidenceRef] = {}
        chosen_image_steps: set[tuple[int, int]] = set()
        for criterion in criteria:
            scored = sorted(
                refs, key=lambda ref: (self._relevance_score(criterion, ref), *_sort_key(ref)), reverse=True
            )
            count = 0
            for ref in scored:
                if ref.id in chosen:
                    continue
                if ref.kind == EvaluationEvidenceKind.IMAGE:
                    step_key = _image_step_key(ref)
                    if step_key in chosen_image_steps:
                        continue
                    chosen_image_steps.add(step_key)
                chosen[ref.id] = ref
                count += 1
                if count >= self.config.top_k_per_criterion:
                    break
        has_image_refs = any((ref.kind == EvaluationEvidenceKind.IMAGE for ref in refs))
        limit = self.max_images if has_image_refs else len(refs)
        ranked = sorted(chosen.values(), key=_sort_key, reverse=True)
        return ranked[:limit]

    def _relevance_score(self, criterion: EvaluationCriterion, ref: EvaluationEvidenceRef) -> int:
        """Compute a simple token-overlap relevance score.

        Args:
            criterion: Rubric criterion to compare.
            ref: Evidence reference to score.

        Returns:
            Count of shared normalized tokens.
        """
        criterion_tokens = set(_tokenize(f"{criterion.name} {criterion.description}"))
        ref_tokens = set(_tokenize(f"{ref.label} {ref.summary}"))
        return len(criterion_tokens & ref_tokens)

    def _with_pinned_evidence(
        self, bundle: RoundEvaluationBundle, selected_refs: list[EvaluationEvidenceRef]
    ) -> list[EvaluationEvidenceRef]:
        """Ensure policy-pinned screenshots survive selector limits."""
        selected_by_id = {
            ref.id: ref.model_copy(
                update={"selection_reason": ref.selection_reason or EvidenceSelectionReason.SELECTED_BY_POLICY.value}
            )
            for ref in selected_refs
        }
        pinned_refs = self._policy_pinned_images(bundle.screenshot_refs)
        for ref in pinned_refs:
            selected_by_id[ref.id] = ref
        ordered: list[EvaluationEvidenceRef] = []
        seen: set[str] = set()
        for ref in [*selected_refs, *pinned_refs]:
            selected = selected_by_id.get(ref.id)
            if selected is None or selected.id in seen:
                continue
            ordered.append(selected)
            seen.add(selected.id)
        return _limit_non_image_refs(
            self._respect_image_limit_after_pins(ordered, pinned_refs),
            max_non_image_refs=self.max_non_image_refs,
        )

    def _respect_image_limit_after_pins(
        self, refs: list[EvaluationEvidenceRef], pinned_refs: list[EvaluationEvidenceRef]
    ) -> list[EvaluationEvidenceRef]:
        """Keep pinned screenshots while enforcing the configured image budget.

        Args:
            refs: Selected refs in output order.
            pinned_refs: Refs that must survive image limiting.

        Returns:
            Refs with non-pinned image refs trimmed to the remaining budget.
        """
        pinned_ids = {ref.id for ref in pinned_refs}
        pinned_steps = {_image_step_key(ref) for ref in pinned_refs}
        non_pinned_image_budget = max(0, self.max_images - len(pinned_ids))
        selected: list[EvaluationEvidenceRef] = []
        non_pinned_image_count = 0
        selected_non_pinned_steps: set[tuple[int, int]] = set()
        for ref in refs:
            if ref.kind != EvaluationEvidenceKind.IMAGE:
                selected.append(ref)
                continue
            if ref.id in pinned_ids:
                selected.append(ref)
                continue
            step_key = _image_step_key(ref)
            if step_key in pinned_steps or step_key in selected_non_pinned_steps:
                continue
            if non_pinned_image_count < non_pinned_image_budget:
                selected.append(ref)
                non_pinned_image_count += 1
                selected_non_pinned_steps.add(step_key)
        return selected

    def _policy_pinned_images(self, image_refs: list[EvaluationEvidenceRef]) -> list[EvaluationEvidenceRef]:
        """Return screenshots that must be pinned by AgentKit evidence policy."""
        pinned_by_id: dict[str, EvaluationEvidenceRef] = {}
        if self.policy.include_initial_screenshot and image_refs:
            ref = self._best_image_for_step(
                image_refs,
                min(_image_step_key(item) for item in image_refs),
                prefer_latest=False,
            )
            pinned_by_id[ref.id] = ref.model_copy(
                update={"pinned": True, "selection_reason": EvidenceSelectionReason.PINNED_INITIAL_SCREENSHOT.value}
            )
        if self.policy.include_last_screenshot and image_refs:
            ref = self._best_image_for_step(
                image_refs,
                max(_image_step_key(item) for item in image_refs),
                prefer_latest=True,
            )
            pinned_by_id[ref.id] = ref.model_copy(
                update={"pinned": True, "selection_reason": EvidenceSelectionReason.PINNED_LAST_SCREENSHOT.value}
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

    @staticmethod
    def _best_image_for_step(
        image_refs: list[EvaluationEvidenceRef],
        step_key: tuple[int, int],
        *,
        prefer_latest: bool,
    ) -> EvaluationEvidenceRef:
        candidates = [ref for ref in image_refs if _image_step_key(ref) == step_key]
        if prefer_latest:
            terminal = [ref for ref in candidates if _image_variant_priority(ref) >= 5]
            return terminal[-1] if terminal else candidates[-1]
        initial = [ref for ref in candidates if _is_initial_state_image(ref)]
        if initial:
            return initial[0]
        return max(candidates, key=_sort_key)


def _is_initial_state_image(ref: EvaluationEvidenceRef) -> bool:
    """Return whether an image is explicitly marked as the starting-state screenshot."""

    metadata = ref.metadata
    variant = str(metadata.get("variant") or metadata.get("selected_image_kind") or "").lower()
    return bool(metadata.get("initial_state")) or variant == "initial_state"


def _one_image_per_step(refs: list[EvaluationEvidenceRef]) -> list[EvaluationEvidenceRef]:
    selected: list[EvaluationEvidenceRef] = []
    seen_steps: set[tuple[int, int]] = set()
    for ref in sorted(refs, key=_sort_key, reverse=True):
        if ref.kind != EvaluationEvidenceKind.IMAGE:
            selected.append(ref)
            continue
        step_key = _image_step_key(ref)
        if step_key in seen_steps:
            continue
        selected.append(ref)
        seen_steps.add(step_key)
    return selected


def _sort_key(ref: EvaluationEvidenceRef) -> tuple[int, int, int, int]:
    kind_priority = 2 if ref.kind == EvaluationEvidenceKind.IMAGE else 1
    return (kind_priority, _image_variant_priority(ref), ref.session_step, ref.round_step)


def _image_step_key(ref: EvaluationEvidenceRef) -> tuple[int, int]:
    return (ref.session_step, ref.round_step)


def _image_variant_priority(ref: EvaluationEvidenceRef) -> int:
    metadata = ref.metadata
    variant = str(metadata.get("variant") or metadata.get("selected_image_kind") or "").lower()
    if variant == "terminal_verification":
        return 5
    if variant == "overlay":
        return 4
    if variant == "raw":
        return 3
    if metadata.get("tool_name"):
        return 2
    return 1


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _limit_non_image_refs(refs: list[EvaluationEvidenceRef], *, max_non_image_refs: int) -> list[EvaluationEvidenceRef]:
    """Cap accessibility/runtime refs so long runs cannot flood prompts."""

    non_image_refs = [ref for ref in refs if ref.kind != EvaluationEvidenceKind.IMAGE]
    if len(non_image_refs) <= max_non_image_refs:
        return refs
    kept_non_image_ids = {ref.id for ref in _head_tail_non_image(non_image_refs, limit=max_non_image_refs)}
    return [ref for ref in refs if ref.kind == EvaluationEvidenceKind.IMAGE or ref.id in kept_non_image_ids]


def _head_tail_non_image(refs: list[EvaluationEvidenceRef], *, limit: int) -> list[EvaluationEvidenceRef]:
    if limit <= 0:
        return []
    head_count = max(1, limit // 2)
    tail_count = max(0, limit - head_count)
    if tail_count == 0:
        return refs[:head_count]
    return [*refs[:head_count], *refs[-tail_count:]]
