"""Model-actionable ref filtering for web observations."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from ..domain.observations import WebElementRef, WebSnapshotObservation


class WebModelRefOmissionReason(StrEnum):
    """Reason an element is omitted from model-actionable refs."""

    ATTRIBUTE_HIDDEN = "attribute_hidden"
    ATTRIBUTE_DISABLED = "attribute_disabled"
    STATE_HIDDEN = "state_hidden"
    STATE_DISABLED = "state_disabled"


class WebModelActionableRefPolicy:
    """Conservative policy for refs that the model may act on.

    This policy intentionally filters only deterministic non-actionable states.
    It does not apply SoM geometry, density, overlap, or role-value heuristics,
    because those can remove valid targets on large real-world pages.
    """

    def actionable_elements(self, snapshot: WebSnapshotObservation) -> list[WebElementRef]:
        """Return snapshot elements that may safely become model-visible refs."""

        return [element for element in snapshot.elements if self.omission_reason(element) is None]

    def omission_reason(self, element: WebElementRef) -> WebModelRefOmissionReason | None:
        """Return why ``element`` must not be exposed as an actionable ref."""

        attrs = self._normalized_attrs(element.attributes.items())
        if self._truthy_attr(attrs.get("hidden")) or self._truthy_attr(attrs.get("aria-hidden")):
            return WebModelRefOmissionReason.ATTRIBUTE_HIDDEN
        if (
            self._truthy_attr(attrs.get("disabled"))
            or self._truthy_attr(attrs.get("aria-disabled"))
            or self._truthy_attr(attrs.get("inert"))
        ):
            return WebModelRefOmissionReason.ATTRIBUTE_DISABLED
        if self._falsey_state(attrs.get("visible")):
            return WebModelRefOmissionReason.STATE_HIDDEN
        if self._falsey_state(attrs.get("enabled")):
            return WebModelRefOmissionReason.STATE_DISABLED
        return None

    def _normalized_attrs(self, attrs: Iterable[tuple[object, object]]) -> dict[str, object]:
        return {str(key).strip().lower(): value for key, value in attrs}

    def _truthy_attr(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"", "1", "true", "yes", "disabled", "hidden", "inert"}

    def _falsey_state(self, value: object) -> bool:
        if isinstance(value, bool):
            return not value
        if value is None:
            return False
        return str(value).strip().lower() in {"0", "false", "no", "hidden", "disabled"}
