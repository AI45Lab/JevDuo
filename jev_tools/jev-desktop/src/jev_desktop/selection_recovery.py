"""Reobserve a completed selection whose postcondition failed after a UI change."""

from .screen import describe, fingerprint, target_identity


def needs_selection_observation(outcome, operation):
    error = outcome.get("error") or {}
    return (
        operation in {"SELECT", "SELECT_ITEM"}
        and error.get("code") == "ACTION_FAILED"
        and (error.get("details") or {}).get("cause") == "write_ignored"
        and outcome.get("delivery") == "delivered_unverified"
        and error.get("disposition", {}).get("delivery") == "delivered_unverified"
    )


def needs_value_observation(outcome, operation):
    error = outcome.get("error") or {}
    details = error.get("details") or {}
    return (
        operation in {"TYPE_TEXT", "INSERT_TEXT", "CLEAR"}
        and error.get("code") == "ACTION_FAILED"
        and details.get("kind") == "post_action_verification"
        and details.get("cause") == "commit_unconfirmed"
        and outcome.get("delivery") == "delivered_unverified"
        and error.get("disposition", {}).get("delivery") == "delivered_unverified"
    )


def needs_destroyed_target_observation(outcome, operation):
    error = outcome.get("error") or {}
    details = error.get("details") or {}
    action = details.get("after_action") or {}
    return (
        operation in {"CHECK", "UNCHECK", "SELECT", "SELECT_ITEM", "CLICK", "ACTIVATE_OPTION"}
        and error.get("code") == "STALE_REF"
        and details.get("kind") == "post_action_verification"
        and action.get("disposition", {}).get("delivery") == "delivered_unverified"
        and outcome.get("delivery") == "delivered_unverified"
        and error.get("disposition", {}).get("delivery") == "delivered_unverified"
    )


def same_complete_scope(before, after):
    previous, current = before.screen, after.screen
    return (
        bool(previous.get("app") and previous.get("window_id"))
        and previous.get("complete") is True
        and current.get("complete") is True
        and all(current.get(key) == previous.get(key) for key in ("app", "window_id", "window", "root", "surface"))
        and not previous.get("unclassified_surfaces")
        and not current.get("unclassified_surfaces")
    )


def changed_selection_context(before, after):
    if same_complete_scope(before, after) and fingerprint(before.nodes) != fingerprint(after.nodes):
        return {
            "window_id": after.screen["window_id"],
            "observation": "The complete UI changed after the acknowledged operation. This does not prove the requested postcondition or goal completion. Inspect the new state; do not replay the original operation.",
        }
    return None


def input_changed(before, after):
    previous, current = before.screen, after.screen
    if (
        not previous.get("app")
        or not previous.get("window_id")
        or previous.get("complete") is not True
        or current.get("complete") is not True
        or any(
            current.get(key) != previous.get(key) for key in ("app", "window_id", "window", "root", "surface")
        )
        or previous.get("unclassified_surfaces")
        or current.get("unclassified_surfaces")
    ):
        return None
    changes = []
    for old in before.nodes:
        if old.get("role") not in {"textfield", "combobox", "incrementor", "slider"} or not isinstance(old.get("value"), str):
            continue
        if old.get("ref_actionable") is False or {
            "readonly",
            "secure",
            "disabled",
            "hidden",
            "offscreen",
        }.intersection(old.get("states", [])):
            continue
        if not {"SetValue", "TypeText"}.intersection(
            old.get("available_actions", [])
        ) and "editable" not in old.get("states", []):
            continue
        matches = [node for node in after.nodes if target_identity(node) == target_identity(old)]
        prior = [node for node in before.nodes if target_identity(node) == target_identity(old)]
        if len(matches) != 1 or len(prior) != 1:
            continue
        new = matches[0]
        if new.get("ref_actionable") is False or {
            "readonly",
            "secure",
            "disabled",
            "hidden",
            "offscreen",
        }.intersection(new.get("states", [])):
            continue
        if not {"SetValue", "TypeText"}.intersection(
            new.get("available_actions", [])
        ) and "editable" not in new.get("states", []):
            continue
        if isinstance(new.get("value"), str) and new["value"] != old["value"]:
            changes.append(
                {"field": describe(new), "previous_value": old["value"], "current_value": new["value"]}
            )
    return {"window_id": current["window_id"], "input_changes": changes} if changes else None
