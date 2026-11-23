"""Explain excluded refs without offering unproven controls as action targets."""


def summarize_refs(screen, *, values=True):
    groups = []
    for node in screen.get("unaddressable_targets", []):
        fields = {key: value for key, value in node.items() if key != "ref_id" and (values or key != "value")}
        existing = next((row for row in groups if row["control"] == fields), None)
        if existing is None:
            groups.append({"control": fields, "count": 1})
        else:
            existing["count"] += 1
    return groups
