"""Copy snapshot-reported focus without claiming selection or receiver identity."""


def reported_focus(screen):
    return [
        {key: node[key] for key in ("role", "name", "states", "bounds") if key in node}
        for node in screen.get("context", [])
        if "focused" in node.get("states", [])
        and not {"hidden", "offscreen"}.intersection(node.get("states", []))
    ]


def activation_key(operation, node):
    if operation not in {"PRESS_KEY:return", "PRESS_KEY:space"} or not node:
        return False
    focus = node.get("reported_focus", [])
    return (
        node.get("role") == "window" and bool(node.get("window_id"))
        and node.get("focus_observation_complete") is True
        and len(focus) == 1 and "focused" in focus[0].get("states", [])
        and focus[0].get("role") in {"button", "menubutton", "option", "listitem", "row", "treeitem", "tab", "menuitem", "checkbox", "radiobutton", "switch", "link"}
        and not {"editable", "secure", "disabled", "hidden", "offscreen"}.intersection(focus[0].get("states", []))
    )
