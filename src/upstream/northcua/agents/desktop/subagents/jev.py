"""Reuse jev-desktop's synchronous loop through budgeted async runtime bridges."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
import time
from contextlib import closing, suppress
from dataclasses import asdict, replace
from typing import Any, cast
from typing import Literal
from pathlib import Path
from uuid import uuid4

import httpx
from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, Limits, RunOptions, Scope, TypeSafeModel
from jev_desktop.desktop import CATALOG
from jev_desktop.desktop import delivery_evidence
from jev_desktop.text_model import INSTRUCTIONS
from pydantic import Field, StrictBool, StrictInt

from northcua.agentkit.orchestration.contracts import (
    ContractModel,
    SubagentRequest,
    SubagentResult,
    SubagentRuntime,
    SubagentSpec,
    Usage,
)
from northcua.agentkit.orchestration.runtime import BudgetExceeded
from northcua.core.config import DesktopPolicyConfig
from northcua.core.config_base import GuiJevConfig

from .jev_environment import JevEnvironment
from .jev_applications import resolve_application
from .jev_feedback import bounded_json, feedback
from .jev_progress import repeated_blocker, unreconciled_actions
from .jev_chat import ChatDecisionModel, ChatDecisionSettings
from .jev_trajectory import TrajectoryWriteError


class JevInput(ContractModel):
    app: str | None = None
    root: str | None = None
    window_id: str | None = None
    text: list[str] | None = None
    snapshot_timeout_ms: StrictInt | None = Field(default=None, ge=1, le=60000)
    input_mode: Literal["both", "replace", "insert"] = "both"
    keys: list[str] | None = Field(default=None, max_length=192)
    resources: list[str] | None = Field(default=None, max_length=16)
    allowed_operations: list[str] | None = None


class JevOptions(ContractModel):
    scope: dict[str, str | None] = Field(default_factory=dict)
    text: str | list[str] | None = None
    cursor: StrictBool = False
    values: StrictBool = True
    confirm_destructive: StrictBool = False
    resources: list[str] = Field(default_factory=list, max_length=16)
    replan_safe_failures: StrictBool = False
    reconcile_observed_popups: StrictBool = False
    text_replans: StrictInt = Field(default=0, ge=0, le=3)
    replan_changed_text_targets: StrictBool = False
    allow_infeasible: StrictBool = False
    follow_owned_dialogs: StrictBool = False
    review_goal_scope: StrictBool = False
    follow_application_windows: StrictBool = False
    remember_closed_windows: StrictBool = False
    replan_no_effect: StrictBool = False
    verify_terminal: StrictBool = False
    startup_observation_retries: StrictInt = Field(default=0, ge=0, le=10)


class _Stopped(RuntimeError):
    def __init__(self, message, *, result=None, dispatched=False):
        super().__init__(message)
        self.result, self.dispatched = result, dispatched


class _Bridge:
    def __init__(self, runtime: SubagentRuntime):
        self.runtime, self.loop = runtime, asyncio.get_running_loop()
        self.stopping = threading.Event()
        self.cleaning = False
        self.active = None
        self.execution_diagnostics = []
        self.pending_action = None
        self.trajectory = getattr(runtime, "trajectory", None)
        self.active_action = None
        self.last_action = None
        self.decision_id = None
        self.pending_decisions = []

    def record(self, event, **payload):
        if self.trajectory is not None:
            if self.cleaning:
                # A storage failure must never prevent already-required resource cleanup.
                with suppress(TrajectoryWriteError):
                    self.trajectory.record(event, payload)
            else:
                self.trajectory.record(event, payload)

    def capture(self, phase, action_id):
        if self.trajectory is None or not self.trajectory.screenshots:
            return
        if self.stopping.is_set():
            self.record("screenshot_unavailable", action_id=action_id, phase=phase, reason="interrupted")
            return
        try:
            observed = self.call(lambda: self.runtime.observe_for_trajectory(), kind="observation")
        except _Stopped:
            self.record("screenshot_unavailable", action_id=action_id, phase=phase, reason="interrupted")
            raise
        except Exception as error:
            self.record("screenshot_unavailable", action_id=action_id, phase=phase,
                        reason=type(error).__name__)
            return
        path = observed.get("screenshot_path")
        if not isinstance(path, str) or not Path(path).is_file():
            self.record("screenshot_unavailable", action_id=action_id, phase=phase,
                        reason="image_not_found", observation=observed)
            return
        self.record("screenshot", action_id=action_id, phase=phase, observation=observed,
                    policy_input=False)

    def wire_response(self, response, *, backend, attempt):
        self.record("model_response", decision_id=self.decision_id, backend=backend, attempt=attempt,
                    http_status=response.status_code, response_body=response.text)

    async def post_model(self, endpoint, payload, *, api_key, timeout, backend, attempt, direct_api=False):
        ledger = getattr(self.runtime, "model_usage", None)
        call_id = ledger.start(payload.get("model"), backend=backend, attempt=attempt,
                               decision_id=self.decision_id,
                               response_ref=str(self.trajectory.path) if self.trajectory else None) if ledger else None
        try:
            self.record("model_request", decision_id=self.decision_id, backend=backend,
                        attempt=attempt, request=payload, call_id=call_id)
            async with httpx.AsyncClient(trust_env=not direct_api, follow_redirects=False) as client:
                response = await client.post(endpoint, headers={"Authorization": f"Bearer {api_key}"},
                                             json=payload, timeout=timeout)
            if ledger is not None:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                ledger.response(call_id, body, http_status=response.status_code)
            self.wire_response(response, backend=backend, attempt=attempt)
            return response
        except BaseException as error:
            if ledger is not None:
                ledger.failure(call_id, error)
            raise

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
            raise _Stopped("gui_jev interrupted", dispatched=True) from None
        finally:
            self.active = None
        if self.stopping.is_set() and not cleanup:
            raise _Stopped("gui_jev interrupted", result=value, dispatched=True)
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
        call_id = uuid4().hex
        self.record("native_started", native_call_id=call_id, action_id=self.active_action,
                    executable=executable, argv=argv, timeout=timeout, cleanup=self.cleaning)
        try:
            if self.cleaning:
                result = self.call(lambda: self.runtime.cleanup("agent_desktop", arguments),
                                   kind="cleanup", cleanup=True)
            else:
                result = self.call(lambda: self.runtime.execute("agent_desktop", arguments, sensitive=True), kind="tool")
        except BaseException as error:
            result = getattr(error, "result", None)
            self.record("native_interrupted", native_call_id=call_id, action_id=self.active_action,
                        result=result, error_type=type(error).__name__,
                        delivery=(delivery_evidence(result).get("delivery") if isinstance(result, dict) else
                                  "not_dispatched" if isinstance(error, BudgetExceeded)
                                  or isinstance(error, _Stopped) and not error.dispatched else "unconfirmed"))
            raise
        self.record("native_finished", native_call_id=call_id, action_id=self.active_action, result=result)
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

    def observe(self, scope):
        observed = super().observe(scope)
        self.bridge.record("ax_observation", action_id=self.bridge.active_action, scope=asdict(scope),
                           observation=asdict(observed))
        return observed

    def _action(self, operation, call, *, window_id=None, ref=None, native=False, arguments=None):
        receipt = None
        result, error_name = None, None
        outer = self.bridge.active_action is None
        action_id = self.bridge.active_action or uuid4().hex
        started = time.monotonic()
        called = False
        if outer:
            self.bridge.active_action = action_id
        try:
            if outer:
                self.bridge.record("action_started", action_id=action_id, operation=operation,
                                   window_id=window_id, ref=ref, arguments=arguments,
                                   decision_ids=list(self.bridge.pending_decisions))
                self.bridge.pending_decisions.clear()
                self.bridge.capture("before_action", action_id)
            called = True
            result = call()
            receipt = delivery_evidence(result) if native else result
            return result
        except BudgetExceeded:
            error_name = "BudgetExceeded"
            raise  # ScopedRuntime rejects exhausted budgets before issuing the tool call.
        except BaseException as error:
            error_name = type(error).__name__
            if not called or isinstance(error, _Stopped) and not error.dispatched:
                raise
            raw = getattr(error, "result", None)
            receipt = delivery_evidence(raw) if isinstance(raw, dict) else {"delivery": "delivery_uncertain"}
            raise
        finally:
            if receipt is not None and operation not in ("WAIT", "DRILL", "WIDEN"):
                # Do not retain argv/text or unredacted native response bodies. Without a
                # yielded turn, the target is deliberately unresolved within this window.
                self.bridge.pending_action = {
                    "operation": operation, "target": "*", "ref": ref, "window_id": window_id,
                    "ok": receipt.get("ok", False),
                    "delivery": receipt.get("delivery") or "delivery_uncertain",
                    "error_code": (receipt.get("error") or {}).get("code"),
                }
            if outer:
                self.bridge.last_action = action_id
                try:
                    # Persist the outcome before any optional screenshot read.
                    self.bridge.record("action_finished", action_id=action_id, operation=operation,
                                       window_id=window_id, ref=ref, result=result, receipt=receipt,
                                       error_type=error_name, dispatch_invoked=called,
                                       elapsed_seconds=time.monotonic() - started)
                    if called:
                        self.bridge.capture("after_action", action_id)
                finally:
                    self.bridge.active_action = None

    def execute(self, scope, operation, node, text):
        execute = super().execute
        return self._action(
            operation, lambda: execute(scope, operation, node, text),
            window_id=scope.window_id, ref=node.get("ref_id") if node else None,
            arguments={"scope": asdict(scope), "node": node, "text": text},
        )

    def cli(self, *argv):
        previous = self.bridge.cleaning
        self.bridge.cleaning |= argv == ("cursor-overlay", "disable")
        try:
            cli = super().cli
            if argv and argv[0] == "launch":
                result = self._action("LAUNCH", lambda: cli(*argv), native=True, arguments={"argv": list(argv)})
            else:
                result = cli(*argv)
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
                response = await self.bridge.post_model(
                    self.config.endpoint, {**request, "model": self.config.model},
                    api_key=self.config.api_key, timeout=30, backend="typesafe", attempt=attempt + 1,
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
        text_id = uuid4().hex
        self.bridge.record("text_requested", text_request_id=text_id, request=asdict(request), prompt=prompt)
        response = self.bridge.call(lambda: self.bridge.runtime.infer(prompt))
        self.bridge.record("text_returned", text_request_id=text_id, response=response)
        value = json.loads(response)
        if not isinstance(value, dict) or set(value) != {"text"} or not isinstance(value["text"], (str, type(None))):
            raise ValueError("Slow model returned invalid field text")
        return value["text"]


class _RecordedDecision:
    def __init__(self, bridge, model):
        self.bridge, self.model = bridge, model

    def ask(self, request):
        decision_id = uuid4().hex
        self.bridge.decision_id = decision_id
        self.bridge.pending_decisions.append(decision_id)
        try:
            self.bridge.record("decision_requested", decision_id=decision_id, request=request,
                               last_action_id=self.bridge.last_action)
            result = self.model.ask(request)
            self.bridge.record("decision_returned", decision_id=decision_id, result=result,
                               validation="see backend validation and subsequent loop/action events")
            return result
        except BaseException as error:
            self.bridge.record("decision_failed", decision_id=decision_id, error_type=type(error).__name__)
            raise
        finally:
            self.bridge.decision_id = None


class JevSubagent:
    spec = SubagentSpec(
        agent_id="gui_jev",
        description=(
            "PRIMARY GUI EXECUTOR. Prefer gui_jev for application work addressable through native accessibility "
            "(AX), observed element actions or supported window keys. It runs its own observe/select/execute "
            "loop through agent-desktop, with the configured finite-choice model; Slow supplies a bounded "
            "desired state rather than individual coordinates. Use allowed_tools=[agent_desktop]. "
            "Normal work includes opening an installed app or existing window; menus, context menus, toolbars, "
            "dialogs and preferences; filling form, mail and editor fields; find/replace and command palettes; "
            "checkboxes and radio controls; list, tab, tree, combo and table choices; AX-exposed slides or "
            "document objects; formatting through exposed controls; and GUI file-open, save and confirmation "
            "workflows. These depend on actual capabilities, not the application's name. "
            "Actions include advertised semantic click/focus/toggle/expand/scroll and selection operations. "
            "Headed mode additionally offers bound pointer actions and drags between observed source/destination "
            "elements, window focus/maximize, and finite keyboard shortcuts for navigation, editing, search, "
            "save and dialogs. An unexposed canvas object or arbitrary pixel location is outside this interface. "
            "Typing multi-line or Unicode content is not a reason to switch agents. TYPE_TEXT replaces a whole "
            "field; INSERT_TEXT inserts at the current caret/selection. Supply exact ordered values in text, "
            "or describe the content explicitly in the goal and use the shared Slow text callback. Only those "
            "two operations consume text. SELECT chooses an observed option without consuming text; selection, "
            "focus, activation and Enter have different effects. The opt-in configured clipboard backend is "
            "only a guarded whole-field route after a safe native refusal; no unknown write is replayed. "
            "Without window_id/root, it first selects an existing window or installed application. app is a hint; "
            "verified window_id/root impose a strict scope. Do not invent native IDs or reuse historical refs. "
            "resources binds up to 16 known authorized absolute guest paths to OPEN_RESOURCE:n choices; "
            "Ctrl+O also permits ordinary GUI file opening. A handoff does not verify resource identity. "
            "input_mode controls replacement/insertion; keys replaces the default shortcut list; "
            "allowed_operations restricts choices and should retain needed navigation. snapshot_timeout_ms "
            "sets the executable read timeout. Prose cannot create an operation. "
            "Returns current AX context, supported operations, action/delivery evidence and blockers. "
            "A yield with progress supports another Jev subgoal. Inspect a blocker before choosing a recovery; "
            "a missing ref is not proof that all keyboard/menu routes fail. No screenshot reasoning, arbitrary "
            "coordinate generation or direct shell tool. Verify field content separately from save/send/commit."
        ),
        capabilities=("native_gui", "accessibility", "text_input", "keyboard", "pointer", "selection"),
        required_tools=("agent_desktop",),
        input_model=JevInput,
    )

    def __init__(self, config: GuiJevConfig, desktop: DesktopPolicyConfig):
        if not (config.desktop.get("binary") or os.environ.get("AGENT_DESKTOP_BIN")):
            raise ValueError("gui_jev requires desktop.binary or AGENT_DESKTOP_BIN naming the VM binary")
        self.desktop_config = DesktopConfig.from_env(remote=True, overrides={"clipboard_text": True, **config.desktop})
        self.environment = JevEnvironment(desktop, self.desktop_config)
        self.chat = (
            ChatDecisionSettings.from_env(config.decision)
            if config.decision.backend == "chat_completions" else None
        )
        self.model = None if self.chat is not None else TypeSafeModel.from_env()
        self.limits = Limits(**cast(dict[str, Any], config.limits))
        self.launch_args = config.launch_args
        options = JevOptions.model_validate(config.options)
        self.options = RunOptions(
            scope=Scope(**options.scope),
            resources=tuple(options.resources),
            **options.model_dump(exclude={"scope", "resources"}, exclude_unset=True),
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
            resources=tuple(agent_input.resources) if agent_input.resources is not None else self.options.resources,
            follow_application_windows=self.options.follow_application_windows and not bool(scope.window_id),
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
        bridge.record("agent_started", request=request.model_dump(mode="json"),
                      effective_scope=asdict(scope), keys=options.keys, resources=options.resources,
                      input_mode=options.input_mode, allowed_operations=options.allowed_operations)

        def run():
            config = self.desktop_config
            if agent_input.snapshot_timeout_ms is not None:
                config = replace(config, snapshot_timeout_ms=agent_input.snapshot_timeout_ms)
            desktop = _Desktop(config, bridge, sleep=bridge.sleep)
            model = ChatDecisionModel(bridge, self.chat) if self.chat is not None else _DecisionModel(bridge, self.model)
            model = _RecordedDecision(bridge, model)
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
            for event in events:
                bridge.record("loop_event", data=event, action_id=bridge.last_action)
            bridge.pending_action = None  # Application-selection turns are already in events.
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
                    if "turn" in event:
                        bridge.pending_action = None
                    bridge.record("loop_event", data=event, action_id=bridge.last_action)
            return events

        def partial_result(error):
            summary = feedback(events, execution_diagnostics=bridge.execution_diagnostics)
            action = bridge.pending_action
            if action is not None:
                summary["interrupted_action"] = action
                if action["delivery"] != "not_delivered":
                    summary["unsafe_to_replay"].append(action)
                    summary["unresolved_delivery"] = True
                    if action["window_id"] is not None:
                        summary["screen"]["window_id"] = action["window_id"]
                    summary["recovery"]["must_reobserve"] = True
            return SubagentResult(
                subgoal_id=request.contract.subgoal_id,
                attempt_id=request.attempt_id,
                outcome="failed",
                exit_reason=type(error).__name__,
                summary=bounded_json(summary),
                evidence_refs=[str(bridge.trajectory.path)] if bridge.trajectory is not None else [],
                usage=getattr(runtime, "usage", Usage()).model_copy(),
            )

        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            events = await asyncio.shield(worker)
            final_observation = await runtime.observe()
            image = final_observation.get("screenshot_path")
            bridge.record("subgoal_observation", observation=final_observation, policy_input=False,
                          image_available=isinstance(image, str) and Path(image).is_file())
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
            error.subagent_result = partial_result(error)
            with suppress(TrajectoryWriteError):
                bridge.record("attempt_finished", status="exception", result=error.subagent_result.model_dump(mode="json"))
            raise
        except BaseException as error:
            bridge.stop()
            # A cancelled wrapper must not return while the synchronous loop can still act.
            while not worker.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(worker)
            with suppress(BaseException):
                worker.result()
            events.append({"stop": "cancelled", "error": {"code": type(error).__name__}})
            error.subagent_result = partial_result(error)
            with suppress(TrajectoryWriteError):
                bridge.record("attempt_finished", status="cancelled", result=error.subagent_result.model_dump(mode="json"))
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
        result = SubagentResult(
            subgoal_id=request.contract.subgoal_id,
            attempt_id=request.attempt_id,
            outcome=outcome,
            exit_reason="window_activation_unconfirmed" if activation_blocked else stop,
            summary=bounded_json(feedback(events, execution_diagnostics=bridge.execution_diagnostics)),
            criterion_claims=request.contract.success_criteria if stop == "done" else [],
            evidence_refs=[str(bridge.trajectory.path)] if bridge.trajectory is not None else [],
            last_observation=final_observation,
            usage=getattr(runtime, "usage", Usage()).model_copy(),
        )
        try:
            bridge.record("attempt_finished", status="returned", result=result.model_dump(mode="json"))
        except TrajectoryWriteError as error:
            result.outcome, result.exit_reason = "failed", "trajectory_write_failed"
            error.subagent_result = result
            raise
        return result
