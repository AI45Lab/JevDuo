"""agent-desktop backend with explicit physical input and cursor cleanup."""

import time
from contextlib import contextmanager

from .contracts import DesktopError
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
        timeout = max(30, self.config.snapshot_timeout_ms / 1000 + 5)
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
        seen = observe(self.cli, scope, self.config.snapshot_timeout_ms)
        if self.config.headed:
            seen.screen["headed_text"] = True
        return seen

    def _enter_text(self, node, text, command="set-value"):
        argv = (["--headed"] if self.config.headed else []) + [command, node["ref_id"], text]
        return self.cli(*argv), argv

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
        if operation in ("TYPE_TEXT", "INSERT_TEXT", "SELECT"):
            command = {"TYPE_TEXT": "set-value", "INSERT_TEXT": "type", "SELECT": "select"}[operation]
            result, argv = self._enter_text(node, text, command)
            return {
                **delivery_evidence(result),
                "route": result.get("data", {}).get("details", {}).get("route", command),
                "argv": argv,
            }
        commands = {
            "CLICK": "click",
            "POINTER_CLICK": "click",
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
        prefix = ["--headed"] if self.config.headed and operation != "CLICK" else []
        if operation.startswith("PRESS_KEY:") or operation == "FOCUS_WINDOW":
            if not self.config.headed or not scope.window_id:
                raise DesktopError("window actions require headed permission and a pinned window")
            argv = (
                ["press", operation.partition(":")[2]] if operation != "FOCUS_WINDOW" else ["focus-window"]
            ) + ["--window-id", scope.window_id]
            if scope.app and operation != "FOCUS_WINDOW":
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
        argv = prefix + argv
        return {**delivery_evidence(self.cli(*argv)), "argv": argv}

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
