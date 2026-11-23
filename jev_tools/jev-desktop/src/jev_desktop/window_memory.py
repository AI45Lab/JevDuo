"""Past native observations, without executable refs or inferred completion."""

import copy


def closed_window(scope, screen, action=None, *, values=True):
    if not scope.window_id or screen.get("window_id") != scope.window_id:
        return None
    if scope.app and screen.get("app") != scope.app:
        return None
    context = screen.get("context", [])
    visible = []
    for entry in context[:200]:
        secure = "secure" in entry.get("states", []) or entry.get("secure") is True
        visible.append(
            {
                key: copy.deepcopy(value)
                for key, value in entry.items()
                if key in {"role", "name", "description", "states", "bounds"}
                or key == "value"
                and values
                and not secure
            }
        )
    return {
        **{
            key: copy.deepcopy(screen.get(key))
            for key in ("app", "window", "window_id", "surface", "complete")
        },
        "confirmed_absent": "WINDOW_NOT_FOUND",
        "observation_timing": "last observed before confirmed closure",
        "visible_context": visible,
        "context_truncated": len(context) > 200,
        "closing_action": (
            None
            if action is None
            else {
                key: copy.deepcopy(action[key])
                for key in ("step", "operation", "target", "ok", "delivery")
                if key in action
            }
        ),
    }


def attach(request, windows):
    if not windows:
        return
    request["state"]["closed_window_observations"] = copy.deepcopy(windows)
    request["state"]["closed_window_observation_rules"] = (
        "Closed-window observations are past native readings, not current controls. "
        "Use their values with the recorded closing action and current state to judge progress; "
        "closure alone does not prove that changes were committed. Cancellation does not save a draft. "
        "Do not repeat a completed edit merely because its form is now closed."
    )
