"""Loopback-only fixtures and a deterministic Chat endpoint for first-run checks.

The model replies are scripted. GUI tools, browser, NexAU, controller and batch
scheduler are real. Responses depend on each request's history, so concurrent
tasks do not share a global turn counter.
"""

from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

import yaml


def tool_call(name: str, arguments: dict, call_id: str | None = None) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id or f"demo-{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def prior_calls(messages: list[dict]) -> list[str]:
    return [
        call["function"]["name"]
        for message in messages
        for call in message.get("tool_calls", [])
        if message.get("role") == "assistant"
    ]


def visible_window_owner(windows, point, *, cursor_layer):
    """Find the input window at a point, ignoring the non-intercepting cursor."""
    for window in windows:
        if window.get("kCGWindowLayer") == cursor_layer:
            continue
        bounds = window.get("kCGWindowBounds", {})
        if window.get("kCGWindowAlpha", 1) and (
            bounds.get("X", 0) <= point["x"] < bounds.get("X", 0) + bounds.get("Width", 0)
            and bounds.get("Y", 0) <= point["y"] < bounds.get("Y", 0) + bounds.get("Height", 0)
        ):
            return window.get("kCGWindowOwnerPID")
    return None


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, directory: Path, port: int, *, desktop: bool = False, delay: float = 0):
        self.directory = directory.resolve()
        self.desktop = desktop
        self.desktop_process = None
        self.delay = delay
        self.marker = "NORTHCUA_CONFIRMED_" + uuid.uuid4().hex[:10]
        self.desktop_text = "NorthCUA desktop " + uuid.uuid4().hex[:8]
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", port), DemoHandler)

    def event(self, event: dict):
        with self.lock:
            with (self.directory / "events.jsonl").open("a") as target:
                target.write(json.dumps({"time": time.time(), **event}) + "\n")

    def focus_desktop(self):
        if not self.desktop:
            raise ValueError("Start demo serve with --desktop for the disposable native form")
        if self.desktop_process is None or self.desktop_process.poll() is not None:
            raise RuntimeError("Disposable form process is no longer running")
        if sys.platform == "darwin":
            import AppKit
            import Quartz

            application = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(self.desktop_process.pid)
            if application is None:
                raise RuntimeError("Disposable form has no native application")
            application.activateWithOptions_(
                AppKit.NSApplicationActivateIgnoringOtherApps | AppKit.NSApplicationActivateAllWindows
            )
            time.sleep(0.2)
        from northcua.runtime.mcp.desktop_native.backend import PyAutoGuiDesktopBackend

        point = json.loads((self.directory / "desktop-ready.json").read_text())
        clicked = self.directory / "desktop-clicked"
        clicked.unlink(missing_ok=True)
        backend = PyAutoGuiDesktopBackend()
        deadline = time.monotonic() + 5
        while not clicked.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Disposable form did not receive input; check desktop permissions")
            if sys.platform == "darwin":
                windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
                owner = visible_window_owner(
                    windows, point, cursor_layer=Quartz.CGWindowLevelForKey(Quartz.kCGCursorWindowLevelKey)
                )
                if owner != self.desktop_process.pid:
                    raise RuntimeError("Disposable form is covered or on another desktop; no input was sent")
            backend.click(point["x"], point["y"])
            time.sleep(0.1)

    def answer(self, request: dict) -> dict:
        if request.get("stream"):
            raise ValueError("The tutorial endpoint requires stream: false")
        messages = request["messages"]
        tools = {item["function"]["name"] for item in request.get("tools", [])}
        calls = prior_calls(messages)
        if "final_report" in tools:
            if "client_browser_snapshot" not in calls:
                return tool_call("client_browser_snapshot", {})
            task_ids = re.findall(
                r"Task ID: ([A-Za-z0-9_-]+)",
                "\n".join(
                    str(message.get("content", "")) for message in messages if message.get("role") in {"user", "system"}
                ),
            )
            task_id = task_ids[0] if task_ids else ""
            expected = re.compile(re.escape(f"{self.marker}/{task_id}") + r"(?![A-Za-z0-9_-])")
            verified = bool(task_id) and any(
                message.get("role") == "tool" and expected.search(str(message.get("content", "")))
                for message in messages
            )
            verdict = "success" if verified else "failure"
            self.event({"kind": "evaluation", "task": task_id, "verified": verified})
            return tool_call(
                "final_report",
                {
                    "overall_verdict": verdict,
                    "process_rating": 5,
                    "outcome_label": verdict,
                    "completion_rating": 5 if verified else 1,
                    "confidence_rating": 5,
                    "controllability": "controllable",
                    "reason": "Inspected the live browser for the fixture receipt.",
                    "page_state_verified": verified,
                    "dom_state_verified": verified,
                },
            )
        if "web_click_point" in tools:
            if "web_note" not in calls:
                return tool_call("web_note", {"key": "plan", "text": "Activate the local form; verify its receipt."})
            if "web_click_point" not in calls:
                return tool_call("web_click_point", {"x": 80, "y": 40})
            return {"role": "assistant", "content": "Activated the local form."}
        if "type" in tools:
            if "note" not in calls:
                self.focus_desktop()
                return tool_call("note", {"key": "plan", "text": "Fill the disposable tutorial form."})
            if "type" not in calls:
                return tool_call("type", {"text": self.desktop_text})
            if "keypress" not in calls:
                return tool_call("keypress", {"key": "enter"})
            receipt = self.directory / "desktop-result.txt"
            if not receipt.is_file() or receipt.read_text() != self.desktop_text:
                raise ValueError(
                    "Native form did not save the expected text; inspect desktop-keys.jsonl and screenshots"
                )
            return {"role": "assistant", "content": "Submitted the disposable form."}
        raise ValueError("This scripted endpoint only supports the bundled Web and Desktop tutorials")


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, *_args):
        pass

    def reply(self, value, *, status=200, content_type="application/json"):
        body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.reply({"status": "ready", "mode": "scripted-local-demo"})
        elif path.startswith("/task/"):
            label = path.rsplit("/", 1)[-1]
            if not label or not all(char.isalnum() or char in "-_" for char in label):
                self.reply({"error": "Invalid fixture id"}, status=400)
                return
            self.reply(
                "<!doctype html><html><title>NorthCUA local task</title><body>"
                '<button style="position:absolute;left:16px;top:16px;width:180px;height:60px" '
                f"onclick=\"fetch('/confirm/{label}',{{method:'POST'}}).then(r=>r.json()).then(v=>this.textContent=v.receipt)\">"
                "Activate</button></body></html>",
                content_type="text/html; charset=utf-8",
            )
        else:
            self.reply({"error": "Not found"}, status=404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/confirm/"):
            task_id = path.rsplit("/", 1)[-1]
            self.server.event({"kind": "web_action", "task": task_id})
            self.reply({"receipt": f"{self.server.marker}/{task_id}"})
            return
        if path != "/v1/chat/completions":
            self.reply({"error": "Not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32 * 1024 * 1024:
                raise ValueError("Invalid request size")
            request = json.loads(self.rfile.read(length))
            time.sleep(self.server.delay)
            message = self.server.answer(request)
            self.server.event(
                {"kind": "model_call", "tools": [c["function"]["name"] for c in message.get("tool_calls", [])]}
            )
            self.reply(
                {
                    "id": "demo",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "scripted-demo",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                }
            )
        except Exception as exc:
            self.server.event({"kind": "error", "error": str(exc)})
            self.reply({"error": {"message": str(exc), "type": "demo_error"}}, status=400)


def write_configuration(server: DemoServer, controller_port: int):
    directory = server.directory
    base_url = f"http://127.0.0.1:{server.server_port}"
    for name in ("agent.example.yaml", "agent.web.example.yaml", "app.example.yaml"):
        data = yaml.safe_load(files("northcua.resources.config").joinpath(name).read_text())
        if "llm_config" in data:
            data["llm_config"].update(
                model="scripted-demo",
                api_key="local-demo",
                base_url=base_url + "/v1",
                stream=False,
                max_retries=0,
                timeout=30,
            )
        else:
            data["artifacts"]["root_dir"] = str(directory / "artifacts")
            data["web"]["post_action_wait_ms"] = 150
            data["evaluation"].update(enabled=False, fail_open=False)
            data["evaluation"]["tools"]["allow_client_live_probe"] = True
            data["prompt"]["screenshot_window_images"] = 2
        (directory / name).write_text(yaml.safe_dump(data, sort_keys=False))
    app = yaml.safe_load((directory / "app.example.yaml").read_text())
    app["evaluation"]["enabled"] = True
    (directory / "app.batch.yaml").write_text(yaml.safe_dump(app, sort_keys=False))
    manifest = {
        "batch_id": "local-web",
        "defaults": {
            "controller_url": f"ws://127.0.0.1:{controller_port}",
            "concurrency": 2,
            "timeout_seconds": 120,
            "max_retries": 1,
            "sandbox": {
                "provider": "local-process",
                "template": "browser",
                "desktop": False,
                "platform": "browser",
                "resources": {"timeout_seconds": 300},
                "metadata": {"controller_db": str(directory / "controller.sqlite")},
            },
        },
        "tasks": [
            {
                "task_id": f"web-{number}",
                "agent_kind": "web",
                "url": f"{base_url}/task/web-{number}",
                "query": f"Task ID: web-{number}. Activate the button and verify the receipt.",
                "max_steps": 6,
            }
            for number in range(1, 4)
        ],
    }
    (directory / "batch.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (directory / "demo.json").write_text(
        json.dumps(
            {
                "mode": "scripted-local-demo",
                "url": base_url,
                "desktop_text": server.desktop_text,
                "marker": server.marker,
                "controller_port": controller_port,
            },
            indent=2,
        )
    )


def serve(directory: Path, *, port: int, controller_port: int, desktop: bool, delay: float) -> int:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Choose an empty demo directory; existing configuration and evidence are never overwritten")
    server = DemoServer(directory, port, desktop=desktop, delay=delay)
    process = None
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        write_configuration(server, controller_port)
        if desktop:
            process = subprocess.Popen([sys.executable, "-m", "northcua.demo.desktop_window", str(directory)])
            server.desktop_process = process
            deadline = time.monotonic() + 15
            while not (directory / "desktop-ready.json").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Disposable form did not open; install Tk and use an interactive desktop")
                time.sleep(0.1)
        print(
            json.dumps(
                {
                    "ready": True,
                    "directory": str(directory),
                    "url": f"http://127.0.0.1:{server.server_port}",
                    "model": "scripted local fixture; no hosted model or cloud used",
                }
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
        if process is not None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    return 0
