"""Pure accessibility-tree descriptions and capability filtering."""

import math


def quoted(value):
    return f' "{value}"' if value else ""


def rounded(value):
    return math.floor(value + 0.5)


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
    if tree["role"] in ("sheet", "alert", "menu", "popover"):
        return tree["role"]
    for child in tree.get("children", []):
        found = overlay_role(child)
        if found:
            return found
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
    if '"' not in result["what"] and node.get("bounds"):
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
