"""Finite native actions and caller-controlled arguments; never shell commands."""

from .focus_context import reported_focus
from .option_activation import activation_options
from .screen import positive_bounds, target_identity

DEFAULT_KEYS = (
    "return",
    "escape",
    "shift+escape",
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
    "shift+up",
    "shift+down",
    "shift+home",
    "shift+end",
    "ctrl+shift+left",
    "ctrl+shift+right",
    "ctrl+shift+home",
    "ctrl+shift+end",
    "ctrl+a",
    "ctrl+b",
    "ctrl+i",
    "ctrl+u",
    "ctrl+e",
    "ctrl+j",
    "ctrl+r",
    "ctrl+c",
    "ctrl+x",
    "ctrl+v",
    "ctrl+z",
    "ctrl+y",
    "ctrl+s",
    "ctrl+shift+s",
    "ctrl+f",
    "ctrl+h",
    "ctrl+g",
    "ctrl+shift+p",
    "ctrl+shift+e",
    "ctrl+shift+f",
    "ctrl+shift+x",
    "ctrl+l",
    "ctrl+t",
    "ctrl+w",
    "alt+left",
    "alt+right",
    "alt+f4",
    *(f"f{number}" for number in range(1, 13)),
    "ctrl+n",
    "ctrl+o",
    "ctrl+k",
    *(f"alt+{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"),
    "alt+f12",
    "shift+f10",
    "ctrl+up",
    "ctrl+down",
    "ctrl+shift+up",
    "ctrl+shift+down",
    "alt+home",
    "alt+down",
    "ctrl+shift+n",
    "ctrl+shift+a",
    "shift+f3",
    "ctrl+return",
    "ctrl+f12",
    "ctrl+shift+b",
    "ctrl+m",
    "c",
    "t",
    "p",
    "l",
    "s",
)
TEXT_OPERATIONS = {"TYPE_TEXT", "INSERT_TEXT"}


def operation_description(operation):
    from .policy import OPS, VIEW

    if operation.startswith("OPEN_RESOURCE:"):
        return "Open this caller-bound resource in the verified application; verify the resulting document before continuing."
    if operation == "MAXIMIZE_WINDOW":
        return "Maximize this pinned application window to expose more of its interface; this does not change document content."
    if operation == "ACTIVATE_OPTION":
        return (
            "Choose and click an observed option using its own ref, activating it. "
            "Use this to open a listed folder, file or command. SELECT changes selection only; "
            "activation can navigate or execute the option and never generates text."
        )

    if operation == "SWITCH_WINDOW":
        return "Observe this other live window of the same verified application process. No application data changes."

    if operation.startswith("PRESS_KEY:"):
        if operation == "PRESS_KEY:ctrl+o":
            return (
                "Open the application's file chooser. Observe its actual path field before requesting text, "
                "enter only the caller-requested resource path, then confirm and verify the opened resource. "
                "This operation supplies no path and does not launch a process with generated arguments."
            )
        if operation == "PRESS_KEY:f2":
            return (
                "Press F2 on the pinned window. In editors this can enter text or rename mode for a selected "
                "item. Confirm the item is selected first, then observe the new editing focus before writing."
            )
        if operation == "PRESS_KEY:shift+escape":
            return (
                "Press Shift+Escape on the pinned window. If this application's command search opens, "
                "observe its focused search field and displayed results before entering or activating anything."
            )
        if operation in {"PRESS_KEY:tab", "PRESS_KEY:shift+tab"}:
            return (
                "Press Tab on the reported focus. Many controls move focus forward; an editor may insert indentation. Inspect the current control and resulting state."
                if operation == "PRESS_KEY:tab" else
                "Press Shift+Tab on the reported focus. Many controls move focus backward; an editor may decrease indentation. Inspect the current control and resulting state."
            )
        if operation == "PRESS_KEY:shift+f10":
            return (
                "Open the context menu for the current keyboard focus. Select and verify the intended "
                "item or set first when the command acts on a selection; FOCUS alone does not select. "
                "Then FOCUS the intended control and inspect the resulting menu before choosing an item."
            )
        if operation == "PRESS_KEY:space":
            return (
                "Press Space on the current keyboard focus to check a focused radio button or toggle a checkbox. "
                "Verify the resulting checked state before confirming the form."
            )
        if operation == "PRESS_KEY:return":
            return (
                "Press Enter on the pinned window's current keyboard focus: submit a filled field, navigate an "
                "address, or activate the highlighted choice when the application supports it. In a multiline editor "
                "it may insert a newline. No text is supplied."
            )
        return "Send " + operation.partition(":")[2] + " to the pinned window's current keyboard focus."
    if operation.startswith("DRAG_TO:"):
        return "Drag this element onto the observed target " + operation.partition(":")[2] + "."
    return OPS[operation][1] if operation in OPS else VIEW[operation]


