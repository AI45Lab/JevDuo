"""Reuse jev-desktop's synchronous loop through budgeted async runtime bridges."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from contextlib import closing, suppress
from dataclasses import asdict, replace
from typing import Any, cast
from typing import Literal

import httpx
from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, Limits, RunOptions, Scope, TypeSafeModel
from jev_desktop.desktop import CATALOG
from jev_desktop.text_model import INSTRUCTIONS
from pydantic import Field, StrictBool, StrictInt

from northcua.agentkit.orchestration.contracts import (
    ContractModel,
    SubagentRequest,
    SubagentResult,
    SubagentRuntime,
    SubagentSpec,
)
from northcua.core.config import DesktopPolicyConfig
from northcua.core.config_base import GuiJevConfig

from .jev_environment import JevEnvironment
from .jev_applications import resolve_application
from .jev_feedback import bounded_json, feedback
from .jev_progress import repeated_blocker, unreconciled_actions


class JevInput(ContractModel):
    app: str | None = None
    root: str | None = None
    window_id: str | None = None
    text: list[str] | None = None
    snapshot_timeout_ms: StrictInt | None = Field(default=None, ge=1, le=60000)
    input_mode: Literal["both", "replace", "insert"] = "both"
    keys: list[str] | None = Field(default=None, max_length=100)
    allowed_operations: list[str] | None = None


class JevOptions(ContractModel):
    scope: dict[str, str | None] = Field(default_factory=dict)
    text: str | list[str] | None = None
    cursor: StrictBool = False
    values: StrictBool = True
    confirm_destructive: StrictBool = False


class _Stopped(RuntimeError):
    pass


class _Bridge:
    def __init__(self, runtime: SubagentRuntime):
        self.runtime, self.loop = runtime, asyncio.get_running_loop()
        self.stopping = threading.Event()
        self.cleaning = False
        self.active = None
        self.execution_diagnostics = []

    def call(self, factory, *, kind="model", cleanup=False):
        async def guarded():
            if self.stopping.is_set() and not cleanup:
                raise _Stopped("gui_jev interrupted")
            return await factory()

        if self.stopping.is_set() and not cleanup:
            raise _Stopped("gui_jev interrupted")
        future = asyncio.run_coroutine_threadsafe(guarded(), self.loop)
        self.active = future, kind
        try:
            value = future.result()
        except concurrent.futures.CancelledError:
            raise _Stopped("gui_jev interrupted") from None
        finally:
            self.active = None
        if self.stopping.is_set() and not cleanup:
            raise _Stopped("gui_jev interrupted")
        return value

    def stop(self):
        self.stopping.set()
        active = self.active
        if active is not None and active[1] == "model":
            active[0].cancel()

    def sleep(self, seconds):
        if self.stopping.wait(seconds):
            raise _Stopped("gui_jev interrupted")

    def invoke(self, executable, argv, timeout):
        operation = "catalog" if executable == "python3" and argv == ["-c", CATALOG] else "cli"
        arguments = {"operation": operation, "argv": [] if operation == "catalog" else argv, "timeout": timeout}
        if self.cleaning:
            return self.call(lambda: self.runtime.cleanup("agent_desktop", arguments), kind="cleanup", cleanup=True)
        result = self.call(lambda: self.runtime.execute("agent_desktop", arguments, sensitive=True), kind="tool")
        diagnostic = result.get("execution_diagnostic")
        if isinstance(diagnostic, dict):
            self.execution_diagnostics.append(diagnostic)
            self.execution_diagnostics = self.execution_diagnostics[-8:]
        return result


class _Desktop(AgentDesktop):
    def __init__(self, config, bridge: _Bridge, **kwargs):
        super().__init__(config, bridge, **kwargs)
        self.bridge = bridge
        self.app_inventory = {}

    def cli(self, *argv):
        previous = self.bridge.cleaning
        self.bridge.cleaning |= argv == ("cursor-overlay", "disable")
        try:
            result = super().cli(*argv)
            if self.bridge.cleaning and not result.get("ok"):
                raise RuntimeError("gui_jev cursor cleanup failed")
            if argv == ("list-apps",) and result.get("ok"):
                self.app_inventory = result["data"]
            return result
        finally:
            self.bridge.cleaning = previous


class _DecisionModel:
    def __init__(self, bridge: _Bridge, config: TypeSafeModel):
        self.bridge, self.config = bridge, config

    def ask(self, request):
        async def ask() -> dict:
            async def post():
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    response = await client.post(
                        self.config.endpoint,
                        headers={"Authorization": f"Bearer {self.config.api_key}"},
                        json={**request, "model": self.config.model},
                        timeout=30,
                    )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                    raise ValueError("invalid Jev response")
                return result

            for attempt in range(3):
                try:
                    return await self.bridge.runtime.call_model(self.config.model, post)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in (429, 503, 529) or attempt == 2:
                        raise RuntimeError(f"Jev returned HTTP {error.response.status_code}") from None
                    await asyncio.sleep(0.5 * 2**attempt)
                except (httpx.HTTPError, ValueError):
                    raise RuntimeError("Jev returned no valid decision; no action dispatched") from None
            raise RuntimeError("Jev stayed unavailable")

        return self.bridge.call(ask)


class _TextModel:
    def __init__(self, bridge: _Bridge):
        self.bridge = bridge

    def generate(self, request):
        prompt = (
            INSTRUCTIONS
            + '\nEncode the field content as JSON {"text": string or null}. Return ONLY this JSON object. '
            + "Use null when essential information is missing. Input:\n"
            + json.dumps(asdict(request), ensure_ascii=False)
        )
        response = self.bridge.call(lambda: self.bridge.runtime.infer(prompt))
        value = json.loads(response)
        if not isinstance(value, dict) or set(value) != {"text"} or not isinstance(value["text"], (str, type(None))):
            raise ValueError("Slow model returned invalid field text")
        return value["text"]


class JevSubagent:
    spec = SubagentSpec(
        agent_id="gui_jev",
        description=(
            "Native accessibility GUI subagent using agent-desktop Jev target/operation selection. "
            "Text for editable fields is generated by the shared Slow model when not supplied. "
            "allowed_tools must include agent_desktop. Specify one bounded, visibly verifiable goal. "
            "Supports advertised ref actions; no screenshot coordinate or arbitrary shell fallback."
            " Before an unpinned subgoal, Jev selects an existing window or an installed app to launch. "
            "app is a selection hint; window_id/root pin a strict scope. In headed mode WINDOW offers native keys "
            "including return, escape, tab, arrows and editing shortcuts. keys replaces the bounded native key list. "
            "input_mode enforces insert/replace; allowed_operations restricts operation names; "
            "Only TYPE_TEXT and INSERT_TEXT consume the supplied text list. SELECT applies to containers with "
            "observed options and makes a separate bounded choice without consuming text. "
            "Selecting an item is distinct from activating it; WINDOW PRESS_KEY:return submits the current input "
            "or navigates an address when appropriate. "
            "snapshot_timeout_ms sets a bounded read timeout. Textual constraints alone do not add operations. "
            "Blocked results include native screen evidence and available_operations; do not repeat an unavailable action."
        ),
        capabilities=("native_gui", "accessibility", "text_input", "keyboard", "pointer", "selection"),
        required_tools=("agent_desktop",),
        input_model=JevInput,
    )

    def __init__(self, config: GuiJevConfig, desktop: DesktopPolicyConfig):
        if not (config.desktop.get("binary") or os.environ.get("AGENT_DESKTOP_BIN")):
            raise ValueError("gui_jev requires desktop.binary or AGENT_DESKTOP_BIN naming the VM binary")
        self.desktop_config = DesktopConfig.from_env(remote=True, overrides=config.desktop)
        self.environment = JevEnvironment(desktop, self.desktop_config)
        self.model = TypeSafeModel.from_env()
        self.limits = Limits(**cast(dict[str, Any], config.limits))
        self.launch_args = config.launch_args
        options = JevOptions.model_validate(config.options)
        self.options = RunOptions(
            scope=Scope(**options.scope), **options.model_dump(exclude={"scope"}, exclude_unset=True)
        )

    async def run(self, request: SubagentRequest, runtime: SubagentRuntime) -> SubagentResult:
        agent_input = JevInput.model_validate(request.contract.agent_input)
        blocker = repeated_blocker(request.history, request.contract.agent_input)
        bridge = _Bridge(runtime)
        scope = replace(
            self.options.scope, **agent_input.model_dump(include={"app", "root", "window_id"}, exclude_none=True)
        )
        options = replace(
            self.options,
            scope=scope,
            text=agent_input.text if agent_input.text is not None else self.options.text,
            text_model=_TextModel(bridge),
            input_mode=agent_input.input_mode,
            keys=tuple(agent_input.keys) if agent_input.keys is not None else self.options.keys,
            allowed_operations=(
                tuple(agent_input.allowed_operations) if agent_input.allowed_operations is not None else None
            ),
            include_context=True,
            previous_blocker=blocker,
            blocked_actions=unreconciled_actions(request.history),
        )
        goal = json.dumps(
            {
                "goal": request.contract.goal,
                "success_criteria": request.contract.success_criteria,
                "constraints": request.contract.constraints,
            },
            ensure_ascii=False,
        )

        events = []

        def run():
            config = self.desktop_config
            if agent_input.snapshot_timeout_ms is not None:
                config = replace(config, snapshot_timeout_ms=agent_input.snapshot_timeout_ms)
            desktop = _Desktop(config, bridge, sleep=bridge.sleep)
            model = _DecisionModel(bridge, self.model)
            selected, _, calls = resolve_application(
                desktop,
                model,
                goal,
                scope,
                self.launch_args,
                self.limits.calls,
                blocked_launches=tuple(
                    action["target"]
                    for action in options.blocked_actions
                    if action.get("operation") == "LAUNCH"
                    or (action.get("operation") == "*" and action.get("window_id") is None)
                ),
                events=events,
            )
            if selected is None:
                return events
            if calls >= self.limits.calls:
                return [*events, {"stop": f"stopped at the {self.limits.calls} model call budget"}]
            loop = JevLoop(
                desktop, model, limits=replace(self.limits, calls=self.limits.calls - calls), sleep=bridge.sleep
            )
            with closing(loop.run(goal, replace(options, scope=selected))) as iterator:
                for event in iterator:
                    events.append(event)
            return events

        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            events = await asyncio.shield(worker)
            await runtime.observe()
        except Exception as error:
            events.append(
                {
                    "stop": "exception",
                    "error": {
                        "code": getattr(error, "code", None) or type(error).__name__,
                        "message": str(error),
                    },
                }
            )
            error.subagent_result = SubagentResult(
                subgoal_id=request.contract.subgoal_id,
                attempt_id=request.attempt_id,
                outcome="failed",
                exit_reason=type(error).__name__,
                summary=bounded_json(feedback(events, execution_diagnostics=bridge.execution_diagnostics)),
            )
            raise
        except BaseException:
            bridge.stop()
            # A cancelled wrapper must not return while the synchronous loop can still act.
            while not worker.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(worker)
            with suppress(BaseException):
                worker.result()
            raise
        terminal = events[-1] if events else {}
        stop = terminal.get("stop", "no_terminal_event")
        error = terminal.get("error") or {}
        activation_blocked = (
            stop == "action_failed"
            and error.get("details", {}).get("cause") == "frame_not_active"
            and error.get("details", {}).get("physical_delivery_started") is False
        )
        outcome = (
            "blocked"
            if activation_blocked
            else "achieved"
            if stop == "done"
            else "failed"
            if stop in ("action_failed", "exception")
            else "yielded"
            if stop.startswith("stopped ")
            else "blocked"
        )
        return SubagentResult(
            subgoal_id=request.contract.subgoal_id,
            attempt_id=request.attempt_id,
            outcome=outcome,
            exit_reason="window_activation_unconfirmed" if activation_blocked else stop,
            summary=bounded_json(feedback(events, execution_diagnostics=bridge.execution_diagnostics)),
            criterion_claims=request.contract.success_criteria if stop == "done" else [],
        )
