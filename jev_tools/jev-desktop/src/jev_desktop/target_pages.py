"""Page observed candidates without changing refs or dispatching GUI actions."""

from .policy import criterion


def add_page_history(request, history):
    page = request["state"].get("candidate_page")
    if not page:
        return
    visited = []
    for index in history:
        if index not in visited:
            visited.append(index)
    page["visited_indices"] = sorted(visited)
    page["recent_indices"] = history[-8:]
    page["instructions"] = (
        "These pages describe the same unchanged observation. Visited pages have already been inspected. "
        "Revisit a page to choose a specific observed candidate, rather than cycling between pages. "
        "All candidates remain available. Window keyboard operations can provide another observed route."
    )
    criteria = request["questions"]["target"]["criteria"]
    for key, index in (("PREVIOUS_PAGE", page["index"] - 1), ("NEXT_PAGE", page["index"] + 1)):
        if key in criteria and index in visited:
            criteria[key] += " That page was already inspected in this unchanged observation."


def target_page(space, page, *, values=True, size=96):
    candidates = [(key, value) for key, value in space["by_target"].items() if key != "WINDOW"]
    candidates.sort(
        key=lambda item: (
            "focused" not in item[1]["node"].get("states", []),
            not (
                "editable" in item[1]["node"].get("states", [])
                or set(item[1]["node"].get("available_actions", [])) - {"Click", "RightClick", "SetFocus"}
            ),
            "SetFocus" not in item[1]["node"].get("available_actions", []),
        )
    )
    count = max(1, (len(candidates) + size - 1) // size)
    if not 0 <= page < count:
        raise ValueError("candidate page is outside the observed action space")
    if count == 1 and len(candidates) == len(space["elements"]):
        return space
    targets = dict(candidates[page * size : (page + 1) * size])
    if "WINDOW" in space["by_target"]:
        targets["WINDOW"] = space["by_target"]["WINDOW"]
    elements = [
        criterion(item["node"], key, values=values)
        + (f" · holds {item['node']['children_count']} more" if item["node"].get("children_count") else "")
        for key, item in targets.items()
        if key != "WINDOW"
    ]
    if count == 1:
        return {**space, "elements": elements}
    return {
        **space,
        "by_target": targets,
        "elements": elements,
        "candidate_page": {
            "index": page,
            "count": count,
            "total_candidates": len(candidates),
            "size": size,
        },
    }
