"""Summary/statistics helpers for normalized accessibility trees."""

from __future__ import annotations

from collections.abc import Iterator

from northcua.core.accessibility import AccessibilityCaptureStats, AccessibilityNode, AccessibilitySummary

_ACTIONABLE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "menuitem",
    "radio",
    "slider",
    "textbox",
    "text_field",
}


def iter_nodes(root: AccessibilityNode | None) -> Iterator[tuple[AccessibilityNode, int, list[AccessibilityNode]]]:
    """Yield nodes with depth and ancestry path.

    Args:
        root: Root node for traversal, or None when no tree is available.

    Returns:
        Iterator of node, depth, and parent-list tuples in depth-first order.
    """

    if root is None:
        return

    stack: list[tuple[AccessibilityNode, int, list[AccessibilityNode]]] = [(root, 0, [])]
    while stack:
        node, depth, parents = stack.pop()
        yield node, depth, parents
        for child in reversed(node.children):
            stack.append((child, depth + 1, [*parents, node]))


def calculate_capture_stats(
    root: AccessibilityNode | None,
    *,
    capture_ms: float,
    truncated: bool,
) -> AccessibilityCaptureStats:
    """Calculate node-count and max-depth from a normalized tree.

    Args:
        root: Root node for traversal, or None when no tree is available.
        capture_ms: Elapsed capture time in milliseconds.
        truncated: Whether traversal limits truncated the snapshot.

    Returns:
        Normalized capture statistics.
    """

    node_count = 0
    max_depth = 0
    for _node, depth, _parents in iter_nodes(root):
        node_count += 1
        max_depth = max(max_depth, depth)
    return AccessibilityCaptureStats(
        node_count=node_count,
        max_depth=max_depth,
        truncated=truncated,
        capture_ms=round(capture_ms, 2),
    )


class AccessibilitySummaryBuilder:
    """Build compact focus/action summaries from normalized trees."""

    def build(self, root: AccessibilityNode | None) -> AccessibilitySummary | None:
        """Return a summary for the given tree, or `None` if no tree exists.

        Args:
            root: Root node for summary extraction.

        Returns:
            Compact accessibility summary, or None when no tree exists.
        """

        if root is None:
            return None

        focused_role = ""
        focused_name = ""
        focused_path: list[str] = []
        actionable_count = 0

        for node, _depth, parents in iter_nodes(root):
            if node.actions or node.role in _ACTIONABLE_ROLES:
                actionable_count += 1
            if "focused" in node.states and not focused_path:
                focused_role = node.role
                focused_name = node.name
                focused_path = [ancestor.name or ancestor.role for ancestor in [*parents, node]]

        return AccessibilitySummary(
            root_role=root.role,
            root_name=root.name,
            focused_role=focused_role,
            focused_name=focused_name,
            focused_path=focused_path,
            actionable_count=actionable_count,
        )
