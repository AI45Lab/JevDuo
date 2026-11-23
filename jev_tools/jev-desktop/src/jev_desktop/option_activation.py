"""Activate an observed clickable option independently of selection state."""

from .selection import observed_options


def option_nodes(container):
    from .screen import collect

    found = {}
    for node in collect(container):
        node = {**node, "path": [*container.get("path", []), *node["path"]]}
        found.setdefault(node["ref_id"], []).append(node)
    return found


def activation_options(container, *, values=True):
    nodes = option_nodes(container)
    return [
        item
        for item in observed_options(container, values=values)
        if len(nodes.get(item.get("ref"), [])) == 1
        and "Click" in nodes[item["ref"]][0].get("available_actions", [])
        and nodes[item["ref"]][0].get("ref_actionable") is not False
    ]


def activation_node(container, option):
    matches = option_nodes(container).get(option.get("ref"), [])
    if len(matches) != 1 or "Click" not in matches[0].get("available_actions", []):
        raise ValueError("the observed option has no unique clickable ref; nothing ran")
    return matches[0]
