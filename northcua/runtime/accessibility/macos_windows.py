"""macOS Quartz/AX window reference helpers."""

from __future__ import annotations

from northcua.core.accessibility import AccessibilityBounds, AccessibilityWindowRef

from .macos_attributes import _ax_string_attribute
from .macos_bounds import _ax_bounds, _bounds_match
from .macos_collections import _iter_collection
from .macos_types import AXElement, QuartzWindowInfo


def _visible_windows(Quartz: object) -> list[QuartzWindowInfo]:
    """Return visible non-desktop Quartz windows."""

    window_info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    return [dict(item) for item in window_info or [] if int(item.get("kCGWindowLayer", 0) or 0) == 0]


def _resolve_ax_window(
    ApplicationServices: object,
    pid: int,
    title: str,
    bounds: AccessibilityBounds | None = None,
) -> AXElement | None:
    """Resolve a Quartz window to an AX window element."""

    if pid <= 0:
        return None
    app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
    error, windows = ApplicationServices.AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
    if error != 0 or not windows:
        return None
    window_elements = _iter_collection(windows)
    if title:
        for element in window_elements:
            element_title = _ax_string_attribute(ApplicationServices, element, "AXTitle")
            if element_title == title:
                return element
    if bounds is not None:
        for element in window_elements:
            element_bounds = _ax_bounds(ApplicationServices, element)
            if _bounds_match(bounds, element_bounds):
                return element
    return window_elements[0] if window_elements else None


def _focused_window_for_application(ApplicationServices: object, pid: int) -> AXElement | None:
    """Return the focused AX window for an application process."""

    if pid <= 0:
        return None
    app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
    error, value = ApplicationServices.AXUIElementCopyAttributeValue(app_ref, "AXFocusedWindow", None)
    if error != 0 or value is None:
        return None
    return value


def _focused_window(ApplicationServices: object) -> AXElement | None:
    """Return the currently focused system-wide AX window."""

    system_wide = ApplicationServices.AXUIElementCreateSystemWide()
    error, current_window = ApplicationServices.AXUIElementCopyAttributeValue(
        system_wide,
        "AXFocusedWindow",
        None,
    )
    if error != 0:
        return None
    return current_window


def _window_ref_from_info(window_info: QuartzWindowInfo | None) -> AccessibilityWindowRef | None:
    """Build a window reference from Quartz window metadata."""

    if window_info is None:
        return None
    bounds_dict = dict(window_info.get("kCGWindowBounds") or {})
    return AccessibilityWindowRef(
        app_name=str(window_info.get("kCGWindowOwnerName") or ""),
        title=str(window_info.get("kCGWindowName") or ""),
        pid=int(window_info.get("kCGWindowOwnerPID") or 0),
        bounds=AccessibilityBounds(
            x=int(bounds_dict.get("X") or 0),
            y=int(bounds_dict.get("Y") or 0),
            width=int(bounds_dict.get("Width") or 0),
            height=int(bounds_dict.get("Height") or 0),
        ),
    )


def _window_ref_from_ax_element(
    ApplicationServices: object,
    element: AXElement | None,
) -> AccessibilityWindowRef | None:
    """Build a window reference from an AX window element."""

    if element is None:
        return None
    return AccessibilityWindowRef(
        app_name="",
        title=_ax_string_attribute(ApplicationServices, element, "AXTitle"),
        pid=0,
        bounds=_ax_bounds(ApplicationServices, element),
    )


__all__ = [
    "_focused_window",
    "_focused_window_for_application",
    "_resolve_ax_window",
    "_visible_windows",
    "_window_ref_from_ax_element",
    "_window_ref_from_info",
]
