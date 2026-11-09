"""NorthCUA MCP registration: independent VM CLI, JEV-only desktop input."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .choice import LocalChoicePolicy
from .desktop import CLIPBOARD_SCRIPT, ActionBudget, DesktopPort
from .executor import LocalExecutor

SERVER = "DesktopNativeActionExecutor"
INSTRUCTIONS = """All GUI input MUST use gui_goal; direct click/type/keypress tools are unavailable.
Use terminal for independent task-authorized Bash, computation and file editing INSIDE the task VM.
Never put desktop automation, GUI input or hidden evaluation access inside a terminal command.
You own business reasoning, exact source-backed input values, memory and independent stage verification.
Supply an ordered card of small operations, each with ONE current intent and observable success condition.
Use fill/select for one exact field/value, files for paths in an OPEN file chooser, keys for an exact
keyboard sequence, drag for a grounded path, controls for navigation, reactive for dynamic scenes.
For fill/select use an observed target_hint or target_id, not an acceptance paragraph. For native dates
provide ISO YYYY-MM-DD. If the tree omits a visible control, provide a visual_target in CURRENT full-screen
coordinates normalized to 0..1000. Its label must match the fill target_hint. Refresh proposals after layout
changes. Keep cards bounded but group several known operations rather than replanning after every click.
The action budget counts EVERY GUI primitive and CLI command. A text fill normally needs THREE actions.
Tool acknowledgements, unreadable field values and JEV Done are NOT task success. Inspect results.
On blocked results resolve the specific target/focus/value problem; never blindly repeat the same card.
Preserve source facts and save a deliverable before optional polish. Never read evaluator/setup answers.
"""
_GUI_AUTOMATION = re.compile(r"\b(pyautogui|xdotool|ydotool|pynput|xte|wmctrl)\b", re.I)


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    x: float = Field(ge=0, le=1000)
    y: float = Field(ge=0, le=1000)


class VisualTarget(Point):
    label: str
    operation: Literal["click", "double_click", "right_click", "move"] = "click"


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["controls", "fill", "select", "files", "keys", "drag", "reactive"]
    intent: str = Field(min_length=1, max_length=1200)
    success_condition: str = Field(min_length=1, max_length=800)
    target_hint: str = ""
    target_id: str = ""
    text: str | None = None
    target_kind: Literal["editable", "terminal"] = "editable"
    keys: list[str] = Field(default_factory=list, max_length=100)
    paths: list[str] = Field(default_factory=list, max_length=30)
    drag_path: list[Point] = Field(default_factory=list, max_length=30)
    visual_targets: list[VisualTarget] = Field(default_factory=list, max_length=8)
    allowed_keys: list[str] = Field(default_factory=lambda: ["tab", "shift+tab", "enter", "esc"])
    commit_targets: list[str] = Field(default_factory=list, max_length=12)
    max_actions: int = Field(default=12, ge=1, le=500)

    @model_validator(mode="after")
    def executable(self):
        if self.kind in {"fill", "select"} and (
            self.text is None or not (self.target_hint or self.target_id)
        ):
            raise ValueError("fill/select require an observed target and exact text")
        if self.kind == "keys" and not self.keys:
            raise ValueError("keys requires an exact non-empty key sequence")
        if self.kind == "files" and not self.paths:
            raise ValueError("files requires exact VM paths and an already open chooser")
        if self.kind == "drag" and len(self.drag_path) < 2:
            raise ValueError("drag requires at least two grounded points")
        return self


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=2400)
    operations: list[Operation] = Field(min_length=1, max_length=12)
    max_actions: int = Field(default=64, ge=1, le=500)


class Terminal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1)
    cwd: str = "/home/user"
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class LocalTools:
    """One actor-local component; physical primitives never enter the model's tool surface."""

    def __init__(self, config, output, max_actions, deadline):
        self.config, self.output = config, Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.budget = ActionBudget(max_actions, deadline)
        self.port = None
        self.original = None
        fast = config["fast_model"]
        self.selector = LocalChoicePolicy(
            fast.get("endpoint_file", ""), fast.get("timeout_seconds", 30), endpoint=fast.get("endpoint", ""))
        self.choice_calls = 0
        self.choice_seconds = 0.0
        self.logical_steps = 0

    def emit(self, event):
        if event.get("route") == "gui":
            self.choice_calls += 1
            self.choice_seconds += event["ranking"]["latency_ms"] / 1000
        with (self.output / "jev_actions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"time": time.time(), **event}, ensure_ascii=False) + "\n")

    def bind(self):
        """Construct the backend after NorthCUA applies the app's OSWorld environment."""
        if self.port is not None:
            return self.port
        from northcua.runtime.mcp.desktop_native.osworld_backend import OSWorldDesktopBackend
        from northcua_runtime_shared.desktop_surface.models import TextTargetKind

        from .receipts import run_python

        owner = self

        class ReceiptBackend(OSWorldDesktopBackend):
            def _run_python(self, script, *, timeout):
                return run_python(self, script, timeout=timeout,
                                  wall_deadline=owner.budget.deadline, emit=owner.emit)

            def type_text(self, text, target_kind=TextTargetKind.EDITABLE):
                payload = json.dumps({"text": text, "target_kind": TextTargetKind.coerce(target_kind).value})
                self._run_python(CLIPBOARD_SCRIPT.format(serialized=payload),
                                 timeout=self.command_timeout_seconds)
                return f"Typed {len(text)} character(s)."

        self.port = DesktopPort(ReceiptBackend(), self.output, self.emit, self.budget)
        return self.port

    async def gui_goal(self, params: Card):
        self.budget.check()
        port = self.bind()
        card = params.model_dump()
        card["max_actions"] = min(card["max_actions"], self.config.get("max_actions", 64),
                                  self.budget.limit - self.budget.actions)
        runner = LocalExecutor(
            self.selector, port.observe, port.perform, self.emit,
            max_frame_age=self.config.get("max_frame_age_seconds", 1.0),
            failed_actions=port.failed_actions, input_history=port.input_history, initial_frame=port.frame)
        result = await runner.run(card)
        result["text"] += f"\nRemaining episode actions: {self.budget.limit - self.budget.actions}."
        return result

    def terminal(self, params: Terminal):
        """This policy rejects common GUI automation; it is not a security sandbox for arbitrary Bash."""
        from .receipts import run_terminal

        if _GUI_AUTOMATION.search(params.command):
            raise ValueError("GUI automation is not allowed in terminal; use gui_goal")
        self.budget.reserve("cli")
        self.emit({"route": "action", "owner": "slow_model", "step": self.budget.actions,
                   "operation": "terminal", "parameters": params.model_dump()})
        result = run_terminal(self.bind().backend, params.model_dump(), self.budget.deadline - time.time())
        self.emit({"route": "receipt", "step": self.budget.actions, "owner": "slow_model",
                   "operation": "terminal", "result": result})
        return result

    def statistics(self):
        return {"physical_actions": self.budget.actions, "gui_actions": self.budget.gui_actions,
                "cli_actions": self.budget.cli_actions, "jev_calls": self.choice_calls,
                "jev_seconds": self.choice_seconds, "logical_steps": self.logical_steps}

    def on_step(self, record):
        self.logical_steps += 1
        self.emit({"route": "logical_step", "logical_step": self.logical_steps,
                   "physical_actions": self.budget.actions})

    def before_step(self, index):
        self.budget.check()

    def model_call(self, seconds, failed, usage):
        self.emit({"route": "slow_model_call", "seconds": seconds, "failed": failed, "usage": usage})

    def close(self):
        from northcua.runtime.mcp.registry import NorthCUAMCPRegistry

        (self.output / "jev_stats.json").write_text(json.dumps(self.statistics()), encoding="utf-8")
        if self.original is not None:
            NorthCUAMCPRegistry.register(
                SERVER, self.original.factory, tool_contracts=self.original.tool_contracts)


def install(config, output, max_actions, deadline):
    """Replace the actor's action-server surface through the existing public MCP registry."""
    from fastmcp import FastMCP
    from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
    from northcua.runtime.mcp.contracts import register_contract_handlers
    from northcua.runtime.mcp.desktop_native.action_tools import _desktop_action_handler_map
    from northcua.runtime.mcp.desktop_native.state import DesktopNativeServerState
    from northcua.runtime.mcp.registry import NorthCUAMCPRegistry
    from northcua_protocol.capabilities import Capability, ToolRiskLevel
    from northcua_runtime_shared.tool_contracts import McpBinding, ToolContract

    ensure_builtin_mcp_servers_registered()
    owner = LocalTools(config, output, max_actions, deadline)
    owner.original = NorthCUAMCPRegistry._registry[SERVER]
    contracts = [c for c in owner.original.tool_contracts if c.tool_id in {"note", "screenshot"}]
    for name, model, description in (
        ("gui_goal", Card, INSTRUCTIONS),
        ("terminal", Terminal, "Run bounded Bash INSIDE the task VM, not on the worker. "
         "Return stdout/stderr/exit code. Never issue GUI input or inspect evaluator/setup answers. "
         "A failed or timed-out command may already have side effects; inspect before retrying."),
    ):
        contracts.append(ToolContract(
            tool_id=name, namespace="jev_local", agent_kind="desktop", description=description,
            params_model=model, timeout_seconds=18000 if name == "gui_goal" else 660,
            capability=Capability.DESKTOP_INPUT, risk_level=ToolRiskLevel.HIGH,
            binding=McpBinding(server_name=SERVER)))

    def factory(context):
        server = FastMCP(SERVER)
        native = _desktop_action_handler_map(DesktopNativeServerState(), owner.bind().backend)
        handlers = {name: native[name] for name in ("note", "screenshot")}
        handlers.update(gui_goal=owner.gui_goal, terminal=owner.terminal)
        register_contract_handlers(server, contracts, handlers)
        return server

    NorthCUAMCPRegistry.register(SERVER, factory, tool_contracts=contracts)
    return owner
