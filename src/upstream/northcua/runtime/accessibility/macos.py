"""macOS Accessibility API backend."""

from __future__ import annotations

import logging
import time

from northcua.core.accessibility import (
    AccessibilityCapabilities,
    AccessibilityCaptureRequest,
    AccessibilityCaptureStats,
    AccessibilityNode,
    AccessibilitySnapshot,
)
from northcua.core.enums import (
    AccessibilityCaptureStatus,
    AccessibilityRawFormat,
    AccessibilityScope,
    AccessibilitySourceKind,
)

from .base import AccessibilityBackend, AccessibilityTraversalBudget
from .macos_ax import (
    AX_BOOLEAN_STATE_ATTRIBUTES,
    AX_CHILD_ATTRIBUTE_PRIORITY,
    AX_FALLBACK_VALUE_ATTRIBUTES,
    AX_PRIMARY_NAME_ATTRIBUTES,
    AX_STRING_ATTRIBUTES,
)
from .macos_ax import _ax_actions as ax_actions
from .macos_ax import _ax_boolean_attribute as ax_boolean_attribute
from .macos_ax import _ax_bounds as ax_bounds
from .macos_ax import _ax_element_signature as ax_element_signature
from .macos_ax import _ax_element_values as ax_element_values
from .macos_ax import _ax_object_children as ax_object_children
from .macos_ax import _ax_scalar_attributes as ax_scalar_attributes
from .macos_ax import _ax_states as ax_states
from .macos_ax import _ax_string_attribute as ax_string_attribute
from .macos_ax import _bounds_match as bounds_match
from .macos_ax import _deadline_exceeded as deadline_exceeded
from .macos_ax import _focused_window as focused_window
from .macos_ax import _focused_window_for_application as focused_window_for_application
from .macos_ax import _is_ax_element as is_ax_element
from .macos_ax import _iter_collection as iter_collection
from .macos_ax import _normalize_ax_token as normalize_ax_token
from .macos_ax import _preferred_ax_name as preferred_ax_name
from .macos_ax import _resolve_ax_window as resolve_ax_window
from .macos_ax import _same_ax_element as same_ax_element
from .macos_ax import _visible_windows as visible_windows
from .macos_ax import _window_ref_from_ax_element as window_ref_from_ax_element
from .macos_ax import _window_ref_from_info as window_ref_from_info
from .macos_traversal import _build_ax_node as build_ax_node
from .macos_traversal import _build_window_node as build_window_node

logger = logging.getLogger(__name__)


