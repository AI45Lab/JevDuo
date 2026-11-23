"""Pure accessibility-tree descriptions and capability filtering."""

import math


def quoted(value):
    return f' "{value}"' if value else ""


def rounded(value):
    return math.floor(value + 0.5)


def positive_bounds(node):
    bounds = node.get("bounds") or {}
    return (
        all(
            isinstance(bounds.get(key), (int, float)) and math.isfinite(bounds[key])
            for key in ("x", "y", "width", "height")
        )
        and bounds["width"] > 0
        and bounds["height"] > 0
    )


def target_identity(node):
    return {key: node.get(key) for key in ("role", "name", "path", "bounds")}


def selection_still_unselected(node, nodes):
    if (
        node.get("role") not in {"cell", "row", "treeitem", "option", "tab", "listitem"}
        or "Select" not in node.get("available_actions", [])
        or "selected" in node.get("states", [])
    ):
        return False
    matches = [n for n in nodes if target_identity(n) == target_identity(node)]
    return (
        len(matches) == 1
        and matches[0].get("ref_actionable") is not False
        and "Select" in matches[0].get("available_actions", [])
        and "selected" not in matches[0].get("states", [])
    )


def expansion_still_collapsed(node, nodes):
    if (
        node.get("role") not in {"cell", "row", "treeitem", "disclosure"}
        or "Expand" not in node.get("available_actions", [])
        or "expanded" in node.get("states", [])
    ):
        return False
    matches = [n for n in nodes if target_identity(n) == target_identity(node)]
    return (
        len(matches) == 1
        and matches[0].get("ref_actionable") is not False
        and "Expand" in matches[0].get("available_actions", [])
        and "expanded" not in matches[0].get("states", [])
    )


def collect(tree):
    found = []

    def walk(node, path, parent_role):
        if node.get("ref_id"):
            found.append({**node, "path": path, "parentRole": parent_role})
        children = node.get("children", [])
        next_path = [*path, node["role"] + quoted(node.get("name"))] if children else path
        for child in children:
            walk(child, next_path, node["role"])

    walk(tree, [], None)
    return found


def offerable(nodes):
    return [
        n
        for n in nodes
        if not {"disabled", "hidden"}.intersection(n.get("states", []))
        and n.get("available_actions")
        and not (n["role"] == "cell" and n.get("parentRole") == "treeitem")
    ]


def outside_window(data, window_id):
    ids = [data.get("window", {}).get("id"), data.get("surface", {}).get("window_id")]
    ids = [value for value in ids if value is not None]
    return not ids or any(value != window_id for value in ids)


def overlay_role(tree):
    found = set()

    def visit(node):
        if "hidden" in node.get("states", []):
            return
        if node["role"] == "webarea":
            return
        if "offscreen" not in node.get("states", []):
            found.add(node["role"])
        for child in node.get("children", []):
            visit(child)

    visit(tree)
    return next((role for role in ("sheet", "alert", "popover", "menu") if role in found), None)
    return None


def describe(node, rich=False, *, values=True):
    result = {
        "what": node["role"] + quoted(node.get("name", node.get("description"))),
        "where": " > ".join(node.get("path", [])) or "top level",
    }
    if values and node.get("value") not in (None, ""):
        result["holds"] = str(node["value"])[:120]
    if node.get("states"):
        result["state"] = ", ".join(node["states"])
    if '"' not in result["what"] and positive_bounds(node):
        result["at"] = f"x {rounded(node['bounds']['x'])}, y {rounded(node['bounds']['y'])}"
    if rich:
        if node.get("available_actions"):
            result["supports"] = ", ".join(node["available_actions"])
        if node.get("children_count"):
            result["contains"] = f"{node['children_count']} items not shown"
    return result


def fingerprint(nodes):
    """Compare structured fields directly, without hashes or checksums."""
    return [
        [
            n.get("role"),
            n.get("name"),
            n.get("value"),
            n.get("states", []),
            n.get("description"),
            n.get("bounds"),
            n.get("available_actions", []),
        ]
        for n in nodes
    ]
