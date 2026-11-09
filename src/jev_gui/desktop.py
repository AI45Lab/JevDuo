"""Task-VM observation and input ports; no VM lifecycle or official scoring."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from .browser import VM_SCRIPT
from .executor import LocalFrame
from .semantics import _bounds, enrich_targets
from .targets import A11yTargetBuilder, _local, _state

CAPTURE_SCRIPT = """
import base64, io, json
import pyautogui
from Xlib.display import Display
d = Display()
try:
    root = d.screen().root
    active = root.get_full_property(d.intern_atom('_NET_ACTIVE_WINDOW'), 0)
    wid = int(active.value[0]) if active is not None and len(active.value) else 0
    window = d.create_resource_object('window', wid) if wid else None
    title = window.get_wm_name() if window else ''
    image = pyautogui.screenshot()
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    print(json.dumps({'image': base64.b64encode(buffer.getvalue()).decode(),
        'title': title, 'surface': str(wid) + ':' + str(title),
        'width': image.width, 'height': image.height}))
finally:
    d.close()
"""

CLIPBOARD_SCRIPT = """
import json, shutil, subprocess, time
import pyautogui
payload = json.loads({serialized!r})
if shutil.which('xclip'):
    command = ['xclip', '-selection', 'clipboard']
elif shutil.which('xsel'):
    command = ['xsel', '--clipboard', '--input']
else:
    raise RuntimeError('GUI text input requires xclip or xsel in the task VM')
# Clipboard owners fork; inherited output pipes must not keep communicate() waiting.
subprocess.run(command, input=payload['text'], text=True, check=True, timeout=5,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.05)
pyautogui.PAUSE = 0
pyautogui.hotkey(*(['ctrl', 'shift', 'v'] if payload['target_kind'] == 'terminal' else ['ctrl', 'v']))
"""


class ActionLimit(BaseException):
    """An episode budget boundary, never an ordinary tool error to retry."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ActionBudget:
    """One ledger shared by every GUI primitive and independent CLI command."""

    def __init__(self, limit, deadline):
        self.limit, self.deadline = limit, deadline
        self.gui_actions = self.cli_actions = 0

    @property
    def actions(self):
        return self.gui_actions + self.cli_actions

    def check(self):
        if time.time() >= self.deadline:
            raise ActionLimit("wall_time_limit")
        if self.actions >= self.limit:
            raise ActionLimit("max_steps")

    def reserve(self, route):
        self.check()
        if route == "cli":
            self.cli_actions += 1
        else:
            self.gui_actions += 1


