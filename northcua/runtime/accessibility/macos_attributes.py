"""macOS AX scalar, state, action, and child attribute helpers."""

from __future__ import annotations

import logging
import re

from .macos_collections import _ax_element_signature, _ax_element_values
from .macos_types import (
    AX_BOOLEAN_STATE_ATTRIBUTES,
    AX_CHILD_ATTRIBUTE_PRIORITY,
    AX_FALLBACK_VALUE_ATTRIBUTES,
    AX_PRIMARY_NAME_ATTRIBUTES,
    AX_STRING_ATTRIBUTES,
    AXChild,
    AXElement,
)

logger = logging.getLogger(__name__)


def _normalize_ax_token(value: str) -> str:
    """Normalize AX role/action tokens to snake-case names."""

    normalized = value.strip()
    if normalized.startswith("AX") and len(normalized) > 2:
        normalized = normalized[2:]
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return normalized.replace("-", "_").replace(" ", "_").lower()


def _ax_string_attribute(ApplicationServices: object, element: AXElement, attribute: str) -> str:
    """Read one AX attribute as text."""

    error, value = ApplicationServices.AXUIElementCopyAttributeValue(element, attribute, None)
    if error != 0 or value is None:
        return ""
    return str(value)


def _ax_boolean_attribute(
    ApplicationServices: object,
    element: AXElement,
    attribute: str,
    default: bool = False,
) -> bool:
    """Read one AX attribute as a boolean."""

    error, value = ApplicationServices.AXUIElementCopyAttributeValue(element, attribute, None)
    if error != 0 or value is None:
        return default
    return bool(value)


def _preferred_ax_name(ApplicationServices: object, element: AXElement) -> str:
    """Return the best available human-readable AX name."""

    for attribute in AX_PRIMARY_NAME_ATTRIBUTES:
        value = _ax_string_attribute(ApplicationServices, element, attribute)
        if value:
            return value
    for attribute in AX_FALLBACK_VALUE_ATTRIBUTES:
        value = _ax_string_attribute(ApplicationServices, element, attribute)
        if value:
            return value
    return ""


def _ax_states(ApplicationServices: object, element: AXElement) -> list[str]:
    """Read normalized state names from an AX element."""

    states: list[str] = []
    for attribute, state_name in AX_BOOLEAN_STATE_ATTRIBUTES.items():
        default = attribute == "AXEnabled"
        if _ax_boolean_attribute(ApplicationServices, element, attribute, default=default):
            states.append(state_name)
    return states


def _ax_scalar_attributes(ApplicationServices: object, element: AXElement) -> dict[str, str]:
    """Read scalar AX attributes for the normalized node payload."""

    attributes: dict[str, str] = {}
    for attribute in AX_STRING_ATTRIBUTES:
        value = _ax_string_attribute(ApplicationServices, element, attribute)
        if not value:
            continue
        if attribute in {"AXTitle", "AXDescription", "AXValue"}:
            continue
        attributes[attribute.removeprefix("AX").lower()] = value
    return attributes


def _ax_object_children(ApplicationServices: object, element: AXElement) -> list[AXChild]:
    """Return unique AX child elements with their relation attribute."""

    yielded_signatures: set[str] = set()
    children: list[AXChild] = []
    for attribute in AX_CHILD_ATTRIBUTE_PRIORITY:
        error, value = ApplicationServices.AXUIElementCopyAttributeValue(element, attribute, None)
        if error != 0 or value is None:
            continue
        for child in _ax_element_values(ApplicationServices, value):
            signature = _ax_element_signature(child)
            if signature in yielded_signatures:
                continue
            yielded_signatures.add(signature)
            children.append((attribute, child))
    return children


def _ax_actions(ApplicationServices: object, element: AXElement) -> list[str]:
    """Read action names supported by an AX element."""

    try:
        error, names = ApplicationServices.AXUIElementCopyActionNames(element, None)
    except Exception:
        logger.debug("macOS AX action-name query failed for element.", exc_info=True)
        return []
    if error != 0 or names is None:
        return []
    return [str(name) for name in names]


__all__ = [
    "_ax_actions",
    "_ax_boolean_attribute",
    "_ax_object_children",
    "_ax_scalar_attributes",
    "_ax_states",
    "_ax_string_attribute",
    "_normalize_ax_token",
    "_preferred_ax_name",
]
