"""Typed IR for web Set-of-Mark candidate selection."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from northcua.agentkernel.overlay.set_of_mark import SetOfMarkBox

from ...domain.observations import WebElementRef


class WebSomOmissionReason(StrEnum):
    """Reasons a snapshot ref did not become a model-visible SoM mark."""

    ATTRIBUTE_HIDDEN = "attribute_hidden"
    ATTRIBUTE_DISABLED = "attribute_disabled"
    MISSING_BBOX = "missing_bbox"
    STATE_HIDDEN = "state_hidden"
    STATE_DISABLED = "state_disabled"
    OUTSIDE_VIEWPORT = "outside_viewport"
    TINY_BBOX = "tiny_bbox"
    OVER_BOX_LOOKUP_LIMIT = "over_box_lookup_limit"
    OVER_MARK_LIMIT = "over_mark_limit"


class WebSomCandidate(BaseModel):
    """One ref with enough page geometry to be considered for SoM marking."""

    element: WebElementRef
    bbox_css: SetOfMarkBox
    visible: bool = True
    enabled: bool = True


class WebSomMark(BaseModel):
    """One final selected mark before screenshot-pixel projection."""

    element: WebElementRef
    bbox_css: SetOfMarkBox
    visible: bool = True
    enabled: bool = True


class WebSomSelection(BaseModel):
    """Final interaction-first SoM selection and accounting metadata."""

    marks: list[WebSomMark] = Field(default_factory=list)
    visible_interactive_count: int = 0
    omitted_reason_counts: dict[WebSomOmissionReason, int] = Field(default_factory=dict)
    truncated: bool = False
    max_marks: int
    max_box_lookup: int

    @property
    def marked_count(self) -> int:
        """Return the number of selected model-visible marks."""

        return len(self.marks)

    @property
    def omitted_count(self) -> int:
        """Return the total number of omitted candidate refs."""

        return sum(self.omitted_reason_counts.values())
