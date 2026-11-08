"""macOS AX tree traversal helpers."""

from __future__ import annotations

from northcua.core.accessibility import AccessibilityBounds, AccessibilityNode

from .base import AccessibilityTraversalBudget
from .macos_ax import (
    AXElement,
    QuartzWindowInfo,
    _ax_actions,
    _ax_bounds,
    _ax_element_signature,
    _ax_object_children,
    _ax_scalar_attributes,
    _ax_states,
    _ax_string_attribute,
    _deadline_exceeded,
    _focused_window_for_application,
    _normalize_ax_token,
    _preferred_ax_name,
    _resolve_ax_window,
    _same_ax_element,
)


def _build_window_node(
    window_info: QuartzWindowInfo,
    *,
    ApplicationServices: object,
    budget: AccessibilityTraversalBudget,
    path: str,
    depth: int,
    expand_children: bool,
    deadline_at: float,
) -> AccessibilityNode | None:
    """Build a normalized node for one visible macOS window."""

    if not budget.claim_node(depth):
        return None

    owner_name = str(window_info.get("kCGWindowOwnerName") or "")
    title = str(window_info.get("kCGWindowName") or "")
    pid = int(window_info.get("kCGWindowOwnerPID") or 0)
    bounds_dict = dict(window_info.get("kCGWindowBounds") or {})
    bounds = AccessibilityBounds(
        x=int(bounds_dict.get("X") or 0),
        y=int(bounds_dict.get("Y") or 0),
        width=int(bounds_dict.get("Width") or 0),
        height=int(bounds_dict.get("Height") or 0),
    )

    attributes = {"app_name": owner_name}
    element = _resolve_ax_window(ApplicationServices, pid, title, bounds)
    focused_window = _focused_window_for_application(ApplicationServices, pid)
    if element is not None and expand_children and not _deadline_exceeded(deadline_at):
        node = _build_ax_node(
            element,
            ApplicationServices=ApplicationServices,
            budget=budget,
            path=path,
            depth=depth,
            seen=set(),
            fallback_name=title or owner_name,
            forced_bounds=bounds,
            extra_attributes=attributes,
            forced_states=["focused"] if _same_ax_element(element, focused_window) else None,
            deadline_at=deadline_at,
        )
        if node is not None:
            return node
    elif _deadline_exceeded(deadline_at):
        budget.truncated = True

    return AccessibilityNode(
        node_id=path,
        role="window",
        name=title or owner_name,
        bounds=bounds,
        states=["enabled"],
        attributes=attributes,
        children=[],
    )


def _build_ax_node(
    element: AXElement,
    *,
    ApplicationServices: object,
    budget: AccessibilityTraversalBudget,
    path: str,
    depth: int,
    seen: set[str],
    fallback_name: str = "",
    forced_bounds: AccessibilityBounds | None = None,
    extra_attributes: dict[str, str] | None = None,
    forced_states: list[str] | None = None,
    deadline_at: float | None = None,
) -> AccessibilityNode | None:
    """Build a normalized node from one AX element."""

    if _deadline_exceeded(deadline_at):
        budget.truncated = True
        return None
    if element is None or not budget.claim_node(depth):
        return None

    signature = _ax_element_signature(element)
    if signature in seen:
        return None
    seen.add(signature)

    role = _normalize_ax_token(_ax_string_attribute(ApplicationServices, element, "AXRole") or "node")
    name = _preferred_ax_name(ApplicationServices, element) or fallback_name
    value = _ax_string_attribute(ApplicationServices, element, "AXValue")
    text = value if value and role in {"statictext", "text", "text_field", "textfield", "textarea"} else ""
    description = _ax_string_attribute(ApplicationServices, element, "AXRoleDescription") or _ax_string_attribute(
        ApplicationServices,
        element,
        "AXDescription",
    )
    states = _ax_states(ApplicationServices, element)
    for state in forced_states or []:
        if state not in states:
            states.append(state)
    bounds = forced_bounds or _ax_bounds(ApplicationServices, element)
    actions = [_normalize_ax_token(action) for action in _ax_actions(ApplicationServices, element)]
    attributes = dict(extra_attributes or {})
    attributes.update(_ax_scalar_attributes(ApplicationServices, element))
    children: list[AccessibilityNode] = []
    raw_children = _ax_object_children(ApplicationServices, element)
    for index in budget.limit_children(len(raw_children)):
        relation_name, child_element = raw_children[index]
        child = _build_ax_node(
            child_element,
            ApplicationServices=ApplicationServices,
            budget=budget,
            path=f"{path}/{index}",
            depth=depth + 1,
            seen=seen,
            forced_states=None,
            deadline_at=deadline_at,
        )
        if child is not None:
            children.append(_with_relation_attribute(child, relation_name))

    return AccessibilityNode(
        node_id=path,
        role=role,
        name=name,
        description=description,
        value=value,
        text=text,
        bounds=bounds,
        states=states,
        actions=actions,
        attributes=attributes,
        children=children,
    )


def _with_relation_attribute(child: AccessibilityNode, relation_name: str) -> AccessibilityNode:
    if not relation_name or relation_name == "AXChildren" or "ax_relation" in child.attributes:
        return child
    return child.model_copy(
        update={
            "attributes": {
                **child.attributes,
                "ax_relation": _normalize_ax_token(relation_name),
            }
        }
    )
