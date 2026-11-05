"""Observable stage postconditions; unknown or ambiguous evidence never proves success."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def controls(tree: str | None) -> list[dict]:
    """Read active, visible controls, including non-actionable labels and paragraphs."""
    if tree is None:
        return []
    result = []

    def visit(node, parents=(), active=False, hidden=False):
        attrs = {key.rsplit("}", 1)[-1]: value for key, value in node.attrib.items()}
        active = active or attrs.get("active") == "true"
        hidden = hidden or attrs.get("showing") == "false" or attrs.get("visible") == "false"
        name = attrs.get("name", "")
        if active and not hidden:
            result.append({
                "name": name, "role": node.tag.rsplit("}", 1)[-1].replace("-", "_"),
                "parents": parents, "value": attrs.get("value", node.text or ""),
                **{key: attrs.get(key) == "true" if key in attrs else None
                   for key in ("focused", "selected", "checked", "expanded")},
            })
        for child in node:
            visit(child, parents + ((name,) if name else ()), active, hidden)

    visit(ET.fromstring(tree))
    return result


def matches(items, query):
    return [item for item in items
            if item["role"] == query["role"] and item["name"] == query["name"]
            and (not query.get("parent_contains")
                 or any(query["parent_contains"] in parent for parent in item["parents"]))]


def check_state(items, check) -> str:
    found = matches(items, check["target"])
    if len(found) != 1:
        return "uncertain"
    observed = True if check["property"] == "visible" else found[0].get(check["property"])
    if observed is None:
        return "uncertain"
    return "met" if observed == check["expected"] else "not_met"


def rule_verification(pending, receipt, tree):
    """Return None for semantic model verification; never use exit=0 as success proof."""
    base = {"task_status": "incomplete", "path_input_focused": False, "state_changed": False}
    if not receipt.acknowledged or receipt.returncode != 0:
        return {**base, "outcome": "not_achieved", "evidence": "Execution receipt failed.",
                "source": "rule_receipt"}
    checks = pending["plan"].get("checks", [])
    if not checks:
        return None
    before, after = controls(pending.get("before_tree")), controls(tree)
    states = [check_state(after, check) for check in checks]
    if "uncertain" in states:
        return None
    changed = before != after
    if "not_met" in states:
        # Only scrolling may repeat locally; never blindly repeat clicks, typing or commands.
        repeat = pending["kind"] == "gui" and pending["winner"].operation == "scroll" and changed
        return {**base, "outcome": "uncertain" if repeat else "not_achieved",
                "state_changed": changed, "continue_local": repeat,
                "evidence": f"Observable postconditions: {states}.", "source": "rule_a11y"}
    # A condition already true before the action is not evidence of its effect.
    if not any(check_state(before, check) != "met" for check in checks):
        return None
    return {**base, "outcome": "achieved", "state_changed": changed,
            "evidence": "All postconditions met, including a newly observed condition.",
            "source": "rule_a11y"}
