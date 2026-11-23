"""Opt-in whole-field replacement after a positively undelivered native refusal.

The clipboard proves the full selection before replacement. Every key is pinned
to the owning window. No insertion fallback, secure fields, unknown writes,
clipboard restoration, or application-specific routes are attempted.
"""

import unicodedata

from .contracts import DesktopError

REFUSALS = {"INVALID_ARGS", "ACTION_NOT_SUPPORTED", "PLATFORM_NOT_SUPPORTED"}
FORBIDDEN = {"secure", "readonly", "disabled", "hidden", "offscreen", "indeterminate"}


def identity(node):
    # Values and geometry do not establish identity. Native refs validate live identity again.
    return {k: node.get(k) for k in ("role", "name", "description", "native_id")}


def replace_after_refusal(desktop, scope, target, text, native):
    error = native.get("error") or {}
    if (
        native.get("ok") is not False
        or error.get("code") not in REFUSALS
        or error.get("disposition") != {"delivery": "not_delivered", "retry": "safe"}
        or not scope.app or not scope.window_id or scope.root
        or not isinstance(text, str) or not text
        or any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text)
        or target.get("role") != "textfield"
        or "SetFocus" not in target.get("available_actions", [])
        or FORBIDDEN.intersection(target.get("states", []))
        or target.get("ref_actionable") is False
        or not isinstance(target.get("value"), str)
    ):
        return None
    old = target["value"]
    receipts = []
    pasted = False

    def call(*argv):
        result = desktop.cli(*argv)
        receipts.append({"argv": list(argv), "result": result})
        if not result.get("ok"):
            raise DesktopError("Clipboard text preparation or dispatch failed")
        return result

    def observe(expected, focused=False):
        seen = desktop.observe(scope)
        screen = seen.screen
        if (
            screen.get("complete") is not True
            or screen.get("app") != scope.app
            or screen.get("window_id") != scope.window_id
            or screen.get("root") or screen.get("surface") != "window"
            or screen.get("unclassified_surfaces")
        ):
            raise DesktopError("Clipboard text requires a complete owned window")
        matches = [n for n in seen.nodes if identity(n) == identity(target)]
        if len(matches) != 1:
            raise DesktopError("Clipboard text target identity changed or is ambiguous")
        node = matches[0]
        if (
            node.get("ref_actionable") is False
            or FORBIDDEN.intersection(node.get("states", []))
            or node.get("value") != expected
        ):
            raise DesktopError("Clipboard text target or value changed")
        if focused:
            focus = [n for n in screen.get("context", []) if "focused" in n.get("states", [])]
            if (
                "focused" not in node.get("states", []) or len(focus) != 1
                or any(focus[0].get(k) != node.get(k) for k in ("role", "name", "value"))
            ):
                raise DesktopError("Clipboard text focus is not uniquely proven")
        return node

    def key(combo):
        return call("--headed", "press", combo, "--window-id", scope.window_id, "--app", scope.app)

    def clipboard(expected):
        data = call("clipboard-get", "--format", "text").get("data", {})
        if data.get("type") != "text" or data.get("text") != expected:
            raise DesktopError("Clipboard text content was not confirmed")

    try:
        node = observe(old)
        call("--headed", "focus", node["ref_id"], *desktop._ref_timeout())
        observe(old, focused=True)
        if old:
            call("clipboard-clear")
            cleared = call("clipboard-get", "--format", "text").get("data", {})
            if cleared.get("type") != "text" or not (
                cleared.get("found") is False or cleared.get("text") == ""
            ):
                raise DesktopError("Clipboard clear was not confirmed")
            key("ctrl+a")
            observe(old, focused=True)
            key("ctrl+c")
            clipboard(old)
            observe(old, focused=True)
        # An explicitly empty, focused field has no old content to select.
        call("clipboard-set", "--", text)
        clipboard(text)
        observe(old, focused=True)
        # From this point the field write may have occurred, even if its reply is lost.
        pasted = True
        key("ctrl+v")
        observe(text, focused=True)
    except DesktopError as exc:
        delivery = "delivery_uncertain" if pasted else "not_delivered"
        return {
            "ok": False, "delivery": delivery, "route": "clipboard_replace",
            "error": {
                "code": "ACTION_FAILED", "message": str(exc),
                "disposition": {"delivery": delivery, "retry": "unsafe"},
            },
            "details": {"native_attempt": native, "receipts": receipts,
                        "preparation_may_have_changed_focus_selection_or_clipboard": True},
        }
    return {
        "ok": True, "delivery": "delivered_verified", "route": "clipboard_replace",
        "post_state": {"value": text},
        "details": {"native_attempt": native, "receipts": receipts,
                    "verification_scope": "element_value", "application_commit": "not_verified",
                    "selection_proof": "empty_field" if not old else "fresh_copy_equals_complete_original_value"},
    }
