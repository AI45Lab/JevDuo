"""Goal loop with injected desktop, decision model, budgets, and clock delay."""

import copy
import time
from dataclasses import replace

from .actions import TEXT_OPERATIONS, extend_space
from .contracts import DecisionModel, Desktop, DesktopError, Limits, RunOptions, Scope, TextModelError
from .policy import (
    TextSupply,
    action_space,
    build_operation_request,
    build_request,
    criterion,
    launch_request,
    risk_request,
    valid_probability,
    validate_choice,
)
from .screen import describe, fingerprint
from .selection import choose_option
from .text_request import build_text_request, capture_text_state, offer_text_model

WINDOW_STOPS = {
    "WINDOW_NOT_FOUND": "window_closed",
    "SURFACE_OUTSIDE_WINDOW": "surface_outside_window",
    "ROOT_OUTSIDE_WINDOW": "root_outside_window",
}


def redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


class BudgetStop(Exception):
    pass


class JevLoop:
    def __init__(self, desktop: Desktop, model: DecisionModel, *, limits=None, sleep=time.sleep):
        self.desktop, self.model = desktop, model
        self.limits, self.sleep = limits or Limits(), sleep

    def run(self, goal: str, options: RunOptions | None = None):
        """Yield detached JSON events; exhaust or close this iterator to release resources."""
        options = options or RunOptions()
        supply = TextSupply(options.text)
        events = self._run(goal, options, supply)
        try:
            for event in events:
                event = copy.deepcopy(event)
                if "screen" in event:
                    context = event["screen"].pop("context", [])
                    if options.include_context:
                        event["screen"]["visible_context"] = [
                            {k: v for k, v in n.items() if options.values or k != "value"}
                            for n in context[:200]
                        ]
                        event["screen"]["context_truncated"] = len(context) > 200
                yield redact(event, supply.known)
        except Exception as error:
            raise DesktopError(redact(str(error), supply.known), getattr(error, "code", None)) from None
        finally:
            events.close()

    def _run(self, goal, options, supply):
        calls, steps, history = 0, 0, []
        scope, last = options.scope, {}

        def consume_call():
            nonlocal calls
            if calls >= self.limits.calls:
                raise BudgetStop(f"stopped at the {self.limits.calls} model call budget")
            calls += 1

        def ask(request):
            consume_call()
            return self.model.ask(request)

        with self.desktop.run_context(goal, options.cursor):
            try:
                while True:
                    if steps >= self.limits.steps:
                        yield {
                            "stop": f"stopped at the {self.limits.steps} action budget",
                            "screen": last,
                            "history": history,
                        }
                        return
                    if calls >= self.limits.calls:
                        raise BudgetStop(f"stopped at the {self.limits.calls} model call budget")
                    try:
                        seen = self.desktop.observe(scope)
                    except DesktopError as error:
                        if scope == Scope() and steps == 0 and error.code == "WINDOW_NOT_FOUND":
                            scope = yield from self._bootstrap(goal, ask)
                            if scope is None:
                                return
                            continue
                        else:
                            stop = WINDOW_STOPS.get(error.code) if scope.window_id else None
                            if stop is None:
                                raise
                            yield {"stop": stop, "screen": last, "history": history}
                            return
                    nodes, screen = seen.nodes, seen.screen
                    scope = replace(scope, app=screen.get("app") or scope.app)
                    last = screen
                    blocker = options.previous_blocker
                    context = [
                        {key: value for key, value in entry.items() if options.values or key != "value"}
                        for entry in screen.get("context", [])[:200]
                    ]
                    if (
                        steps == 0
                        and blocker
                        and blocker.get("context") is not None
                        and blocker.get("app") == screen.get("app")
                        and blocker.get("window") == screen.get("window")
                        and blocker["context"] == redact(context, supply.known)
                    ):
                        yield {
                            "stop": "repeated_native_blocker",
                            "screen": screen,
                            "history": history,
                            "blocker": blocker,
                        }
                        return
                    before = copy.deepcopy(fingerprint(nodes))
                    generate_text = options.text_model is not None and not supply.available()
                    space = action_space(
                        nodes,
                        drillable=not scope.root,
                        typable=supply.available() or generate_text,
                        values=options.values,
                        headed=screen.get("headed_text", False),
                        max_elements=250 if screen.get("headed_text") else 251,
                    )
                    extend_space(space, screen, options)
                    request = build_request(goal, screen, space, history, values=options.values, compact=True)
                    if generate_text:
                        offer_text_model(request)
                    else:
                        request["state"]["text_input"] = supply.preview(values=options.values)
                    target = validate_choice(
                        ask(request).get("answers", {}).get("target"),
                        request["questions"]["target"]["criteria"],
                    )
                    selected = space["by_target"].get(target["choice"])
                    operation = target["choice"]
                    if operation in ("DONE", "BLOCKED"):
                        yield {
                            "stop": operation.lower(),
                            "screen": screen,
                            "why": target["probabilities"],
                            "confidence": target["confidence"],
                            "history": history,
                            "available_operations": sorted(
                                {op for entry in space["by_target"].values() for op in entry["operations"]}
                            ),
                        }
                        return
                    node = selected["node"] if selected else None
                    selection = None
                    if selected:
                        request = build_operation_request(
                            goal, screen, space, history, target=target, values=options.values, compact=True
                        )
                        if generate_text:
                            offer_text_model(request)
                        else:
                            request["state"]["text_input"] = supply.preview(values=options.values)
                        answer = validate_choice(
                            ask(request).get("answers", {}).get("operation"), selected["operations"]
                        )
                        operation = answer["choice"]
                        if operation == "SELECT":
                            selection = choose_option(ask, request, selected["selection_options"])
                            if selection is None:
                                yield {"stop": "selection_cancelled", "screen": screen, "history": history}
                                return
                        if operation == "DRAG":
                            criteria = {
                                index: criterion(candidate, index, values=options.values)
                                for index, candidate in node["drag_candidates"].items()
                            }
                            destination = validate_choice(
                                ask(
                                    {
                                        "state": request["state"],
                                        "questions": {
                                            "destination": {
                                                "type": "choice",
                                                "criteria": criteria,
                                                "instructions": "Choose the observed drop target for the selected drag.",
                                            }
                                        },
                                    }
                                )
                                .get("answers", {})
                                .get("destination"),
                                criteria,
                            )
                            operation = "DRAG_TO:" + destination["choice"]
                        if options.confirm_destructive:
                            rated = ask(risk_request(goal, screen, operation, node, values=options.values))
                            destructive = rated.get("answers", {}).get("destructive", {}).get("noul")
                            if not valid_probability(destructive):
                                destructive = 1
                            if destructive >= 0.5:
                                yield {
                                    "stop": f"confirm: hard to undo (destructive {destructive:.2f})",
                                    "screen": screen,
                                    "candidate": {
                                        "operation": operation,
                                        "target": criterion(node, target["choice"]),
                                        "confidence": target["confidence"],
                                    },
                                    "history": history,
                                }
                                return
                    value = selection["label"] if selection else None
                    target_scope = replace(
                        scope,
                        app=screen.get("app") or scope.app,
                        window_id=scope.window_id or screen.get("window_id"),
                    )
                    target_name = (
                        (node["role"] + (f' "{node["name"]}"' if node.get("name") else "")) if node else None
                    )
                    if any(
                        blocked.get("target") in (target_name, "*")
                        and blocked.get("window_id") in (None, target_scope.window_id)
                        and (
                            blocked.get("operation") in (operation, "*")
                            or (
                                blocked.get("operation") in {"TYPE_TEXT", "INSERT_TEXT", "CLEAR"}
                                and operation in {"TYPE_TEXT", "INSERT_TEXT", "CLEAR"}
                            )
                        )
                        for blocked in options.blocked_actions
                    ):
                        yield {
                            "stop": "unsafe_write_replay_blocked",
                            "screen": screen,
                            "error": {
                                "code": "UNRECONCILED_WRITE",
                                "operation": operation,
                                "target": target_name,
                            },
                            "history": history,
                        }
                        return
                    if operation in TEXT_OPERATIONS:
                        if generate_text:
                            consume_call()
                            text_state = capture_text_state(seen)
                            value = self._generate_text(
                                options.text_model,
                                replace(
                                    build_text_request(goal, screen, node, history, values=options.values),
                                    operation=operation,
                                ),
                                supply,
                            )
                        else:
                            value = supply.take(describe(node, True), history)
                    history = redact(history, supply.known)
                    if operation in TEXT_OPERATIONS and (not isinstance(value, str) or not value.strip()):
                        yield {
                            "stop": f"no value was supplied for {criterion(node, '?')}",
                            "screen": screen,
                            "history": history,
                        }
                        return
                    if operation in TEXT_OPERATIONS and generate_text:
                        target_scope = replace(
                            target_scope, window_id=scope.window_id or screen.get("window_id")
                        )
                        try:
                            fresh = self.desktop.observe(target_scope)
                        except DesktopError as error:
                            stop = WINDOW_STOPS.get(error.code) if target_scope.window_id else None
                            if stop is None:
                                raise
                            yield {"stop": stop, "screen": screen, "history": history}
                            return
                        if capture_text_state(fresh) != text_state:
                            yield {
                                "stop": "screen_changed_during_text_generation",
                                "screen": fresh.screen,
                                "history": history,
                            }
                            return
                    outcome = self.desktop.execute(target_scope, operation, node, value)
                    steps += 1
                    turn = {
                        "step": steps,
                        "operation": operation,
                        "argv": outcome.get("argv"),
                        "target": node["role"] + (f' "{node["name"]}"' if node.get("name") else "")
                        if node
                        else None,
                        "ref": node.get("ref_id") if node else None,
                        "confidence": target["confidence"],
                        "ok": outcome["ok"],
                        "delivery": outcome.get("delivery"),
                        "steps": outcome.get("steps", []),
                        "post_state": outcome.get("post_state"),
                        "details": outcome.get("details"),
                        "error": outcome.get("error"),
                        "route": outcome.get("route"),
                        "truncated": space["truncated"],
                        "changed": None,
                    }
                    if operation in TEXT_OPERATIONS:
                        turn["text_source"] = "text_model" if generate_text else "supplied"
                    if selection:
                        turn["selection"] = selection
                        turn["text_source"] = "observed_option"
                    turn = redact(turn, supply.known)
                    history.append(turn)
                    try:
                        self.sleep(1)
                    except Exception:
                        yield {"turn": turn, "screen": screen}
                        raise
                    if not outcome["ok"]:
                        yield {"turn": turn, "screen": screen}
                        yield {
                            "stop": "action_failed",
                            "screen": screen,
                            "error": outcome.get("error"),
                            "history": history,
                        }
                        return
                    if "root" in outcome:
                        scope = replace(scope, root=outcome["root"])
                        turn["changed"] = True
                    else:
                        try:
                            after = self.desktop.observe(scope)
                        except DesktopError as error:
                            stop = WINDOW_STOPS.get(error.code) if scope.window_id else None
                            if stop is None:
                                yield {"turn": turn, "screen": screen}
                                raise
                            turn["changed"] = True
                            yield {"turn": turn, "screen": screen}
                            yield {"stop": stop, "screen": screen, "history": history}
                            return
                        turn["changed"] = fingerprint(after.nodes) != before
                    yield {"turn": turn, "screen": screen}
                    recent = history[-self.limits.stalled_turns :]
                    if len(recent) == self.limits.stalled_turns and all(
                        h["changed"] is False and h["operation"] != "WAIT" for h in recent
                    ):
                        yield {
                            "stop": f"stopped after {self.limits.stalled_turns} turns that changed nothing",
                            "screen": screen,
                            "history": history,
                        }
                        return
            except BudgetStop as error:
                yield {"stop": str(error), "screen": last, "history": history}

    def _generate_text(self, model, request, supply):
        try:
            value = model.generate(request)
        except TextModelError:
            raise
        except Exception:
            raise TextModelError("text_model failed; no text was entered") from None
        if isinstance(value, str) and value.strip():
            supply.known.add(value)
        return value

    def _bootstrap(self, goal, ask):
        apps = self.desktop.applications()
        for page in range((len(apps) + 249) // 250):
            request = launch_request(goal, apps, page)
            answer = validate_choice(
                ask(request).get("answers", {}).get("application"),
                request["questions"]["application"]["criteria"],
            )
            choice = answer["choice"]
            if choice == "NEXT_PAGE":
                continue
            if choice in ("DONE", "BLOCKED"):
                yield {
                    "stop": choice.lower(),
                    "screen": {"surface": "desktop"},
                    "confidence": answer["confidence"],
                }
                return None
            app = apps[int(choice[4:])]
            launched = self.desktop.launch(app)
            self.sleep(1)
            yield {
                "turn": {
                    "step": 0,
                    "operation": "LAUNCH",
                    "target": app["name"],
                    "confidence": answer["confidence"],
                    **launched,
                },
                "screen": {"surface": "desktop"},
            }
            if not launched["ok"]:
                yield {
                    "stop": "action_failed",
                    "screen": {"surface": "desktop"},
                    "error": launched.get("error"),
                }
                return None
            window = self.desktop.wait_for_window(launched)
            return Scope(app=window["app_name"], window_id=window["id"])
        raise DesktopError("No application was selected from the installed catalog")