def extend_space(space, screen, options):
    """Bind window keys and drag destinations to this observation, then apply caller restrictions."""
    targets = space["by_target"]
    for entry in targets.values():
        choices = activation_options(entry["node"], values=options.values)
        if choices:
            entry["activation_options"] = choices
            if "ACTIVATE_OPTION" not in entry["operations"]:
                entry["operations"].append("ACTIVATE_OPTION")
    space["allow_infeasible"] = options.allow_infeasible
    space["review_goal_scope"] = options.review_goal_scope
    if screen.get("headed_text"):
        destinations = {
            index: entry["node"] for index, entry in targets.items() if positive_bounds(entry["node"])
        }
        for entry in targets.values():
            entry["operations"] = [op for op in entry["operations"] if op != "DRAG"]
            if positive_bounds(entry["node"]):
                entry["node"]["drag_targets"] = {
                    index: destination["ref_id"]
                    for index, destination in destinations.items()
                    if destination["ref_id"] != entry["node"]["ref_id"]
                    and not any(
                        action.get("source_identity") == target_identity(entry["node"])
                        and action.get("destination_identity") == target_identity(destination)
                        and action.get("window_id") == screen.get("window_id")
                        for action in options.blocked_actions
                    )
                }
                entry["node"]["drag_candidates"] = {
                    index: {
                        key: destination[key]
                        for key in ("role", "name", "value", "bounds", "path")
                        if key in destination
                    }
                    for index, destination in destinations.items()
                    if index in entry["node"]["drag_targets"]
                }
                if entry["node"]["drag_targets"]:
                    entry["operations"].append("DRAG")
        if screen.get("window_id"):
            targets["WINDOW"] = {
                "node": {
                    "role": "window", "name": screen.get("window"), "window_id": screen["window_id"],
                    "reported_focus": reported_focus(screen),
                    "focus_observation_complete": screen.get("complete") is True
                    and bool(screen.get("app")) and not screen.get("root")
                    and screen.get("surface") == "window" and not screen.get("unclassified_surfaces"),
                },
                "operations": ["FOCUS_WINDOW", "MAXIMIZE_WINDOW", *("PRESS_KEY:" + key for key in options.keys)],
            }
            if screen.get("app") and options.resources:
                targets["WINDOW"]["node"]["resources"] = {str(i): path for i, path in enumerate(options.resources)}
                targets["WINDOW"]["operations"].extend("OPEN_RESOURCE:" + str(i) for i in range(len(options.resources)))
    allowed = options.allowed_operations
    blocked = (
        options.blocked_actions if options.replan_safe_failures or options.reconcile_observed_popups else ()
    )
    for index, entry in list(targets.items()):
        node = entry["node"]
        label = node["role"] + (f' "{node["name"]}"' if node.get("name") else "")
        unavailable = {
            operation
            for action in blocked
            if action.get("target") in (label, "*")
            and action.get("window_id") in (None, screen.get("window_id"))
            for operation in (
                entry["operations"] if action.get("operation") == "*" else [action.get("operation")]
            )
        }
        if any(
            action.get("target") in (label, "*")
            and action.get("window_id") in (None, screen.get("window_id"))
            and str(action.get("operation", "")).startswith("DRAG_TO:")
            and not action.get("destination_identity")
            for action in blocked
        ):
            unavailable.add("DRAG")
        entry["operations"] = [
            op
            for op in entry["operations"]
            if "*" not in unavailable
            and op not in unavailable
            and (options.input_mode != "insert" or op not in ("TYPE_TEXT", "CLEAR"))
            and (options.input_mode != "replace" or op != "INSERT_TEXT")
            and (allowed is None or op in allowed or op.partition(":")[0] in allowed)
        ]
        if not entry["operations"]:
            del targets[index]
    space["elements"] = [line for index, line in enumerate(space["elements"], 1) if str(index) in targets]
    return space