class MacOSAccessibilityBackend(AccessibilityBackend):
    """Collect desktop/window accessibility trees via AXUIElement APIs."""

    _DESKTOP_EXPANDED_WINDOW_LIMIT = 1
    _AX_PRIMARY_NAME_ATTRIBUTES = AX_PRIMARY_NAME_ATTRIBUTES
    _AX_FALLBACK_VALUE_ATTRIBUTES = AX_FALLBACK_VALUE_ATTRIBUTES
    _AX_STRING_ATTRIBUTES = AX_STRING_ATTRIBUTES
    _AX_BOOLEAN_STATE_ATTRIBUTES = AX_BOOLEAN_STATE_ATTRIBUTES
    _AX_CHILD_ATTRIBUTE_PRIORITY = AX_CHILD_ATTRIBUTE_PRIORITY

    def __init__(self) -> None:
        super().__init__(platform_name="darwin", source_kind=AccessibilitySourceKind.AX)

    def capabilities(self) -> AccessibilityCapabilities:
        """Report macOS Accessibility framework and permission availability.

        Returns:
            Capability metadata for desktop and active-window capture, including
            permission state and setup diagnostics.
        """

        frameworks_available, frameworks_message = self._framework_state()
        if not frameworks_available:
            return self.default_capabilities(
                supported_scopes=[],
                requires_permission=True,
                permission_granted=False,
                raw_formats=[],
                message=frameworks_message,
            )
        permission_granted, permission_message = self._permission_state()
        return self.default_capabilities(
            supported_scopes=[AccessibilityScope.DESKTOP, AccessibilityScope.ACTIVE_WINDOW],
            requires_permission=True,
            permission_granted=permission_granted,
            raw_formats=[],
            message=permission_message,
        )

    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Capture a macOS Accessibility API snapshot.

        Args:
            request: Capture scope, traversal limits, and timeout budget.

        Returns:
            Normalized snapshot, or a failed snapshot when frameworks,
            permissions, or capture traversal are unavailable.
        """

        frameworks_available, frameworks_message = self._framework_state()
        if not frameworks_available:
            return self.unsupported_snapshot(scope=request.scope, message=frameworks_message)

        permission_granted, message = self._permission_state()
        if not permission_granted:
            return self.permission_denied_snapshot(scope=request.scope, message=message)

        try:
            import ApplicationServices
            import Quartz
        except Exception as exc:
            logger.debug("macOS AX framework import failed during capture.", exc_info=True)
            return self.unsupported_snapshot(scope=request.scope, message=f"macOS AX backend unavailable: {exc}")

        budget = AccessibilityTraversalBudget(
            max_depth=request.max_depth,
            max_children_per_node=request.max_children_per_node,
            max_nodes=request.max_nodes,
        )
        deadline_at = time.perf_counter() + max(request.capture_timeout_seconds * 0.8, 0.1)
        try:
            if request.scope == AccessibilityScope.ACTIVE_WINDOW:
                tree, window_ref = self._capture_active_window(ApplicationServices, Quartz, budget, deadline_at)
            else:
                tree, window_ref = self._capture_desktop(ApplicationServices, Quartz, budget, deadline_at)
        except Exception as exc:
            logger.warning("macOS AX capture failed.", exc_info=True)
            return self.error_snapshot(scope=request.scope, message=f"macOS AX capture failed: {exc}")

        return AccessibilitySnapshot(
            status=AccessibilityCaptureStatus.SUCCESS,
            platform_name=self.platform_name,
            source=self.source_kind,
            scope=request.scope,
            window=window_ref,
            tree=tree,
            stats=AccessibilityCaptureStats(truncated=budget.truncated),
            raw_format=AccessibilityRawFormat.NONE,
            raw_payload="",
        )

    def _capture_active_window(
        self,
        ApplicationServices: object,
        Quartz: object,
        budget: AccessibilityTraversalBudget,
        deadline_at: float,
    ) -> tuple[AccessibilityNode | None, object | None]:
        focused_ax_window = self._focused_window(ApplicationServices)
        fallback_window_info = None
        if focused_ax_window is None:
            fallback_window_info = next(iter(self._visible_windows(Quartz)), None)
            if fallback_window_info is not None:
                focused_ax_window = self._resolve_ax_window(
                    ApplicationServices,
                    int(fallback_window_info.get("kCGWindowOwnerPID") or 0),
                    str(fallback_window_info.get("kCGWindowName") or ""),
                )
        tree = (
            self._build_ax_node(
                focused_ax_window,
                ApplicationServices=ApplicationServices,
                budget=budget,
                path="active-window",
                depth=0,
                seen=set(),
                deadline_at=deadline_at,
            )
            if focused_ax_window is not None
            else None
        )
        window_ref = self._window_ref_from_ax_element(
            ApplicationServices, focused_ax_window
        ) or self._window_ref_from_info(fallback_window_info)
        return tree, window_ref

    def _capture_desktop(
        self,
        ApplicationServices: object,
        Quartz: object,
        budget: AccessibilityTraversalBudget,
        deadline_at: float,
    ) -> tuple[AccessibilityNode, None]:
        windows = self._visible_windows(Quartz)
        children: list[AccessibilityNode] = []
        for index in budget.limit_children(len(windows)):
            child = self._build_window_node(
                windows[index],
                ApplicationServices=ApplicationServices,
                budget=budget,
                path=f"desktop/{index}",
                depth=1,
                expand_children=index < self._DESKTOP_EXPANDED_WINDOW_LIMIT,
                deadline_at=deadline_at,
            )
            if child is not None:
                children.append(child)
        tree = AccessibilityNode(node_id="desktop-root", role="desktop", name="desktop", children=children)
        return tree, None

    def _framework_state(self) -> tuple[bool, str]:
        """Probe macOS framework imports without failing capability discovery.

        Returns:
            Tuple of availability flag and diagnostic message.
        """

        try:
            import ApplicationServices  # noqa: F401
            import Quartz  # noqa: F401
        except Exception as exc:
            logger.debug("macOS accessibility framework probe failed.", exc_info=True)
            return False, f"macOS accessibility frameworks unavailable: {exc}"
        return True, ""

    def _permission_state(self) -> tuple[bool, str]:
        """Return whether the current process has AX trust.

        Returns:
            Tuple of permission flag and setup diagnostic message.
        """

        import ApplicationServices

        trusted = bool(ApplicationServices.AXIsProcessTrusted())
        if trusted:
            return True, ""
        return False, "Accessibility permission not granted"


MacOSAccessibilityBackend._build_window_node = staticmethod(build_window_node)
MacOSAccessibilityBackend._build_ax_node = staticmethod(build_ax_node)
MacOSAccessibilityBackend._normalize_ax_token = staticmethod(normalize_ax_token)
MacOSAccessibilityBackend._visible_windows = staticmethod(visible_windows)
MacOSAccessibilityBackend._resolve_ax_window = staticmethod(resolve_ax_window)
MacOSAccessibilityBackend._focused_window_for_application = staticmethod(focused_window_for_application)
MacOSAccessibilityBackend._focused_window = staticmethod(focused_window)
MacOSAccessibilityBackend._ax_string_attribute = staticmethod(ax_string_attribute)
MacOSAccessibilityBackend._ax_boolean_attribute = staticmethod(ax_boolean_attribute)
MacOSAccessibilityBackend._preferred_ax_name = staticmethod(preferred_ax_name)
MacOSAccessibilityBackend._ax_states = staticmethod(ax_states)
MacOSAccessibilityBackend._ax_scalar_attributes = staticmethod(ax_scalar_attributes)
MacOSAccessibilityBackend._ax_object_children = staticmethod(ax_object_children)
MacOSAccessibilityBackend._ax_element_values = staticmethod(ax_element_values)
MacOSAccessibilityBackend._is_ax_element = staticmethod(is_ax_element)
MacOSAccessibilityBackend._ax_element_signature = staticmethod(ax_element_signature)
MacOSAccessibilityBackend._deadline_exceeded = staticmethod(deadline_exceeded)
MacOSAccessibilityBackend._same_ax_element = staticmethod(same_ax_element)
MacOSAccessibilityBackend._bounds_match = staticmethod(bounds_match)
MacOSAccessibilityBackend._iter_collection = staticmethod(iter_collection)
MacOSAccessibilityBackend._ax_actions = staticmethod(ax_actions)
MacOSAccessibilityBackend._window_ref_from_info = staticmethod(window_ref_from_info)
MacOSAccessibilityBackend._window_ref_from_ax_element = staticmethod(window_ref_from_ax_element)
MacOSAccessibilityBackend._ax_bounds = staticmethod(ax_bounds)