class DesktopPort:
    """Use NorthCUA's native backend with receipt transport inside the task VM."""

    def __init__(self, backend, output, emit, budget):
        self.backend, self.emit, self.budget = backend, emit, budget
        self.directory = Path(output) / "local_gui"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.frame = None
        self.failed_actions = {}
        self.input_history = {}

    async def observe(self, reactive=False):
        started = time.monotonic()
        tree, browser, errors = "", None, []
        if not reactive:
            try:
                tree = self.backend._request("GET", "/accessibility", timeout=20).json()["AT"] or ""
            except Exception as error:
                errors.append("a11y:" + type(error).__name__)
            try:
                result = self.backend._run_python(VM_SCRIPT, timeout=20)
                browser = json.loads(result.get("output") or "{}").get("snapshot")
            except Exception as error:
                errors.append("cdp:" + type(error).__name__)
        captured_at = time.monotonic()
        capture = self.backend._run_python(CAPTURE_SCRIPT, timeout=10)
        payload = json.loads(capture["output"])
        image = base64.b64decode(payload["image"])
        self.count += 1
        path = self.directory / f"frame_{self.count:05}.png"
        path.write_bytes(image)
        targets = []
        if tree:
            root = ET.fromstring(tree)
            root.set("foreground_title", payload["title"] or "")
            tree = ET.tostring(root, encoding="unicode")
            targets = A11yTargetBuilder().build(
                tree, f"local{self.count}", max_targets=2048,
                infer_button_labels=True, include_sliders=True, filter_occluded=True,
            )
        if not reactive:
            targets = enrich_targets(targets, tree, (payload["width"], payload["height"]), browser)
            targets = observed_capabilities(targets, tree)
            (self.directory / f"frame_{self.count:05}.json").write_text(json.dumps(
                {"targets": [t.compact() for t in targets], "browser": browser, "errors": errors},
                ensure_ascii=False), encoding="utf-8")
            (self.directory / f"frame_{self.count:05}.xml").write_text(tree, encoding="utf-8")
        self.frame = LocalFrame(
            image, str(path), captured_at, payload["width"], payload["height"], targets, payload["surface"])
        self.emit({"route": "local_observation", "screenshot": str(path), "reactive": reactive,
                   "seconds": time.monotonic() - started, "target_count": len(targets), "errors": errors})
        return self.frame

    async def perform(self, operation, parameters, reactive=False):
        """Called only with a JEV winner; acknowledge input before any later observation."""
        from northcua.runtime.mcp.desktop_native.backend import DesktopNativeError, DesktopNativePoint
        from northcua_runtime_shared.desktop_surface.models import TextTargetKind

        def point(values):
            x, y = values["x"], values["y"]
            if not (0 <= x <= 1000 and 0 <= y <= 1000):
                raise ValueError("GUI coordinates must be normalized to the full screenshot")
            return round(x / 1000 * (self.frame.width - 1)), round(y / 1000 * (self.frame.height - 1))

        self.budget.reserve("gui")
        event = {"route": "action", "owner": "jev", "step": self.budget.actions,
                 "operation": operation, "parameters": parameters,
                 "screenshot_before": self.frame.path, "time": time.time()}
        self.emit(event)
        started = time.monotonic()
        try:
            if operation in {"click", "double_click", "move"}:
                x, y = point(parameters)
                extra = {"button": parameters["button"]} if "button" in parameters else {}
                getattr(self.backend, operation)(x, y, **extra)
            elif operation == "drag":
                path = [DesktopNativePoint(x=x, y=y) for x, y in map(point, parameters["path"])]
                self.backend.drag(path, button=parameters.get("button", "left"))
            elif operation == "type":
                self.backend.type_text(parameters["text"], target_kind=TextTargetKind.coerce(
                    parameters.get("target_kind", "editable")))
            elif operation == "keypress":
                self.backend.keypress(**parameters)
            elif operation == "scroll":
                self.backend.scroll(**parameters)
            elif operation == "wait":
                await asyncio.sleep(min(parameters["ms"] / 1000, max(0, self.budget.deadline - time.time())))
            else:
                raise ValueError(f"Unsupported JEV GUI action: {operation}")
        except BaseException as error:
            self.emit({"route": "receipt", "step": self.budget.actions, "owner": "jev",
                       "operation": operation, "action_status": (
                           "failed_with_receipt" if isinstance(error, DesktopNativeError) else "unconfirmed"),
                       "error_type": type(error).__name__, "seconds": time.monotonic() - started})
            raise
        self.emit({"route": "receipt", "step": self.budget.actions, "owner": "jev",
                   "operation": operation, "action_status": "acknowledged",
                   "seconds": time.monotonic() - started})
        if not reactive:
            await asyncio.sleep(min(0.5, max(0, self.budget.deadline - time.time())))
        return {"text": f"{operation} acknowledged; inspect the next observation.",
                "action_status": "acknowledged", "screenshot_policy": "none"}


def observed_capabilities(targets, tree):
    """Retain DOM abilities and observed native actions rather than guessing from text."""
    nodes = {(_local(n.tag), _bounds(n)): n for n in ET.fromstring(tree).iter()} if tree else {}
    result = []
    for target in targets:
        node = nodes.get((target.role, (target.x, target.y, target.width, target.height)))
        operations = set(target.operations)
        if node is not None:
            actions = {_local(k).removesuffix("_desc") for k in node.attrib if _local(k).endswith("_desc")}
            if actions & {"click", "clickancestor", "press", "activate", "showmenu"}:
                operations.add("click")
            if _state(node, "editable") is True:
                operations.add("type_text")
            if "toggle" in actions and target.role in {"checkbox", "check_box", "toggle_button", "switch"}:
                operations.add("toggle")
            if _state(node, "selectable") is True and _state(node, "transient") is not True:
                operations.add("click")
                if target.role in {"icon", "list_item", "tree_item", "table_cell"} and (
                    "nautilus" in target.application.lower() or target.scope == "dialog"):
                    operations.add("double_click")
            if actions & {"expand", "expandorcontract"}:
                operations.add("collapse" if _state(node, "expanded") is True else "expand")
        result.append(replace(target, operations=tuple(sorted(operations))))
    return result
