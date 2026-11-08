"""Basic diff support for consecutive accessibility snapshots."""

from __future__ import annotations

from collections import Counter

from northcua.core.accessibility import AccessibilityDiffSummary, AccessibilityNode, AccessibilitySnapshot

from .summary import iter_nodes


def _node_signature(node: AccessibilityNode) -> tuple[object, ...]:
    """Build a comparable signature for one normalized node.

    Args:
        node: Accessibility node to summarize.

    Returns:
        Tuple containing stable node fields used for multiset comparison.
    """

    bounds = None
    if node.bounds is not None:
        bounds = (node.bounds.x, node.bounds.y, node.bounds.width, node.bounds.height)
    return (
        node.role,
        node.name,
        node.description,
        node.value,
        node.text,
        bounds,
        tuple(node.states),
        tuple(node.actions),
        tuple(sorted(node.attributes.items())),
    )


def _node_signatures(root: AccessibilityNode | None) -> Counter[tuple[object, ...]]:
    """Count node signatures in a normalized tree.

    Args:
        root: Root node, or ``None`` when no tree is available.

    Returns:
        Multiset of node signatures.
    """

    return Counter(_node_signature(node) for node, _depth, _parents in iter_nodes(root))


class AccessibilityDiffBuilder:
    """Compute a compact diff summary between two normalized snapshots."""

    def build(
        self,
        previous: AccessibilitySnapshot | None,
        current: AccessibilitySnapshot | None,
    ) -> AccessibilityDiffSummary | None:
        """Return a basic structural diff, or `None` if unavailable.

        Args:
            previous: Snapshot from the previous step.
            current: Snapshot from the current step.

        Returns:
            Compact diff summary, or None when either snapshot is unavailable.
        """

        if previous is None or current is None:
            return None

        previous_signatures = _node_signatures(previous.tree)
        current_signatures = _node_signatures(current.tree)
        added_counter = current_signatures - previous_signatures
        removed_counter = previous_signatures - current_signatures
        changed_nodes = 0
        if previous.tree is not None and current.tree is not None:
            if _node_signature(previous.tree) != _node_signature(current.tree):
                changed_nodes = 1

        previous_focus = list(previous.summary.focused_path) if previous.summary else []
        current_focus = list(current.summary.focused_path) if current.summary else []

        return AccessibilityDiffSummary(
            added_nodes=sum(added_counter.values()),
            removed_nodes=sum(removed_counter.values()),
            changed_nodes=changed_nodes,
            focus_changed=previous_focus != current_focus,
            previous_focused_path=previous_focus,
            current_focused_path=current_focus,
        )
