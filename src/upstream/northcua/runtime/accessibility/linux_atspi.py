"""Linux AT-SPI scalar extraction helpers."""

from __future__ import annotations

import logging

try:
    from typing import TypeAlias
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import TypeAlias

from northcua.core.accessibility import AccessibilityBounds, AccessibilityWindowRef

logger = logging.getLogger(__name__)

ATSPIElement: TypeAlias = object

MAX_ATTRIBUTE_ITEMS = 64
MAX_ATTRIBUTE_VALUE_CHARS = 512
MAX_TEXT_CHARS = 2048


def _states(pyatspi: object, element: ATSPIElement) -> list[str]:
    """Read normalized state names from an AT-SPI element."""

    normalized_states: list[str] = []
    try:
        state_set = element.getState()
    except Exception:
        logger.debug("AT-SPI state query failed for element.", exc_info=True)
        return normalized_states
    for state_name in ("STATE_FOCUSED", "STATE_ACTIVE", "STATE_ENABLED", "STATE_VISIBLE", "STATE_SHOWING"):
        state_value = getattr(pyatspi, state_name, None)
        if state_value is not None and state_set.contains(state_value):
            normalized_states.append(state_name.removeprefix("STATE_").lower())
    return normalized_states


def _actions(element: ATSPIElement) -> list[str]:
    """Read normalized action names from an AT-SPI element."""

    try:
        action = element.queryAction()
    except Exception:
        logger.debug("AT-SPI action query failed for element.", exc_info=True)
        return []
    return [str(action.getName(index)).lower() for index in range(action.nActions)]


def _bounds(pyatspi: object, element: ATSPIElement) -> AccessibilityBounds | None:
    """Read desktop-coordinate bounds from an AT-SPI element."""

    states = _states(pyatspi, element)
    if "showing" not in states:
        return None
    coordinate_type = _screen_coordinate_type(pyatspi)
    if coordinate_type is None:
        return None
    try:
        component = element.queryComponent()
        extents = component.getExtents(coordinate_type)
    except Exception:
        logger.debug("AT-SPI bounds query failed for element.", exc_info=True)
        return None
    width = int(getattr(extents, "width", 0))
    height = int(getattr(extents, "height", 0))
    if width <= 0 or height <= 0:
        return None
    return AccessibilityBounds(
        x=int(getattr(extents, "x", 0)),
        y=int(getattr(extents, "y", 0)),
        width=width,
        height=height,
    )


def _screen_coordinate_type(pyatspi: object) -> object | None:
    """Return the preferred AT-SPI screen-coordinate constant."""

    return getattr(pyatspi, "XY_SCREEN", getattr(pyatspi, "DESKTOP_COORDS", None))


def _window_ref_from_element(pyatspi: object, element: ATSPIElement | None) -> AccessibilityWindowRef | None:
    """Build a compact window reference from an AT-SPI element."""

    if element is None:
        return None
    return AccessibilityWindowRef(
        app_name=str(getattr(getattr(element, "parent", None), "name", "") or ""),
        title=str(getattr(element, "name", "") or ""),
        pid=0,
        bounds=_bounds(pyatspi, element),
    )


def _attributes(element: ATSPIElement) -> dict[str, str]:
    """Read bounded AT-SPI attribute key/value pairs."""

    try:
        raw_attributes = dict(element.get_attributes())
    except Exception:
        logger.debug("AT-SPI attribute query failed for element.", exc_info=True)
        return {}
    normalized_attributes: dict[str, str] = {}
    for index, (key, value) in enumerate(raw_attributes.items()):
        if index >= MAX_ATTRIBUTE_ITEMS:
            break
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        text_value = str(value)
        normalized_attributes[normalized_key] = text_value[:MAX_ATTRIBUTE_VALUE_CHARS]
    return normalized_attributes


def _text(element: ATSPIElement) -> str:
    """Read bounded text content from an AT-SPI element."""

    try:
        text_obj = element.queryText()
        text_value = str(text_obj.getText(0, text_obj.characterCount))
    except Exception:
        logger.debug("AT-SPI text query failed for element.", exc_info=True)
        return ""
    return text_value.replace("\ufffc", "").replace("\ufffd", "")[:MAX_TEXT_CHARS]


def _value(element: ATSPIElement) -> str:
    """Read the current value from an AT-SPI value provider."""

    try:
        value_obj = element.queryValue()
    except Exception:
        logger.debug("AT-SPI value query failed for element.", exc_info=True)
        return ""
    try:
        return str(value_obj.currentValue)
    except Exception:
        logger.debug("AT-SPI current value read failed for element.", exc_info=True)
        return ""


def _element_signature(element: ATSPIElement) -> str:
    """Build a cycle-detection signature for an AT-SPI element."""

    return repr(element)
