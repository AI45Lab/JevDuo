"""Release temporary refusals only after the blocking dialog has closed."""

from dataclasses import replace


def mark_modal_refusal(blocked, outcome):
    error = outcome.get("error") or {}
    if (
        error.get("disposition") == {"delivery": "not_delivered", "retry": "safe"}
        and error.get("details", {}).get("cause") == "blocked_by_modal_dialog"
    ):
        return {**blocked, "native_modal_refusal": True}
    return blocked


def release_modal_refusals(options, scope):
    return replace(
        options,
        blocked_actions=tuple(
            item
            for item in options.blocked_actions
            if not (item.get("native_modal_refusal") is True and item.get("window_id") == scope.window_id)
        ),
    )
