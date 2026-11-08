"""Linux AT-SPI accessibility backend."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

try:
    from typing import TypeAlias
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import TypeAlias

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
from .linux_atspi import (
    MAX_ATTRIBUTE_ITEMS,
    MAX_ATTRIBUTE_VALUE_CHARS,
    MAX_TEXT_CHARS,
)
from .linux_atspi import _actions as actions
from .linux_atspi import _attributes as attributes
from .linux_atspi import _bounds as bounds
from .linux_atspi import _element_signature as element_signature
from .linux_atspi import _states as states
from .linux_atspi import _text as text
from .linux_atspi import _value as value
from .linux_atspi import _window_ref_from_element as window_ref_from_element

logger = logging.getLogger(__name__)

ATSPIObject: TypeAlias = object
ATSPIElement: TypeAlias = object

_SYSTEM_PYATSPI_CAPTURE_SCRIPT = r"""
import json
import sys
import time

import pyatspi


def states(element):
    out = []
    try:
        state_set = element.getState()
    except Exception:
        return out
    for name in ("STATE_FOCUSED", "STATE_ACTIVE", "STATE_ENABLED", "STATE_VISIBLE", "STATE_SHOWING"):
        value = getattr(pyatspi, name, None)
        if value is not None and state_set.contains(value):
            out.append(name.removeprefix("STATE_").lower())
    return out


