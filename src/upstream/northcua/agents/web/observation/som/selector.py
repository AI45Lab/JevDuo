"""Interaction-first Set-of-Mark candidate selection for web observations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from northcua.agentkernel.overlay.set_of_mark import SetOfMarkBox

from ...domain.observations import WebElementRef
from .domain import WebSomCandidate, WebSomMark, WebSomOmissionReason, WebSomSelection

_ROLE_PRIORITY = {
    "textbox": 10,
    "searchbox": 10,
    "combobox": 10,
    "checkbox": 9,
    "radio": 9,
    "switch": 9,
    "button": 8,
    "menuitem": 8,
    "tab": 8,
    "link": 6,
    "slider": 5,
    "spinbutton": 5,
    "option": 4,
}
_TAG_PRIORITY = {"input": 9, "textarea": 9, "select": 9, "button": 8, "a": 6}


@dataclass(frozen=True, slots=True)
class WebSomSelectionConfig:
    """Runtime knobs for interaction-first SoM selection."""

    max_marks: int = 150
    max_box_lookup: int = 512
    min_bbox_css_px: float = 3.0


class InteractionFirstSomSelector:
    """Select SoM marks while preserving model-clickable refs.

    The browser snapshot is treated as the source of interactive candidates.
    Selection only applies hard filters that make a mark impossible or unsafe
    to interact with: missing/invalid geometry, hidden/disabled state,
    off-viewport geometry, or tiny boxes. It intentionally avoids domain,
    overlap, and dense-link heuristics so visible refs remain clickable.
    """

    def __init__(self, config: WebSomSelectionConfig) -> None:
        self.config = config

    def select(
        self,
        *,
        elements: list[WebElementRef],
        viewport_width: float,
        viewport_height: float,
        box_payloads: Mapping[str, object],
        state_payloads: Mapping[str, Mapping[str, bool]],
    ) -> WebSomSelection:
        """Return selected marks and typed omission accounting."""

        omission_counts: Counter[WebSomOmissionReason] = Counter()
        lookup_elements = elements[: self.config.max_box_lookup]
        over_lookup = max(0, len(elements) - len(lookup_elements))
        if over_lookup:
            omission_counts[WebSomOmissionReason.OVER_BOX_LOOKUP_LIMIT] += over_lookup

        candidates: list[WebSomCandidate] = []
        for element in lookup_elements:
            attr_reason = self._attribute_omission_reason(element)
            if attr_reason is not None:
                omission_counts[attr_reason] += 1
                continue
            state = state_payloads.get(element.model_ref, {})
            if state.get("visible") is False:
                omission_counts[WebSomOmissionReason.STATE_HIDDEN] += 1
                continue
            if state.get("enabled") is False:
                omission_counts[WebSomOmissionReason.STATE_DISABLED] += 1
                continue
            box = self._box_from_payload(box_payloads.get(element.model_ref))
            if box is None:
                omission_counts[WebSomOmissionReason.MISSING_BBOX] += 1
                continue
            if not self._box_intersects_viewport(box, viewport_width, viewport_height):
                omission_counts[WebSomOmissionReason.OUTSIDE_VIEWPORT] += 1
                continue
            clipped = self._clip_box_to_viewport(box, viewport_width, viewport_height)
            if clipped is None:
                omission_counts[WebSomOmissionReason.OUTSIDE_VIEWPORT] += 1
                continue
            if clipped.width < self.config.min_bbox_css_px or clipped.height < self.config.min_bbox_css_px:
                omission_counts[WebSomOmissionReason.TINY_BBOX] += 1
                continue
            candidates.append(
                WebSomCandidate(
                    element=element,
                    bbox_css=clipped,
                    visible=bool(state.get("visible", True)),
                    enabled=bool(state.get("enabled", True)),
                )
            )

        visible_interactive_count = len(candidates)
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                -self._candidate_score(candidate),
                candidate.bbox_css.y,
                candidate.bbox_css.x,
                candidate.element.model_ref,
            ),
        )
        selected_candidates = ranked[: self.config.max_marks]
        over_mark = max(0, len(ranked) - len(selected_candidates))
        if over_mark:
            omission_counts[WebSomOmissionReason.OVER_MARK_LIMIT] += over_mark
        selected = sorted(
            selected_candidates,
            key=lambda candidate: (candidate.bbox_css.y, candidate.bbox_css.x, candidate.element.model_ref),
        )
        marks = [
            WebSomMark(
                element=candidate.element,
                bbox_css=candidate.bbox_css,
                visible=candidate.visible,
                enabled=candidate.enabled,
            )
            for candidate in selected
        ]
        return WebSomSelection(
            marks=marks,
            visible_interactive_count=visible_interactive_count,
            omitted_reason_counts={reason: count for reason, count in omission_counts.items() if count > 0},
            truncated=over_lookup > 0 or over_mark > 0,
            max_marks=self.config.max_marks,
            max_box_lookup=self.config.max_box_lookup,
        )

    def _candidate_score(self, candidate: WebSomCandidate) -> float:
        box = candidate.bbox_css
        area = min(box.width * box.height, 16_000)
        top_bias = max(0.0, 1200.0 - box.y)
        return self._element_priority(candidate.element) * 10_000 + area + top_bias

    def _element_priority(self, element: WebElementRef) -> int:
        role = element.role.lower().strip()
        tag = element.tag.lower().strip()
        return max(_ROLE_PRIORITY.get(role, 1), _TAG_PRIORITY.get(tag, 1))

    def _attribute_omission_reason(self, element: WebElementRef) -> WebSomOmissionReason | None:
        attrs = {str(key).lower(): value for key, value in element.attributes.items()}
        if self._truthy_attr(attrs.get("hidden")) or self._truthy_attr(attrs.get("aria-hidden")):
            return WebSomOmissionReason.ATTRIBUTE_HIDDEN
        if self._truthy_attr(attrs.get("disabled")) or self._truthy_attr(attrs.get("aria-disabled")):
            return WebSomOmissionReason.ATTRIBUTE_DISABLED
        if self._truthy_attr(attrs.get("inert")):
            return WebSomOmissionReason.ATTRIBUTE_DISABLED
        return None

    def _box_from_payload(self, payload: object) -> SetOfMarkBox | None:
        if not isinstance(payload, Mapping):
            return None
        x = self._optional_float(payload.get("x"))
        y = self._optional_float(payload.get("y"))
        width = self._optional_float(payload.get("width"))
        height = self._optional_float(payload.get("height"))
        if x is None or y is None or width is None or height is None:
            return None
        if width <= 0 or height <= 0:
            return None
        return SetOfMarkBox(x=x, y=y, width=width, height=height)

    def _box_intersects_viewport(self, box: SetOfMarkBox, viewport_width: float, viewport_height: float) -> bool:
        return box.right > 0 and box.bottom > 0 and box.x < viewport_width and box.y < viewport_height

    def _clip_box_to_viewport(
        self,
        box: SetOfMarkBox,
        viewport_width: float,
        viewport_height: float,
    ) -> SetOfMarkBox | None:
        left = max(0.0, box.x)
        top = max(0.0, box.y)
        right = min(viewport_width, box.right)
        bottom = min(viewport_height, box.bottom)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None
        return SetOfMarkBox(x=left, y=top, width=width, height=height)

    def _truthy_attr(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"", "1", "true", "yes", "disabled", "hidden"}

    def _optional_float(self, value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None
