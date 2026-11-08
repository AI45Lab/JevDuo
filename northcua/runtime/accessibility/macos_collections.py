"""Collection and identity helpers for macOS AX values."""

from __future__ import annotations

import time
from collections.abc import Iterable

from .macos_types import AXElement, AXValue


def _ax_element_signature(element: AXElement) -> str:
    """Build a cycle-detection signature for an AX element."""

    return f"{type(element).__name__}:{repr(element)}"


def _deadline_exceeded(deadline_at: float | None) -> bool:
    """Return whether a monotonic traversal deadline has passed."""

    return deadline_at is not None and time.perf_counter() >= deadline_at


def _same_ax_element(left: AXElement | None, right: AXElement | None) -> bool:
    """Compare two AX elements by their capture signatures."""

    if left is None or right is None:
        return False
    return _ax_element_signature(left) == _ax_element_signature(right)


def _iter_collection(value: AXValue) -> list[AXValue]:
    """Convert AX collection-like values to a list."""

    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _is_ax_element(ApplicationServices: object, value: AXValue) -> bool:
    """Return whether a value looks like an AX element."""

    ax_ref_type = getattr(ApplicationServices, "AXUIElementRef", None)
    if ax_ref_type is not None:
        try:
            return isinstance(value, ax_ref_type)
        except TypeError:
            pass
    return value is not None and "AXUIElement" in type(value).__name__


def _ax_element_values(ApplicationServices: object, value: AXValue) -> list[AXElement]:
    """Extract AX elements from a scalar or collection value."""

    if _is_ax_element(ApplicationServices, value):
        return [value]
    if isinstance(value, (str, bytes, dict)):
        return []
    if isinstance(value, Iterable):
        return [item for item in _iter_collection(value) if _is_ax_element(ApplicationServices, item)]
    return []


__all__ = [
    "_ax_element_signature",
    "_ax_element_values",
    "_deadline_exceeded",
    "_is_ax_element",
    "_iter_collection",
    "_same_ax_element",
]
