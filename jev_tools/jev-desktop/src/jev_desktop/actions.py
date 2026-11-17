"""Finite native actions and caller-controlled arguments; never shell commands."""

DEFAULT_KEYS = (
    "return",
    "escape",
    "tab",
    "shift+tab",
    "space",
    "backspace",
    "delete",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pageup",
    "pagedown",
    "ctrl+home",
    "ctrl+end",
    "shift+left",
    "shift+right",
    "ctrl+shift+left",
    "ctrl+shift+right",
    "ctrl+a",
    "ctrl+c",
    "ctrl+x",
    "ctrl+v",
    "ctrl+z",
    "ctrl+y",
    "ctrl+s",
    "ctrl+shift+s",
    "ctrl+f",
    "ctrl+h",
    "ctrl+l",
    "ctrl+t",
    "ctrl+w",
    "alt+left",
    "alt+right",
    "alt+f4",
    "f1",
    "f5",
    "ctrl+n",
    "ctrl+o",
    "ctrl+k",
    "alt+f",
    "alt+i",
    "alt+o",
    "f10",
)
TEXT_OPERATIONS = {"TYPE_TEXT", "INSERT_TEXT"}


def operation_description(operation):
    from .policy import OPS, VIEW

    if operation.startswith("PRESS_KEY:"):
        if operation == "PRESS_KEY:return":
            return (
                "Press Enter on the pinned window's current keyboard focus: submit a filled field, navigate an "
                "address, or activate the highlighted choice when the application supports it. No text is supplied."
            )
        return "Send " + operation.partition(":")[2] + " to the pinned window's current keyboard focus."
    if operation.startswith("DRAG_TO:"):
        return "Drag this element onto the observed target " + operation.partition(":")[2] + "."
    return OPS[operation][1] if operation in OPS else VIEW[operation]


def extend_space(space, screen, options):
    """Bind window keys and drag destinations to this observation, then apply caller restrictions."""
    targets = space["by_target"]
    if screen.get("headed_text"):
        destinations = {
            index: entry["node"] for index, entry in targets.items() if entry["node"].get("bounds")
        }
        for entry in targets.values():
            if entry["node"].get("bounds"):
                entry["node"]["drag_targets"] = {
                    index: destination["ref_id"]
                    for index, destination in destinations.items()
                    if destination["ref_id"] != entry["node"]["ref_id"]
                }
                entry["node"]["drag_candidates"] = {
                    index: {
                        key: destination[key]
                        for key in ("role", "name", "value", "bounds")
                        if key in destination
                    }
                    for index, destination in destinations.items()
                    if index in entry["node"]["drag_targets"]
                }
                if entry["node"]["drag_targets"]:
                    entry["operations"].append("DRAG")
        if screen.get("window_id"):
            targets["WINDOW"] = {
                "node": {"role": "window", "name": screen.get("window"), "window_id": screen["window_id"]},
                "operations": ["FOCUS_WINDOW", *("PRESS_KEY:" + key for key in options.keys)],
            }
    allowed = options.allowed_operations
    for index, entry in list(targets.items()):
        entry["operations"] = [
            op
            for op in entry["operations"]
            if (options.input_mode != "insert" or op not in ("TYPE_TEXT", "CLEAR"))
            and (options.input_mode != "replace" or op != "INSERT_TEXT")
            and (allowed is None or op in allowed or op.partition(":")[0] in allowed)
        ]
        if not entry["operations"]:
            del targets[index]
    space["elements"] = [line for index, line in enumerate(space["elements"], 1) if str(index) in targets]
    return space
