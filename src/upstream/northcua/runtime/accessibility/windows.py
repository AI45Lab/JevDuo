"""Windows UI Automation accessibility backend."""

from __future__ import annotations

import logging

try:
    from typing import TypeAlias
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import TypeAlias

from northcua.core.accessibility import (
    AccessibilityBounds,
    AccessibilityCapabilities,
    AccessibilityCaptureRequest,
    AccessibilityCaptureStats,
    AccessibilityNode,
    AccessibilitySnapshot,
    AccessibilityWindowRef,
)
from northcua.core.enums import (
    AccessibilityCaptureStatus,
    AccessibilityRawFormat,
    AccessibilityScope,
    AccessibilitySourceKind,
)

from .base import AccessibilityBackend, AccessibilityTraversalBudget

logger = logging.getLogger(__name__)

UIAWrapper: TypeAlias = object
UIAElementInfo: TypeAlias = object
UIARect: TypeAlias = object


class WindowsAccessibilityBackend(AccessibilityBackend):
    """Collect desktop/window accessibility trees via Windows UIA."""

    def __init__(self) -> None:
        super().__init__(platform_name="windows", source_kind=AccessibilitySourceKind.UIA)

    def capabilities(self) -> AccessibilityCapabilities:
        """Report whether Windows UI Automation capture is available.

        Returns:
            Capability metadata for desktop and active-window capture, or an
            unsupported response when pywinauto cannot be imported.
        """

        try:
            from pywinauto import Desktop  # noqa: F401
        except Exception as exc:
            logger.debug("Windows UIA capability probe failed.", exc_info=True)
            return self.default_capabilities(
                supported_scopes=[],
                raw_formats=[],
                message=f"Windows UIA backend unavailable: {exc}",
            )
        return self.default_capabilities(
            supported_scopes=[AccessibilityScope.DESKTOP, AccessibilityScope.ACTIVE_WINDOW],
        )

    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Capture a Windows UI Automation accessibility snapshot.

        Args:
            request: Capture scope and traversal limits.

        Returns:
            Normalized snapshot, or a failed snapshot when UIA is unavailable
            or capture fails.
        """

        try:
            from pywinauto import Desktop
        except Exception as exc:
            logger.debug("Windows UIA import failed during capture.", exc_info=True)
            return self.unsupported_snapshot(
                scope=request.scope,
                message=f"Windows UIA backend unavailable: {exc}",
            )

        desktop = Desktop(backend="uia")
        budget = AccessibilityTraversalBudget(
            max_depth=request.max_depth,
            max_children_per_node=request.max_children_per_node,
            max_nodes=request.max_nodes,
        )
        try:
            if request.scope == AccessibilityScope.ACTIVE_WINDOW:
                root_wrapper = desktop.top_window()
                tree = self._build_wrapper_node(
                    root_wrapper,
                    budget=budget,
                    path="active-window",
                    depth=0,
                    seen=set(),
                )
                window_ref = self._window_ref_from_wrapper(root_wrapper)
            else:
                window_wrappers = list(desktop.windows())
                children: list[AccessibilityNode] = []
                for index in budget.limit_children(len(window_wrappers)):
                    child = self._build_wrapper_node(
                        window_wrappers[index],
                        budget=budget,
                        path=f"desktop/{index}",
                        depth=1,
                        seen=set(),
                    )
                    if child is not None:
                        children.append(child)
                tree = AccessibilityNode(
                    node_id="desktop-root",
                    role="desktop",
                    name="desktop",
                    children=children,
                )
                window_ref = None
        except Exception as exc:
            logger.warning("Windows UIA capture failed.", exc_info=True)
            return self.error_snapshot(scope=request.scope, message=f"Windows UIA capture failed: {exc}")

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

    def _window_ref_from_wrapper(self, wrapper: UIAWrapper) -> AccessibilityWindowRef:
        """Build a compact window reference from a UIA wrapper.

        Args:
            wrapper: pywinauto wrapper for a top-level window.

        Returns:
            Normalized window reference.
        """

        name = self._safe_call(wrapper, "window_text", default="")
        element_info = self._safe_attr(wrapper, "element_info")
        process_id = int(
            getattr(element_info, "process_id", 0) or self._safe_attr(wrapper, "process_id", default=0) or 0
        )
        rect = self._safe_call(wrapper, "rectangle")
        bounds = self._bounds_from_rect(rect)
        return AccessibilityWindowRef(
            app_name=str(name),
            title=str(name),
            pid=process_id,
            bounds=bounds,
        )

    def _build_wrapper_node(
        self,
        wrapper: UIAWrapper,
        *,
        budget: AccessibilityTraversalBudget,
        path: str,
        depth: int,
        seen: set[str],
    ) -> AccessibilityNode | None:
        """Build a normalized node from a pywinauto wrapper.

        Args:
            wrapper: Source UIA wrapper.
            budget: Traversal budget enforcing depth and size limits.
            path: Stable traversal path for the node.
            depth: Current traversal depth.
            seen: Wrapper signatures already visited on this branch.

        Returns:
            Normalized accessibility node, or ``None`` when traversal skips it.
        """

        if not budget.claim_node(depth):
            return None

        element_info = self._safe_attr(wrapper, "element_info")
        signature = self._wrapper_signature(wrapper, element_info)
        if signature in seen:
            return None
        seen.add(signature)
        name = str(self._safe_call(wrapper, "window_text", default="") or "")
        role = str(getattr(element_info, "control_type", "") or "node").lower()
        bounds = self._bounds_from_rect(self._safe_call(wrapper, "rectangle"))
        attributes: dict[str, str] = {}
        automation_id = str(getattr(element_info, "automation_id", "") or "")
        if automation_id:
            attributes["automation_id"] = automation_id
        class_name = str(getattr(element_info, "class_name", "") or "")
        if class_name:
            attributes["class_name"] = class_name

        states: list[str] = []
        if bool(self._safe_call(wrapper, "is_enabled", default=False)):
            states.append("enabled")
        if bool(self._safe_call(wrapper, "is_visible", default=False)):
            states.append("visible")
        if bool(self._safe_call(wrapper, "has_keyboard_focus", default=False)):
            states.append("focused")

        children: list[AccessibilityNode] = []
        raw_children = list(self._safe_call(wrapper, "children", default=[]) or [])
        for index in budget.limit_children(len(raw_children)):
            child = self._build_wrapper_node(
                raw_children[index],
                budget=budget,
                path=f"{path}/{index}",
                depth=depth + 1,
                seen=seen,
            )
            if child is not None:
                children.append(child)

        return AccessibilityNode(
            node_id=path,
            role=role,
            name=name,
            bounds=bounds,
            states=states,
            attributes=attributes,
            children=children,
        )

    @staticmethod
    def _safe_call(target: object, name: str, *, default: object = None) -> object:
        """Read or call a wrapper member without failing capture.

        Args:
            target: Object exposing the member.
            name: Attribute or zero-argument method name.
            default: Value returned when lookup or invocation fails.

        Returns:
            Member value, call result, or ``default``.
        """

        try:
            value = getattr(target, name)
        except Exception:
            logger.debug("Windows UIA attribute lookup failed for %s.", name, exc_info=True)
            return default
        try:
            return value() if callable(value) else value
        except Exception:
            logger.debug("Windows UIA callable lookup failed for %s.", name, exc_info=True)
            return default

    @staticmethod
    def _safe_attr(target: object, name: str, *, default: object = None) -> object:
        """Read a wrapper attribute without failing capture.

        Args:
            target: Object exposing the attribute.
            name: Attribute name.
            default: Value returned when lookup fails.

        Returns:
            Attribute value, or ``default``.
        """

        try:
            return getattr(target, name)
        except Exception:
            logger.debug("Windows UIA safe attribute read failed for %s.", name, exc_info=True)
            return default

    @staticmethod
    def _bounds_from_rect(rect: UIARect | None) -> AccessibilityBounds | None:
        """Convert a UIA rectangle to normalized bounds.

        Args:
            rect: pywinauto rectangle-like value.

        Returns:
            Normalized bounds, or ``None`` when conversion fails.
        """

        if rect is None:
            return None
        try:
            return AccessibilityBounds.from_rect(rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            logger.debug("Windows UIA rectangle conversion failed.", exc_info=True)
            return None

    @staticmethod
    def _wrapper_signature(wrapper: UIAWrapper, element_info: UIAElementInfo | None) -> str:
        """Build a cycle-detection signature for a UIA wrapper.

        Args:
            wrapper: Source UIA wrapper.
            element_info: Optional wrapper element metadata.

        Returns:
            Best-effort stable wrapper signature.
        """

        runtime_id = getattr(element_info, "runtime_id", None)
        if runtime_id:
            return f"runtime:{runtime_id}"
        handle = getattr(element_info, "handle", None) or getattr(wrapper, "handle", None)
        process_id = getattr(element_info, "process_id", None) or getattr(wrapper, "process_id", None)
        if handle is not None:
            return f"handle:{process_id}:{handle}"
        return repr(element_info) if element_info is not None else repr(wrapper)
