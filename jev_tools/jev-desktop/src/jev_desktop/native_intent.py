"""Withhold aliases of acknowledged actions without relying on snapshot refs."""

from .focus_context import activation_key


def intent(scope, operation, node, value, nodes):
    receiver = node
    if operation in {"SELECT", "SELECT_ITEM"}:
        kind = "select"
        binding = node.get("selection_container") if operation == "SELECT_ITEM" else None
        if binding:
            owners = [item for item in nodes if item.get("ref_id") == binding["ref_id"]]
            receiver = owners[0] if len(owners) == 1 else None
            value = binding["label"]
    elif operation in {"CLICK", "POINTER_CLICK", "ACTIVATE_OPTION"}:
        kind, value = "pointer_click" if operation == "POINTER_CLICK" else "click", None
    elif activation_key(operation, node) and node["reported_focus"][0].get("role") in {"button", "menubutton"}:
        receiver = node["reported_focus"][0]
        candidates = [item for item in nodes if all(item.get(key) == receiver.get(key) for key in ("role", "name"))
                      and (not receiver.get("bounds") or item.get("bounds") == receiver["bounds"])]
        if len(candidates) == 1:
            receiver = candidates[0]
        kind, value = "click", None
    else:
        return None
    return {
        "app": scope.app,
        "window_id": scope.window_id,
        "kind": kind,
        "receiver": {key: receiver.get(key) for key in ("role", "name", "path", "bounds")} if receiver else None,
        "value": value.strip().casefold() if isinstance(value, str) else value,
    }


def matches(previous, current):
    if not previous or not current:
        return False
    if not all(previous[key] == current[key] for key in ("app", "window_id", "kind", "value")):
        return False
    if previous.get("exact_state"):
        return previous["receiver"] is not None and previous["receiver"] == current["receiver"]
    return previous["receiver"] is None or current["receiver"] is None or all(
        previous["receiver"].get(key) == current["receiver"].get(key) for key in ("role", "name")
    )


def filter_intents(space, scope, nodes, blocked):
    for key, entry in list(space["by_target"].items()):
        node = entry["node"]
        for operation, field in (("SELECT", "selection_options"), ("ACTIVATE_OPTION", "activation_options")):
            if operation not in entry["operations"]:
                continue
            remaining = []
            for option in entry.get(field, []):
                receiver = node
                if operation == "ACTIVATE_OPTION":
                    from .option_activation import activation_node

                    receiver = activation_node(node, option)
                current = intent(scope, operation, receiver, option["label"], nodes)
                if not any(matches(previous, current) for previous in blocked):
                    remaining.append(option)
            entry[field] = remaining
            if not remaining:
                entry["operations"].remove(operation)
        entry["operations"] = [
            operation for operation in entry["operations"]
            if operation not in {"CLICK", "POINTER_CLICK", "SELECT_ITEM", "PRESS_KEY:return", "PRESS_KEY:space"}
            or not any(matches(previous, intent(scope, operation, node, node.get("name"), nodes)) for previous in blocked)
        ]
        if not entry["operations"]:
            del space["by_target"][key]
