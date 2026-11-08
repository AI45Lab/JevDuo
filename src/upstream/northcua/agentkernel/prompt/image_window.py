"""Shared prompt-facing image window allocation interfaces for CUA variants.

The allocator only changes prompt projection copies; it must not mutate the
persisted trace/history records passed by callers. Prompt image windows are
bounded by ``PromptPolicyConfig.screenshot_window_images``. The single visual
window preserves pre-action overlays and the newest clean tool-returned state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Protocol, Sequence

from northcua.agentkernel.json_types import JsonObject
from northcua.core.config import PromptPolicyConfig


class PromptHistoryTurn(Protocol):
    """Minimal replay-turn shape needed by image-window allocation.

    Attributes:
        tool_results: Prompt-facing tool result dictionaries for one replay
            turn. Image keys may include selected, raw, and overlay variants.
    """

    tool_results: list[JsonObject]


@dataclass(slots=True)
class PromptImageWindowPlan:
    """Prompt-facing image allocation across current observation and history.

    Attributes:
        observation_images: Current-user-message image URLs, usually clean
            screenshots or an overlay trace depending on projection policy.
        history_turns: Deep-copied history turns after image budget pruning.
        current_image_role: Semantic role for ``observation_images``:
            ``current_state``, ``overlay_trace``, or ``none``.
    """

    observation_images: list[str]
    history_turns: list[PromptHistoryTurn]
    current_image_role: str = "none"


from .image_window_selection import PromptImageWindowSelectionMixin


class PromptImageWindowAllocator(PromptImageWindowSelectionMixin):
    """Allocate current and historical desktop screenshots according to policy.

    Invariants:
        - ``current_clean_screenshot_url`` is the unannotated current desktop
          state and is only emitted as ``current_state``.
        - Overlay screenshots visualize the action that led to a state; they
          can be selected for history replay or, in the single-history-turn
          sliding policy case, emitted as ``overlay_trace``.
        - Tool-result ``image_*`` fields are prompt-selected aliases. Raw and
          overlay variant fields are preserved on copied history turns so the
          allocator can choose the requested variant without changing the
          persisted artifact schema.
    """

    def __init__(self, policy: PromptPolicyConfig) -> None:
        """Initialize allocation with one prompt policy.

        Args:
            policy: Prompt policy controlling history and image budgets.
        """

        self.policy = policy

    def plan_history_only(
        self,
        *,
        history_turns: Sequence[PromptHistoryTurn],
        current_clean_screenshot_url: str | None,
    ) -> PromptImageWindowPlan:
        """Build a plan that preserves history-first prompt semantics.

        Args:
            history_turns: Candidate history turns already ordered by time.
            current_clean_screenshot_url: Current clean screenshot URL, if
                available for policies that append current state.

        Returns:
            Image-window plan containing retained history and observation
            images selected under the configured policy.
        """

        retained_turns = self._retain_turns(history_turns)
        return self._plan_sliding_overlay_clean(
            retained_turns=retained_turns,
            current_clean_screenshot_url=current_clean_screenshot_url,
        )

    @staticmethod
    def count_history_images(history_turns: Sequence[PromptHistoryTurn]) -> int:
        """Count selected prompt-facing history images.

        Args:
            history_turns: Replay history candidates.

        Returns:
            Number of tool results that currently expose ``image_url`` or
            ``image_path`` to the prompt.
        """

        return sum(
            1
            for turn in history_turns
            for tool_result in turn.tool_results
            if tool_result.get("image_url") or tool_result.get("image_path")
        )

    def _retain_turns(self, history_turns: Sequence[PromptHistoryTurn]) -> list[PromptHistoryTurn]:
        """Return only turns inside the configured history-round window."""

        return list(history_turns or [])[-max(self.policy.history_window_rounds, 0) :]

    def _plan_sliding_overlay_clean(
        self,
        *,
        retained_turns: Sequence[PromptHistoryTurn],
        current_clean_screenshot_url: str | None,
    ) -> PromptImageWindowPlan:
        """Plan the sliding overlay-clean policy without changing its order.

        Candidate images form one chronological sliding sequence:
        ``overlay_1, overlay_2, ..., overlay_m, raw_m``. ``overlay_k`` is the
        pre-action image for step ``k`` with step ``k`` annotated. For ``k > 1``
        that pre-action state is the previous step's post-action state, so the
        overlay is attached to step ``k - 1`` in history. ``overlay_1`` has no
        prior tool result and uses the current user image slot when retained.
        The newest raw image remains the clean grounding state.
        """

        budget = max(self.policy.screenshot_window_images, 0)
        copied_turns = copy.deepcopy(list(retained_turns))
        self._clear_history_images(copied_turns)

        if budget <= 0:
            return PromptImageWindowPlan(observation_images=[], history_turns=copied_turns)

        if not any(self._turn_has_image_payload(turn) for turn in copied_turns):
            observation_images = []
            if self.policy.include_initial_observation_image and current_clean_screenshot_url:
                observation_images = [current_clean_screenshot_url]
            return PromptImageWindowPlan(
                observation_images=observation_images,
                history_turns=copied_turns,
                current_image_role="current_state" if observation_images else "none",
            )

        observation_images = []
        current_image_role = "none"
        candidates: list[tuple[str, int | None, dict[str, str]]] = []
        for index, turn in enumerate(copied_turns):
            overlay_image = self._extract_latest_turn_image(turn, variant="overlay")
            if overlay_image is None:
                continue
            previous_visual_index = self._previous_visual_turn_index(copied_turns, before_index=index)
            if previous_visual_index is None:
                candidates.append(("user", None, overlay_image))
            else:
                candidates.append(("history", previous_visual_index, overlay_image))

        latest_raw_index, latest_raw = self._latest_raw_turn_image(copied_turns)
        if latest_raw_index is not None and latest_raw is not None:
            candidates.append(("history", latest_raw_index, latest_raw))

        selected_candidates = candidates[-budget:]
        for target, index, image_payload in selected_candidates:
            if target == "user":
                observation_images = [image_payload["url"]]
                current_image_role = "overlay_trace"
                continue
            if index is not None:
                self._assign_turn_image_payload(copied_turns[index], image_payload)

        return PromptImageWindowPlan(
            observation_images=observation_images,
            history_turns=copied_turns,
            current_image_role=current_image_role,
        )

    def _previous_visual_turn_index(
        self,
        history_turns: Sequence[PromptHistoryTurn],
        *,
        before_index: int,
    ) -> int | None:
        """Return the nearest earlier turn that owns real image payloads.

        Text-only tools, such as ``note``, can appear between GUI actions. They
        must not become the destination for transition overlays or reset the
        visual sliding window.
        """

        for index in range(before_index - 1, -1, -1):
            if self._turn_has_image_payload(history_turns[index]):
                return index
        return None

    def _latest_raw_turn_image(
        self,
        history_turns: Sequence[PromptHistoryTurn],
    ) -> tuple[int | None, dict[str, str] | None]:
        """Return the newest raw screenshot payload across visual turns."""

        for index in range(len(history_turns) - 1, -1, -1):
            image_payload = self._extract_latest_turn_image(history_turns[index], variant="raw")
            if image_payload is not None:
                return index, image_payload
        return None, None

    def _turn_has_image_payload(self, turn: PromptHistoryTurn) -> bool:
        """Return whether a turn contains at least one real image payload."""

        return any(
            self._select_image_payload(tool_result, variant=variant) is not None
            for tool_result in turn.tool_results
            for variant in ("raw", "overlay", "selected")
        )