def bounds(element):
    if "showing" not in states(element):
        return None
    coordinate_type = getattr(pyatspi, "XY_SCREEN", getattr(pyatspi, "DESKTOP_COORDS", None))
    if coordinate_type is None:
        return None
    try:
        extents = element.queryComponent().getExtents(coordinate_type)
    except Exception:
        return None
    width = int(getattr(extents, "width", 0) or 0)
    height = int(getattr(extents, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    return {"x": int(getattr(extents, "x", 0) or 0), "y": int(getattr(extents, "y", 0) or 0), "width": width, "height": height}


def actions(element):
    try:
        action = element.queryAction()
    except Exception:
        return []
    return [str(action.getName(index)).lower() for index in range(action.nActions)]


def text(element, limit):
    try:
        text_obj = element.queryText()
        value = str(text_obj.getText(0, text_obj.characterCount))
    except Exception:
        return ""
    return value.replace("\ufffc", "").replace("\ufffd", "")[:limit]


def current_value(element):
    try:
        return str(element.queryValue().currentValue)
    except Exception:
        return ""


def attributes(element):
    try:
        raw = dict(element.get_attributes())
    except Exception:
        return {}
    out = {}
    for index, item in enumerate(raw.items()):
        if index >= 64:
            break
        key, value = item
        key = str(key).strip()
        if key:
            out[key] = str(value)[:512]
    return out


class Budget:
    def __init__(self, payload):
        self.max_depth = int(payload.get("max_depth", 50))
        self.max_children_per_node = int(payload.get("max_children_per_node", 256))
        self.max_nodes = int(payload.get("max_nodes", 5000))
        self.node_count = 0
        self.max_seen_depth = 0
        self.truncated = False

    def claim(self, depth):
        if depth > self.max_depth or self.node_count >= self.max_nodes:
            self.truncated = True
            return False
        self.node_count += 1
        self.max_seen_depth = max(self.max_seen_depth, depth)
        return True

    def child_indexes(self, child_count):
        limit = min(child_count, self.max_children_per_node)
        if child_count > limit:
            self.truncated = True
        return range(limit)


def build(element, budget, path, depth, seen):
    if element is None or not budget.claim(depth):
        return None
    signature = repr(element)
    if signature in seen:
        return None
    seen.add(signature)
    child_nodes = []
    child_count = int(getattr(element, "childCount", 0) or 0)
    for index in budget.child_indexes(child_count):
        try:
            child = build(element.getChildAtIndex(index), budget, f"{path}/{index}", depth + 1, seen)
        except Exception:
            child = None
        if child is not None:
            child_nodes.append(child)
    try:
        role = str(element.getRoleName() or "node").lower()
    except Exception:
        role = "node"
    return {
        "node_id": path,
        "role": role,
        "name": str(getattr(element, "name", "") or ""),
        "description": str(getattr(element, "description", "") or ""),
        "value": current_value(element),
        "text": text(element, 2048),
        "bounds": bounds(element),
        "states": states(element),
        "actions": actions(element),
        "attributes": attributes(element),
        "children": child_nodes,
    }


def active_window(desktop):
    active = getattr(pyatspi, "STATE_ACTIVE", None)
    focused = getattr(pyatspi, "STATE_FOCUSED", None)
    for app_index in range(desktop.childCount):
        app = desktop.getChildAtIndex(app_index)
        if app is None:
            continue
        for child_index in range(app.childCount):
            child = app.getChildAtIndex(child_index)
            if child is None:
                continue
            try:
                state_set = child.getState()
            except Exception:
                continue
            if active is not None and state_set.contains(active):
                return child
            if focused is not None and state_set.contains(focused):
                return child
    return None


payload = json.loads(sys.stdin.read() or "{}")
start = time.monotonic()
budget = Budget(payload)
desktop = pyatspi.Registry.getDesktop(0)
window = None
if payload.get("scope") == "active_window":
    target = active_window(desktop)
    tree = build(target, budget, "active-window", 0, set()) if target is not None else None
    if target is not None:
        window = {
            "app_name": str(getattr(getattr(target, "parent", None), "name", "") or ""),
            "title": str(getattr(target, "name", "") or ""),
            "pid": 0,
            "bounds": bounds(target),
        }
else:
    children = []
    for index in budget.child_indexes(int(getattr(desktop, "childCount", 0) or 0)):
        child = build(desktop.getChildAtIndex(index), budget, f"desktop/{index}", 1, set())
        if child is not None:
            children.append(child)
    tree = {"node_id": "desktop-root", "role": "desktop", "name": "desktop", "children": children}

snapshot = {
    "status": "success",
    "platform_name": "linux",
    "source": "atspi",
    "scope": payload.get("scope", "desktop"),
    "window": window,
    "tree": tree,
    "stats": {
        "node_count": budget.node_count,
        "max_depth": budget.max_seen_depth,
        "truncated": budget.truncated,
        "capture_ms": (time.monotonic() - start) * 1000,
    },
    "raw_format": "none",
    "raw_payload": "",
}
print(json.dumps(snapshot))
"""


def _import_pyatspi() -> object:
    """Import pyatspi, including Debian system packages from uv Python runtimes."""

    _prefer_system_dist_packages()
    try:
        import pyatspi

        return pyatspi
    except (ImportError, ModuleNotFoundError) as first_exc:
        _clear_partial_atspi_modules()
        _prefer_system_dist_packages()
        try:
            import pyatspi

            return pyatspi
        except (ImportError, ModuleNotFoundError):
            raise first_exc


def _system_python_candidates() -> list[str]:
    """Return system Python interpreters that may have distro GI bindings."""

    candidates = [os.environ.get("NORTHCUA_ATSPI_SYSTEM_PYTHON", ""), "/usr/bin/python3", "/usr/local/bin/python3"]
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen or candidate == sys.executable:
            continue
        seen.add(candidate)
        if Path(candidate).exists():
            result.append(candidate)
    return result


def _system_python_pyatspi_available(timeout_seconds: float = 2.0) -> bool:
    """Check whether a system Python can import pyatspi."""

    for python in _system_python_candidates():
        try:
            completed = subprocess.run(
                [python, "-c", "import pyatspi"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
            )
        except Exception:
            logger.debug("System Python AT-SPI availability probe failed.", exc_info=True)
            continue
        if completed.returncode == 0:
            return True
    return False


def _capture_with_system_python(request: AccessibilityCaptureRequest) -> AccessibilitySnapshot | None:
    """Capture AT-SPI through distro Python when the active runtime cannot import GI."""

    payload = {
        "scope": request.scope.value,
        "max_depth": request.max_depth,
        "max_children_per_node": request.max_children_per_node,
        "max_nodes": request.max_nodes,
    }
    timeout = max(float(request.capture_timeout_seconds), 1.0)
    for python in _system_python_candidates():
        try:
            completed = subprocess.run(
                [python, "-c", _SYSTEM_PYATSPI_CAPTURE_SCRIPT],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.debug("System Python AT-SPI capture timed out.", exc_info=True)
            continue
        except Exception:
            logger.debug("System Python AT-SPI capture failed to start.", exc_info=True)
            continue
        if completed.returncode != 0:
            logger.debug("System Python AT-SPI capture returned non-zero: %s", completed.stderr.strip())
            continue
        try:
            return AccessibilitySnapshot.model_validate_json(completed.stdout)
        except Exception:
            logger.debug("System Python AT-SPI capture returned invalid JSON.", exc_info=True)
            continue
    return None


def _prefer_system_dist_packages() -> None:
    """Put Debian GI bindings ahead of uv/pip packages when available."""

    for candidate in (Path("/usr/local/lib/python3/dist-packages"), Path("/usr/lib/python3/dist-packages")):
        if candidate.exists():
            candidate_path = str(candidate)
            if candidate_path in sys.path:
                sys.path.remove(candidate_path)
            sys.path.insert(0, candidate_path)


def _clear_partial_atspi_modules() -> None:
    """Drop half-initialized pyatspi/GI modules after an import failure."""

    for module_name in tuple(sys.modules):
        if module_name == "pyatspi" or module_name == "gi" or module_name.startswith("gi."):
            sys.modules.pop(module_name, None)


class LinuxAccessibilityBackend(AccessibilityBackend):
    """Collect desktop/window accessibility trees via AT-SPI."""

    _MAX_ATTRIBUTE_ITEMS = MAX_ATTRIBUTE_ITEMS
    _MAX_ATTRIBUTE_VALUE_CHARS = MAX_ATTRIBUTE_VALUE_CHARS
    _MAX_TEXT_CHARS = MAX_TEXT_CHARS

    def __init__(self) -> None:
        super().__init__(platform_name="linux", source_kind=AccessibilitySourceKind.ATSPI)

    def capabilities(self) -> AccessibilityCapabilities:
        """Report whether AT-SPI capture is available on this Linux session.

        Returns:
            Capability metadata for desktop and active-window capture, or an
            unsupported response when AT-SPI cannot be imported.
        """

        wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        message = "Wayland is not supported for accessibility capture in v1." if wayland else ""
        if not wayland:
            try:
                _import_pyatspi()
            except Exception as exc:
                if _system_python_pyatspi_available():
                    return self.default_capabilities(
                        supported_scopes=[AccessibilityScope.DESKTOP, AccessibilityScope.ACTIVE_WINDOW],
                        raw_formats=[],
                        message="AT-SPI available through system Python fallback.",
                    )
                logger.debug("AT-SPI capability probe failed.", exc_info=True)
                return self.default_capabilities(
                    supported_scopes=[],
                    raw_formats=[],
                    message=f"AT-SPI backend unavailable: {exc}",
                )
        return self.default_capabilities(
            supported_scopes=[AccessibilityScope.DESKTOP, AccessibilityScope.ACTIVE_WINDOW],
            permission_granted=not wayland,
            message=message,
        )

    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Capture an AT-SPI accessibility snapshot.

        Args:
            request: Capture scope and traversal limits.

        Returns:
            Normalized snapshot, or a failed snapshot when AT-SPI is unavailable
            or capture fails.
        """

        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            return self.unsupported_snapshot(
                scope=request.scope,
                message="Wayland is not supported for accessibility capture in v1.",
            )
        try:
            pyatspi = _import_pyatspi()
        except Exception as exc:
            fallback_snapshot = _capture_with_system_python(request)
            if fallback_snapshot is not None:
                return fallback_snapshot
            logger.debug("AT-SPI import failed during capture.", exc_info=True)
            return self.unsupported_snapshot(scope=request.scope, message=f"AT-SPI backend unavailable: {exc}")

        budget = AccessibilityTraversalBudget(
            max_depth=request.max_depth,
            max_children_per_node=request.max_children_per_node,
            max_nodes=request.max_nodes,
        )
        try:
            desktop = pyatspi.Registry.getDesktop(0)
            if request.scope == AccessibilityScope.ACTIVE_WINDOW:
                tree, window_ref = self._capture_active_window(pyatspi, desktop, budget)
            else:
                tree, window_ref = self._capture_desktop(pyatspi, desktop, budget)
        except Exception as exc:
            logger.warning("AT-SPI capture failed.", exc_info=True)
            return self.error_snapshot(scope=request.scope, message=f"AT-SPI capture failed: {exc}")

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
        pyatspi: object,
        desktop: ATSPIObject,
        budget: AccessibilityTraversalBudget,
    ) -> tuple[AccessibilityNode | None, object | None]:
        active_window = self._find_active_window(pyatspi, desktop)
        tree = (
            self._build_accessible_node(
                pyatspi,
                active_window,
                budget=budget,
                path="active-window",
                depth=0,
                seen=set(),
            )
            if active_window is not None
            else None
        )
        return tree, self._window_ref_from_element(pyatspi, active_window)

    def _capture_desktop(
        self,
        pyatspi: object,
        desktop: ATSPIObject,
        budget: AccessibilityTraversalBudget,
    ) -> tuple[AccessibilityNode, None]:
        children: list[AccessibilityNode] = []
        desktop_children = [desktop.getChildAtIndex(index) for index in range(desktop.childCount)]
        for index in budget.limit_children(len(desktop_children)):
            child = self._build_accessible_node(
                pyatspi,
                desktop_children[index],
                budget=budget,
                path=f"desktop/{index}",
                depth=1,
                seen=set(),
            )
            if child is not None:
                children.append(child)
        return AccessibilityNode(node_id="desktop-root", role="desktop", name="desktop", children=children), None

    def _find_active_window(self, pyatspi: object, desktop: ATSPIObject) -> ATSPIElement | None:
        """Find the currently active or focused AT-SPI window.

        Args:
            pyatspi: Imported AT-SPI module.
            desktop: AT-SPI desktop object.

        Returns:
            Active/focused window element, or ``None`` when none is discoverable.
        """

        state_active = getattr(pyatspi, "STATE_ACTIVE", None)
        state_focused = getattr(pyatspi, "STATE_FOCUSED", None)
        for app_index in range(desktop.childCount):
            app = desktop.getChildAtIndex(app_index)
            if app is None:
                continue
            for child_index in range(app.childCount):
                child = app.getChildAtIndex(child_index)
                if child is None:
                    continue
                try:
                    state_set = child.getState()
                except Exception:
                    logger.debug("Skipping AT-SPI child whose state cannot be queried.", exc_info=True)
                    continue
                if state_active is not None and state_set.contains(state_active):
                    return child
                if state_focused is not None and state_set.contains(state_focused):
                    return child
        return None

    def _build_accessible_node(
        self,
        pyatspi: object,
        element: ATSPIElement,
        *,
        budget: AccessibilityTraversalBudget,
        path: str,
        depth: int,
        seen: set[str],
    ) -> AccessibilityNode | None:
        """Build a normalized node from one AT-SPI element.

        Args:
            pyatspi: Imported AT-SPI module.
            element: Source AT-SPI element.
            budget: Traversal budget enforcing depth and size limits.
            path: Stable traversal path for the node.
            depth: Current traversal depth.
            seen: Element signatures already visited on this branch.

        Returns:
            Normalized accessibility node, or ``None`` when traversal skips it.
        """

        if element is None or not budget.claim_node(depth):
            return None
        signature = self._element_signature(element)
        if signature in seen:
            return None
        seen.add(signature)

        child_nodes: list[AccessibilityNode] = []
        child_count = int(getattr(element, "childCount", 0) or 0)
        for index in budget.limit_children(child_count):
            child = self._build_accessible_node(
                pyatspi,
                element.getChildAtIndex(index),
                budget=budget,
                path=f"{path}/{index}",
                depth=depth + 1,
                seen=seen,
            )
            if child is not None:
                child_nodes.append(child)

        return AccessibilityNode(
            node_id=path,
            role=str(element.getRoleName() or "node").lower(),
            name=str(getattr(element, "name", "") or ""),
            description=str(getattr(element, "description", "") or ""),
            value=self._value(element),
            text=self._text(element),
            bounds=self._bounds(pyatspi, element),
            states=self._states(pyatspi, element),
            actions=self._actions(element),
            attributes=self._attributes(element),
            children=child_nodes,
        )


LinuxAccessibilityBackend._states = staticmethod(states)
LinuxAccessibilityBackend._actions = staticmethod(actions)
LinuxAccessibilityBackend._bounds = staticmethod(bounds)
LinuxAccessibilityBackend._window_ref_from_element = staticmethod(window_ref_from_element)
LinuxAccessibilityBackend._attributes = staticmethod(attributes)
LinuxAccessibilityBackend._text = staticmethod(text)
LinuxAccessibilityBackend._value = staticmethod(value)
LinuxAccessibilityBackend._element_signature = staticmethod(element_signature)
