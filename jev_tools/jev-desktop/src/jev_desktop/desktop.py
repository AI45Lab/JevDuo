"""agent-desktop backend with explicit physical input and cursor cleanup."""

import time
from contextlib import contextmanager

from .contracts import DesktopError, Scope
from .observation import observe
from .transport import DesktopConfig, LocalTransport

CATALOG = """import json
from gi.repository import Gio
apps = [{"id": a.get_id(), "name": a.get_display_name(), "description": a.get_description(),
         "commandline": a.get_commandline()}
        for a in Gio.AppInfo.get_all() if a.should_show() and a.get_id()]
print(json.dumps({"ok": True, "data": {"apps": apps}}))"""


def delivery_evidence(result):
    data, error = result.get("data", {}), result.get("error")
    details = data.get("details", (error or {}).get("details"))
    after = (error or {}).get("details", {}).get("after_action", {})
    return {
        "ok": result["ok"],
        "delivery": data.get("disposition", (error or {}).get("disposition", {})).get("delivery"),
        "error": error,
        "steps": data.get("steps", after.get("steps", [])),
        "post_state": data.get("post_state", (details or {}).get("post_state", after.get("post_state"))),
        "details": details,
    }


class AgentDesktop:
    def __init__(self, config=None, transport=None, *, sleep=time.sleep, clock=time.monotonic):
        self.config = config or DesktopConfig()
        self.transport = transport or LocalTransport()
        self.sleep, self.clock = sleep, clock
        self.session, self.agent_id = self.config.session, self.config.agent_id
        self._active = False

    def cli(self, *argv):
        prefix = ["--session", self.session] if self.session else []
        prefix += ["--agent-id", self.agent_id] if self.agent_id else []
        timeout = max(
            30, self.config.snapshot_timeout_ms / 1000 + 5,
            (self.config.action_timeout_ms or 0) / 1000 + 5,
        )
        if argv and argv[0] == "snapshot":
            for index, arg in enumerate(argv[1:], 1):
                value = None
                if arg == "--timeout-ms" and index + 1 < len(argv):
                    value = argv[index + 1]
                elif arg.startswith("--timeout-ms="):
                    value = arg.partition("=")[2]
                if value is not None:
                    try:
                        milliseconds = int(value)
                        if milliseconds > 0:
                            timeout = max(timeout, milliseconds / 1000 + 5)
                    except (TypeError, ValueError, OverflowError):
                        pass
        return self.transport.invoke(self.config.binary, [*prefix, *argv], timeout)

    @contextmanager
    def run_context(self, goal, cursor):
        if self._active:
            raise RuntimeError("use a separate AgentDesktop instance for each concurrent run")
        self._active = True
        previous = self.session, self.agent_id
        started = False
        try:
            if cursor:
                session = self.cli("session", "start", "--name", "jev-desktop", "--cursor", "--multi-agent")
                if session["ok"]:
                    self.session = session["data"]["session_id"]
                    self.agent_id = self.agent_id or "jev-desktop"
                    started = True
                    self.cli("cursor-overlay", "enable", "--label", goal[:60])
            yield
        finally:
            try:
                if started:
                    self.cli("cursor-overlay", "disable")
            finally:
                self.session, self.agent_id = previous
                self._active = False

    def observe(self, scope):
        seen = observe(self.cli, scope, self.config.snapshot_timeout_ms, sleep=self.sleep, clock=self.clock)
        if self.config.headed:
            seen.screen["headed_text"] = True
            if self.config.clipboard_text:
                seen.screen["clipboard_text"] = True
        return seen

    def owned_dialog(self, scope):
        if not scope.window_id:
            return None
        result = self.cli(
            "list-windows",
            "--owned-by",
            scope.window_id,
            "--timeout-ms",
            str(self.config.snapshot_timeout_ms),
            *(["--app", scope.app] if scope.app else []),
        )
        if not result.get("ok"):
            code = result.get("error", {}).get("code")
            if code in {"WINDOW_NOT_FOUND", "APP_NOT_FOUND", "PLATFORM_NOT_SUPPORTED"}:
                return None
            raise DesktopError("Native dialog ownership could not be established", code)
        windows = result.get("data")
        if not isinstance(windows, list) or len(windows) > 1:
            raise DesktopError("Native dialog ownership is ambiguous", "AMBIGUOUS_WINDOW")
        if not windows:
            return None
        window = windows[0]
        if not window.get("id") or not window.get("app_name") or window["id"] == scope.window_id:
            raise DesktopError("Native dialog ownership did not name a distinct window")
        return Scope(app=window["app_name"], window_id=window["id"])

    def application_windows(self, scope):
        from .window_navigation import application_windows

        return application_windows(self.cli, scope)

    def _ref_timeout(self):
        value = self.config.action_timeout_ms
        return ["--timeout-ms", str(value)] if value is not None else []

    def _enter_text(self, node, text, command="set-value"):
        argv = (["--headed"] if self.config.headed and command != "select" else []) + [
            command,
            node["ref_id"],
            text,
        ] + self._ref_timeout()
        result = self.cli(*argv)
        error = result.get("error") or {}
        if (
            command == "select"
            and self.config.headed
            and not result.get("ok")
            and error.get("details", {}).get("kind") == "pointer_required"
            and error.get("disposition", {}) == {"delivery": "not_delivered", "retry": "safe"}
        ):
            semantic = result
            argv = ["--headed", *argv]
            result = {**self.cli(*argv), "semantic_attempt": semantic}
        return result, argv

    def execute(self, scope, operation, node, text):
        if not self._active:
            raise RuntimeError("execute requires run_context for resource cleanup")
        if operation == "WAIT":
            self.sleep(0.25)
            return {"ok": True, "delivery": "waited"}
        if operation in ("DRILL", "WIDEN"):
            return {
                "ok": True,
                "delivery": "looked",
                "root": node["ref_id"] if operation == "DRILL" else None,
            }
        if operation.startswith("OPEN_RESOURCE:"):
            from .contracts import RunOptions

            if (
                not self.config.headed or not scope.app or not scope.window_id
                or (node or {}).get("role") != "window" or node.get("window_id") != scope.window_id
            ):
                raise DesktopError("Opening a resource requires a caller-bound window and application")
            path = node.get("resources", {}).get(operation.partition(":")[2])
            RunOptions(resources=(path,))  # Validate the bound literal again at execution.
            argv = ["launch", scope.app, "--arg=" + path, "--timeout", "5000"]
            result = self.cli(*argv)
            return {**delivery_evidence(result), "argv": argv, "route": "caller_bound_resource",
                    "details": {"native_report": result, "verification_scope": "launch_request",
                                "application_commit": "not_verified", "resource": path}}
        if operation in ("TYPE_TEXT", "INSERT_TEXT", "SELECT", "SELECT_ITEM"):
            command = {
                "TYPE_TEXT": "set-value",
                "INSERT_TEXT": "type",
                "SELECT": "select",
                "SELECT_ITEM": "select",
            }[operation]
            container = node.get("selection_container") if operation == "SELECT_ITEM" else None
            receiver = container if container else node
            value = container["label"] if container else text
            result, argv = self._enter_text(receiver, value, command)
            if operation == "TYPE_TEXT" and self.config.clipboard_text and self.config.headed:
                from .clipboard_text import replace_after_refusal

                replacement = replace_after_refusal(self, scope, node, text, result)
                if replacement is not None:
                    replacement["argv"] = argv
                    return replacement
            return {
                **delivery_evidence(result),
                "route": result.get("data", {}).get("details", {}).get("route", command),
                "argv": argv,
                **({"semantic_attempt": result["semantic_attempt"]} if "semantic_attempt" in result else {}),
            }
        commands = {
            "CLICK": "click",
            "POINTER_CLICK": "click",
            "ACTIVATE_OPTION": "click",
            "CHECK": "check",
            "UNCHECK": "uncheck",
            "EXPAND": "expand",
            "COLLAPSE": "collapse",
            "SCROLL": "scroll",
            "SCROLL_UP": "scroll",
            "SCROLL_LEFT": "scroll",
            "SCROLL_RIGHT": "scroll",
            "SCROLL_TO": "scroll-to",
            "FOCUS": "focus",
            "CLEAR": "set-value",
            "RIGHT_CLICK": "right-click",
            "DOUBLE_CLICK": "double-click",
            "TRIPLE_CLICK": "triple-click",
            "HOVER": "hover",
        }
        prefix = ["--headed"] if self.config.headed and operation not in ("CLICK", "ACTIVATE_OPTION") else []
        if operation.startswith("PRESS_KEY:") or operation in {"FOCUS_WINDOW", "MAXIMIZE_WINDOW"}:
            if not self.config.headed or not scope.window_id:
                raise DesktopError("window actions require headed permission and a pinned window")
            argv = (
                ["press", operation.partition(":")[2]] if operation.startswith("PRESS_KEY:")
                else ["focus-window" if operation == "FOCUS_WINDOW" else "maximize"]
            ) + ["--window-id", scope.window_id]
            if scope.app and operation.startswith("PRESS_KEY:"):
                argv += ["--app", scope.app]
        elif operation.startswith("DRAG_TO:"):
            destination = node["drag_targets"][operation.partition(":")[2]]
            argv = ["drag", "--from", node["ref_id"], "--to", destination]
        else:
            argv = [commands[operation], node["ref_id"]]
            if operation == "CLEAR":
                argv.append("")
            if operation in ("SCROLL", "SCROLL_UP", "SCROLL_LEFT", "SCROLL_RIGHT"):
                argv += ["--direction", "down" if operation == "SCROLL" else operation[7:].lower()]
        if operation not in {"FOCUS_WINDOW", "MAXIMIZE_WINDOW"} and not operation.startswith("PRESS_KEY:"):
            argv += self._ref_timeout()
        argv = prefix + argv
        result = self.cli(*argv)
        outcome = {**delivery_evidence(result), "argv": argv}
        if operation == "FOCUS_WINDOW" and result.get("ok"):
            focused = result.get("data", {}).get("focused")
            if isinstance(focused, dict) and focused.get("id") == scope.window_id and focused.get("is_focused") is True:
                outcome["focus_evidence"] = {"window_id": scope.window_id, "is_focused": True}
        return outcome

    def applications(self):
        status = self.cli("status")
        if not status["ok"] or status.get("data", {}).get("platform") != "linux":
            raise DesktopError("Automatic application discovery requires a Linux desktop session")
        inventory = self.cli("list-apps")
        if not inventory["ok"]:
            raise DesktopError("Application inventory failed")
        catalog = self.transport.invoke("python3", ["-c", CATALOG], 30)
        apps = catalog.get("data", {}).get("apps")
        if not catalog["ok"] or not isinstance(apps, list) or not apps:
            raise DesktopError("Installed applications could not be discovered through Gio; check python3-gi")
        return apps

    def launch(self, application):
        argv = ["launch", application["id"], "--timeout", "5000"]
        result = self.cli(*argv)
        return {**delivery_evidence(result), "argv": argv, "window": result.get("data", {}).get("window")}

    def wait_for_window(self, launched):
        result = (
            {"ok": True, "data": {"window": launched["window"]}}
            if launched.get("window")
            else self.cli("wait", "--window", "", "--timeout", "20000")
        )
        window = result.get("data", {}).get("window", {})
        if not result["ok"] or not window.get("app_name") or not window.get("id"):
            raise DesktopError("the launched application has no observable window", "WINDOW_NOT_FOUND")
        return window
