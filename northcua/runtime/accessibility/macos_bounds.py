"""macOS AX bounds decoding and geometry coercion helpers."""

from __future__ import annotations

import logging
import re

from northcua.core.accessibility import AccessibilityBounds

from .macos_types import AXElement, AXValue

logger = logging.getLogger(__name__)


def _bounds_match(left: AccessibilityBounds | None, right: AccessibilityBounds | None) -> bool:
    """Return whether two bounds are close enough to match a window."""

    if left is None or right is None:
        return False
    return (
        abs(left.x - right.x) <= 4
        and abs(left.y - right.y) <= 4
        and abs(left.width - right.width) <= 4
        and abs(left.height - right.height) <= 4
    )


def _ax_bounds(ApplicationServices: object, element: AXElement) -> AccessibilityBounds | None:
    """Read normalized bounds from AX frame or position/size attributes."""

    try:
        error_frame, frame = ApplicationServices.AXUIElementCopyAttributeValue(element, "AXFrame", None)
        error_pos, pos = ApplicationServices.AXUIElementCopyAttributeValue(element, "AXPosition", None)
        error_size, size = ApplicationServices.AXUIElementCopyAttributeValue(element, "AXSize", None)
    except Exception:
        logger.debug("macOS AX bounds query failed for element.", exc_info=True)
        return None
    if error_frame == 0 and frame is not None:
        frame_bounds = _bounds_from_ax_frame_value(ApplicationServices, frame)
        if frame_bounds is not None:
            return frame_bounds
    if error_pos != 0 or error_size != 0 or pos is None or size is None:
        return None
    point = _point_from_ax_value(ApplicationServices, pos)
    size_value = _size_from_ax_value(ApplicationServices, size)
    if point is None or size_value is None:
        logger.debug("macOS AX bounds conversion failed for element: missing point/size payload.")
        return None
    return AccessibilityBounds(
        x=int(point[0]),
        y=int(point[1]),
        width=max(int(size_value[0]), 0),
        height=max(int(size_value[1]), 0),
    )


def _bounds_from_ax_frame_value(ApplicationServices: object, value: AXValue) -> AccessibilityBounds | None:
    """Decode an AXFrame-style value into normalized bounds."""

    decoded = _decode_ax_value(ApplicationServices, value, "kAXValueCGRectType")
    if decoded is not None:
        rect = _coerce_rect_components(decoded)
        if rect is not None:
            return AccessibilityBounds(
                x=int(rect[0]),
                y=int(rect[1]),
                width=max(int(rect[2]), 0),
                height=max(int(rect[3]), 0),
            )
    parsed = _parse_rect_repr(repr(value))
    if parsed is None:
        return None
    return AccessibilityBounds(
        x=int(parsed[0]),
        y=int(parsed[1]),
        width=max(int(parsed[2]), 0),
        height=max(int(parsed[3]), 0),
    )


def _point_from_ax_value(ApplicationServices: object, value: AXValue) -> tuple[float, float] | None:
    """Decode one AXPosition-style value into an ``(x, y)`` tuple."""

    decoded = _decode_ax_value(ApplicationServices, value, "kAXValueCGPointType")
    if decoded is not None:
        point = _coerce_point_components(decoded)
        if point is not None:
            return point
    return _parse_point_repr(repr(value))


def _size_from_ax_value(ApplicationServices: object, value: AXValue) -> tuple[float, float] | None:
    """Decode one AXSize-style value into a ``(width, height)`` tuple."""

    decoded = _decode_ax_value(ApplicationServices, value, "kAXValueCGSizeType")
    if decoded is not None:
        size = _coerce_size_components(decoded)
        if size is not None:
            return size
    return _parse_size_repr(repr(value))


def _decode_ax_value(ApplicationServices: object, value: AXValue, type_name: str) -> object | None:
    """Best-effort decode of one ``AXValue`` payload into a typed object."""

    ax_value_get_value = getattr(ApplicationServices, "AXValueGetValue", None)
    expected_type = getattr(ApplicationServices, type_name, None)
    if not callable(ax_value_get_value) or expected_type is None or value is None:
        return None
    try:
        return ax_value_get_value(value, expected_type, None)
    except Exception:
        logger.debug("macOS AXValue decode failed for %s.", type_name, exc_info=True)
        return None


