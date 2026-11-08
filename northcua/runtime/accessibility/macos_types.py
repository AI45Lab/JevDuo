"""Shared macOS Accessibility type aliases and attribute constants."""

from __future__ import annotations

try:
    from typing import TypeAlias
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import TypeAlias

AXElement: TypeAlias = object
AXValue: TypeAlias = object
QuartzWindowInfo: TypeAlias = dict[str, object]
AXChild: TypeAlias = tuple[str, AXElement]

AX_PRIMARY_NAME_ATTRIBUTES = ("AXTitle", "AXDescription")
AX_FALLBACK_VALUE_ATTRIBUTES = ("AXValue",)
AX_STRING_ATTRIBUTES = (
    "AXDescription",
    "AXHelp",
    "AXIdentifier",
    "AXPlaceholderValue",
    "AXRoleDescription",
    "AXSubrole",
    "AXTitle",
    "AXURL",
    "AXValue",
)
AX_BOOLEAN_STATE_ATTRIBUTES = {
    "AXEnabled": "enabled",
    "AXFocused": "focused",
    "AXSelected": "selected",
}
AX_CHILD_ATTRIBUTE_PRIORITY = (
    "AXChildren",
    "AXContents",
    "AXVisibleChildren",
    "AXChildrenInNavigationOrder",
    "AXRows",
    "AXColumns",
    "AXTabs",
    "AXTabChildren",
    "AXCells",
    "AXHeader",
    "AXCloseButton",
    "AXZoomButton",
    "AXMinimizeButton",
    "AXDefaultButton",
    "AXCancelButton",
    "AXToolbarButton",
)

__all__ = [
    "AXChild",
    "AXElement",
    "AXValue",
    "QuartzWindowInfo",
    "AX_BOOLEAN_STATE_ATTRIBUTES",
    "AX_CHILD_ATTRIBUTE_PRIORITY",
    "AX_FALLBACK_VALUE_ATTRIBUTES",
    "AX_PRIMARY_NAME_ATTRIBUTES",
    "AX_STRING_ATTRIBUTES",
]
