"""Scoped accessibility observations over an injected command callable."""

from .contracts import DesktopError, Observation
from .screen import collect, offerable, outside_window, overlay_role


def unreadable(what, result):
    code = result.get("error", {}).get("code")
    return DesktopError(f"{what} could not be read: {code}", code)


def observe(cli, scope, timeout_ms, *, recover=True):
    args = (["--app", scope.app] if scope.app else []) + (
        ["--window-id", scope.window_id] if scope.window_id else []
    )
    base = ["snapshot", *args, "--compact", "--include-bounds", "--timeout-ms", str(timeout_ms)]
    snap = cli(*base, *(["--root", scope.root] if scope.root else []))
    if (
        not snap["ok"]
        and scope.window_id
        and scope.app
        and snap.get("error", {}).get("code") == "APP_NOT_FOUND"
    ):
        from dataclasses import replace

        return observe(cli, replace(scope, app=None), timeout_ms, recover=recover)
    if not snap["ok"] and scope.root:
        raise unreadable("that region", snap)
    if snap["ok"] and scope.root and scope.window_id and outside_window(snap["data"], scope.window_id):
        raise DesktopError("that region is outside the pinned window", "ROOT_OUTSIDE_WINDOW")
    if not snap["ok"] and snap.get("error", {}).get("code") != "WINDOW_NOT_FOUND":
        snap = cli(*base, "--max-depth", "4")
    if not snap["ok"]:
        raise unreadable("the screen", snap)
    if scope.window_id and outside_window(snap["data"], scope.window_id):
        raise DesktopError("the observation is outside the pinned window", "SURFACE_OUTSIDE_WINDOW")
    surface = None if scope.root else overlay_role(snap["data"]["tree"])
    unclassified = []
    if not scope.root and (not surface or (surface == "menu" and snap["data"]["tree"]["role"] != "menu")):
        app = snap["data"].get("app") or scope.app
        inventory = cli("list-surfaces", *(["--app", app] if app else []))
        if not inventory["ok"]:
            raise unreadable("the surfaces", inventory)
        surfaces = inventory.get("data", {}).get("surfaces", [])
        unclassified = [entry for item in surfaces for entry in item.get("unclassified", [])]
        surface = (
            "menu"
            if any(
                item.get("type") == "menu" and (not scope.window_id or item.get("id") == scope.window_id)
                for item in surfaces
            )
            else None
        )
    if surface:
        scoped = cli(*base, "--surface", surface)
        if scoped["ok"]:
            if scope.window_id and outside_window(scoped["data"], scope.window_id):
                raise DesktopError("the surface is outside the pinned window", "SURFACE_OUTSIDE_WINDOW")
            snap = scoped
        elif scoped.get("error", {}).get("code") == "ELEMENT_NOT_FOUND":
            surface = None
        else:
            raise unreadable(f"the {surface}", scoped)
    data = snap["data"]
    if recover and timeout_ms < 60000 and (data.get("identity_unproven") or data.get("complete") is False):
        return observe(cli, scope, min(timeout_ms * 2, 60000), recover=False)
    if data.get("identity_unproven"):
        raise DesktopError("the screen identity is unproven", "INCOMPLETE_OBSERVATION")
    context = []

    def walk(node):
        if {"hidden", "offscreen"}.intersection(node.get("states", [])):
            return
        if node.get("name") or node.get("value") is not None:
            context.append(
                {
                    key: node[key]
                    for key in ("role", "name", "description", "value", "states", "bounds")
                    if key in node
                }
            )
        for child in node.get("children", []):
            walk(child)

    walk(data["tree"])
    nodes = [n for n in offerable(collect(data["tree"])) if "offscreen" not in n.get("states", [])]
    return Observation(
        nodes,
        {
            "app": data.get("app") or scope.app,
            "window": data.get("window", {}).get("title"),
            "window_id": data.get("window", {}).get("id") or scope.window_id,
            "surface": surface or "window",
            "unclassified_surfaces": unclassified,
            "complete": data.get("complete"),
            "context": context,
            "root": scope.root,
            "snapshot_timeout_ms": timeout_ms,
            "unaddressable_targets": [
                {key: n[key] for key in ("role", "name", "ref_id", "ref_failure") if key in n}
                for n in collect(data["tree"])
                if n.get("ref_actionable") is False
            ],
        },
    )
