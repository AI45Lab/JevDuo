"""Explicit observation navigation between windows of one verified process."""

from dataclasses import replace

from .contracts import DesktopError


def application_windows(cli, scope):
    result = cli("list-windows", *(["--app", scope.app] if scope.app else []))
    if not result.get("ok") or not isinstance(result.get("data"), list):
        raise DesktopError("Application window inventory could not be read")
    windows = result["data"]
    owners = [w for w in windows if w.get("id") == scope.window_id and w.get("app_name") == scope.app]
    if len(owners) != 1:
        return []
    owner = owners[0]
    if not owner.get("pid") or not owner.get("process_instance"):
        return []
    return [
        w
        for w in windows
        if w.get("id")
        and w["id"] != scope.window_id
        and w.get("visible") is True
        and w.get("accessible") is True
        and all(w.get(k) == owner[k] for k in ("app_name", "pid", "process_instance"))
    ]


def extend_windows(space, windows, options):
    if options.allowed_operations is not None and "SWITCH_WINDOW" not in options.allowed_operations:
        return
    for index, window in enumerate(windows):
        target = f"APP_WINDOW_{index}"
        space["elements"].append(
            f'[{target}] other application window "{window.get("title", "")}"'
            + (" (currently focused)" if window.get("is_focused") else "")
        )
        space["by_target"][target] = {
            "node": {
                "role": "window",
                "name": window.get("title", ""),
                "window_identity": window,
                "path": [],
                "states": ["focused"] if window.get("is_focused") else [],
            },
            "operations": ["SWITCH_WINDOW"],
        }


def switch_window(desktop, scope, node):
    wanted = node["window_identity"]
    fields = ("id", "app_name", "pid", "process_instance")
    matches = [
        w for w in desktop.application_windows(scope) if all(w.get(k) == wanted.get(k) for k in fields)
    ]
    if len(matches) != 1:
        return None
    return replace(scope, app=wanted["app_name"], window_id=wanted["id"], root=None)