def _coerce_rect_components(value: object) -> tuple[float, float, float, float] | None:
    """Extract ``(x, y, width, height)`` from one rect-like object."""

    if value is None:
        return None
    if isinstance(value, dict):
        origin = value.get("origin")
        size = value.get("size")
        if isinstance(origin, dict) and isinstance(size, dict):
            x = origin.get("x")
            y = origin.get("y")
            width = size.get("width", size.get("w"))
            height = size.get("height", size.get("h"))
            if None not in (x, y, width, height):
                return float(x), float(y), float(width), float(height)
        x = value.get("x")
        y = value.get("y")
        width = value.get("width", value.get("w"))
        height = value.get("height", value.get("h"))
        if None not in (x, y, width, height):
            return float(x), float(y), float(width), float(height)
    origin = getattr(value, "origin", None)
    size = getattr(value, "size", None)
    if origin is not None and size is not None:
        x = getattr(origin, "x", None)
        y = getattr(origin, "y", None)
        width = getattr(size, "width", getattr(size, "w", None))
        height = getattr(size, "height", getattr(size, "h", None))
        if None not in (x, y, width, height):
            return float(x), float(y), float(width), float(height)
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    width = getattr(value, "width", getattr(value, "w", None))
    height = getattr(value, "height", getattr(value, "h", None))
    if None not in (x, y, width, height):
        return float(x), float(y), float(width), float(height)
    return None


def _coerce_point_components(value: object) -> tuple[float, float] | None:
    """Extract ``(x, y)`` from one point-like object."""

    if value is None:
        return None
    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
        if None not in (x, y):
            return float(x), float(y)
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    if None not in (x, y):
        return float(x), float(y)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    return None


def _coerce_size_components(value: object) -> tuple[float, float] | None:
    """Extract ``(width, height)`` from one size-like object."""

    if value is None:
        return None
    if isinstance(value, dict):
        width = value.get("width", value.get("w"))
        height = value.get("height", value.get("h"))
        if None not in (width, height):
            return float(width), float(height)
    width = getattr(value, "width", getattr(value, "w", None))
    height = getattr(value, "height", getattr(value, "h", None))
    if None not in (width, height):
        return float(width), float(height)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    return None


def _parse_point_repr(value: str) -> tuple[float, float] | None:
    """Parse a point-like repr string into ``(x, y)``."""

    x_value = re.search(r"\bx\b\s*[:=]\s*(-?[\d.]+)", value)
    y_value = re.search(r"\by\b\s*[:=]\s*(-?[\d.]+)", value)
    if x_value is None or y_value is None:
        return None
    return float(x_value.group(1)), float(y_value.group(1))


def _parse_size_repr(value: str) -> tuple[float, float] | None:
    """Parse a size-like repr string into ``(width, height)``."""

    width_value = re.search(r"\b(?:w|width)\b\s*[:=]\s*(-?[\d.]+)", value)
    height_value = re.search(r"\b(?:h|height)\b\s*[:=]\s*(-?[\d.]+)", value)
    if width_value is None or height_value is None:
        return None
    return float(width_value.group(1)), float(height_value.group(1))


def _parse_rect_repr(value: str) -> tuple[float, float, float, float] | None:
    """Parse a rect-like repr string into ``(x, y, width, height)``."""

    point = _parse_point_repr(value)
    size = _parse_size_repr(value)
    if point is None or size is None:
        return None
    return point[0], point[1], size[0], size[1]


__all__ = [
    "_ax_bounds",
    "_bounds_from_ax_frame_value",
    "_bounds_match",
    "_coerce_point_components",
    "_coerce_rect_components",
    "_coerce_size_components",
    "_decode_ax_value",
    "_parse_point_repr",
    "_parse_rect_repr",
    "_parse_size_repr",
    "_point_from_ax_value",
    "_size_from_ax_value",
]
