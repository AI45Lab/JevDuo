"""Read an embedded combo value from explicit native selection, never focus."""


def selected_value(node):
    if node.get("role") != "combobox" or node.get("value") != "\ufffc":
        return None
    selected, incomplete = [], bool(node.get("children_count"))

    def visit(child):
        nonlocal incomplete
        role = child.get("role")
        if role in {"combobox", "table", "grid", "tablist", "radiogroup", "outline"}:
            return
        incomplete |= bool(child.get("children_count"))
        if role in {"option", "menuitem"} and "selected" in child.get("states", []):
            selected.append(child.get("name"))
        for descendant in child.get("children", []):
            visit(descendant)

    for child in node.get("children", []):
        visit(child)
    if not incomplete and len(selected) == 1 and isinstance(selected[0], str) and selected[0].strip():
        return selected[0]
    return None


def selection_values(tree):
    result = dict(tree)
    value = selected_value(tree)
    if value is not None:
        result.update(value=value, value_source="native_selected_option")
    if "children" in tree:
        result["children"] = [selection_values(child) for child in tree["children"]]
    return result
