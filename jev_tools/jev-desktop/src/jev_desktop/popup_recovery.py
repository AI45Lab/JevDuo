"""Read-only reconciliation of an acknowledged opener with a new usable surface."""

from .policy import action_space
from .screen import describe, positive_bounds


def needs_popup_observation(outcome, operation, node):
    error = outcome.get("error") or {}
    details = error.get("details") or {}
    return (
        operation in {"CLICK", "POINTER_CLICK", "EXPAND"}
        and node is not None
        and "haspopup" in node.get("states", [])
        and error.get("code") == "ACTION_FAILED"
        and details.get("kind") == "popup_not_observed"
        and outcome.get("delivery") == "delivered_unverified"
        and error.get("disposition", {}).get("delivery") == "delivered_unverified"
        and any(
            step.get("mechanism") == "semantic_api" and step.get("outcome") == "succeeded"
            for step in details.get("steps", [])
        )
    )


def identity(node):
    return (node.get("role"), node.get("name"), tuple(node.get("path", [])))


def usable(node):
    bounds = node.get("bounds") or {}
    return (
        node.get("ref_actionable") is not False
        and not {"hidden", "offscreen", "disabled"}.intersection(node.get("states", []))
        and bounds.get("width", 0) > 0
        and bounds.get("height", 0) > 0
    )


def editable_input(node, headed):
    if node.get("role") != "textfield" or {"readonly", "secure"}.intersection(node.get("states", [])):
        return False
    space = action_space([node], headed=headed, drillable=False)
    return any(
        {"TYPE_TEXT", "INSERT_TEXT"}.intersection(entry["operations"])
        for entry in space["by_target"].values()
    )


def observed_popup(before, after, opener):
    """Require new usable controls in the same complete pinned document/window."""
    previous, current = before.screen, after.screen
    if (
        not previous.get("window_id")
        or current.get("window_id") != previous["window_id"]
        or current.get("app") != previous.get("app")
        or current.get("complete") is not True
        or current.get("root")
        or current.get("unclassified_surfaces")
        or current.get("surface") not in {"window", "menu", "alert", "sheet", "popover"}
    ):
        return None
    if current.get("surface") in {"alert", "sheet", "popover"}:
        role = current["surface"]
        prior = {(n.get("role"), n.get("name")) for n in previous.get("context", [])}
        roots = [
            n
            for n in current.get("context", [])
            if n.get("role") == role
            and n.get("name")
            and positive_bounds(n)
            and (role, n["name"]) not in prior
        ]
        if len(roots) != 1:
            return None
        label = role + f' "{roots[0]["name"]}"'
        items = [
            n for n in after.nodes if label in n.get("path", []) and n.get("available_actions") and usable(n)
        ]
        if not items:
            return None
        return {
            "window_id": current["window_id"],
            "surface": describe(roots[0]),
            "items": [describe(n) for n in items],
        }
    path = opener.get("path", [])
    if not path:
        return None
    document = next((i for i in reversed(range(len(path))) if path[i].split(" ", 1)[0] == "webarea"), 0)
    owner = path[: document + 1]
    prior_controls = {identity(n) for n in before.nodes}
    focused = [
        n
        for n in after.nodes
        if n.get("role") in {"textfield", "combobox", "listbox"}
        and "focused" in n.get("states", [])
        and usable(n)
        and n.get("path", [])[: len(owner)] == owner
        and identity(n) not in prior_controls
    ]
    if len(focused) == 1 and current.get("surface") == "window":
        return {
            "window_id": current["window_id"],
            "focused_new_control": describe(focused[0]),
            "owner": owner,
        }
    new_inputs = [
        n
        for n in after.nodes
        if editable_input(n, current.get("headed_text", False))
        and usable(n)
        and n.get("path", [])[: len(owner)] == owner
        and identity(n) not in prior_controls
    ]
    if len(new_inputs) == 1 and current.get("surface") == "window":
        return {
            "window_id": current["window_id"],
            "new_editable_control": describe(new_inputs[0]),
            "owner": owner,
        }
    prior = {identity(n) for n in before.nodes if n.get("role") == "menu"}
    menus = [
        n
        for n in after.nodes
        if n.get("role") == "menu"
        and "focused" in n.get("states", [])
        and usable(n)
        and n.get("path", [])[: len(owner)] == owner
        and identity(n) not in prior
    ]
    if len(menus) != 1:
        return None
    menu = menus[0]
    descendants = [*menu["path"], "menu" + (f' "{menu["name"]}"' if menu.get("name") else "")]
    items = [
        n
        for n in after.nodes
        if n.get("role") in {"menuitem", "checkmenuitem", "radiomenuitem"}
        and n.get("path", [])[: len(descendants)] == descendants
        and n.get("available_actions")
        and usable(n)
    ]
    if not items:
        return None
    return {
        "window_id": current["window_id"],
        "menu": describe(menu),
        "items": [describe(item) for item in items],
    }
