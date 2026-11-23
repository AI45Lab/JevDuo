"""Bound caller-selected rereads of an initially empty document observation."""


def empty_document(seen):
    screen = seen.screen
    if (
        screen.get("complete") is not True
        or screen.get("unclassified_surfaces")
        or screen.get("root")
        or screen.get("surface") != "window"
    ):
        return False
    if not seen.nodes:
        return screen.get("empty_document_structure") is True
    return all(
        node.get("role") == "webarea"
        and not node.get("value")
        and not node.get("children")
        and not node.get("children_count")
        and not node.get("subtree_truncated")
        for node in seen.nodes
    ) and all(
        entry.get("role") in {"window", "webarea"} and not entry.get("value")
        for entry in screen.get("context", [])
    )


def empty_document_tree(tree):
    webareas = 0

    def visit(node):
        nonlocal webareas
        if {"hidden", "offscreen"}.intersection(node.get("states", [])):
            return True
        if node.get("role") not in {"window", "webarea"} or node.get("value") or node.get("children_count") or node.get("subtree_truncated"):
            return False
        webareas += node.get("role") == "webarea"
        return all(visit(child) for child in node.get("children", []))

    return visit(tree) and webareas > 0
