"""Goal loop with injected desktop, decision model, budgets, and clock delay."""

import copy
import time
from dataclasses import replace

from .actions import TEXT_OPERATIONS, extend_space
from .contracts import DecisionModel, Desktop, DesktopError, Limits, RunOptions, Scope, TextModelError
from .event_observation import turn_event
from .focus_context import activation_key, reported_focus
from .goal_scope import filter_rejections, preparation_operation, review_request, state_key
from .modal_recovery import mark_modal_refusal, release_modal_refusals
from .native_intent import filter_intents, intent, matches
from .option_activation import activation_node
from .pointer_recovery import POINTER_OPERATIONS, shared_receiver_refusal
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
from .popup_recovery import needs_popup_observation, observed_popup
from .progress import completion_request, milestone
from .screen import (
    describe,
    fingerprint,
    target_identity,
)
from .selection import choose_option
from .selection_recovery import (
    changed_selection_context,
    input_changed,
    needs_destroyed_target_observation,
    needs_selection_observation,
    needs_value_observation,
    same_complete_scope,
)
from .startup_observation import empty_document
from .target_pages import add_page_history, target_page
from .text_request import (
    build_text_request,
    capture_text_state,
    offer_text_model,
    same_text_window,
    unchanged_text_target,
)
from .window_memory import attach as attach_window_memory
from .window_memory import closed_window
from .window_navigation import extend_windows, switch_window

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


def owned_dialog(desktop, scope, parents):
    child = desktop.owned_dialog(scope)
    if child is not None and (
        len(parents) >= 8 or child.window_id in {p.window_id for p in [*parents, scope]}
    ):
        raise DesktopError("Native dialog ownership would cycle or exceed eight levels", "AMBIGUOUS_WINDOW")
    return child


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
                for key in ("screen", "after_screen"):
                    if key not in event:
                        continue
                    context = event[key].pop("context", [])
                    if options.include_context:
                        event[key]["visible_context"] = [
                            {k: v for k, v in n.items() if options.values or k != "value"}
                            for n in context[:200]
                        ]
                        event[key]["context_truncated"] = len(context) > 200
                yield redact(event, supply.known)
        except Exception as error:
            if isinstance(error, TextModelError):
                raise TextModelError(redact(str(error), supply.known)) from None
            raise DesktopError(redact(str(error), supply.known), getattr(error, "code", None)) from None
        finally:
            events.close()

    def _run(self, goal, options, supply):
        calls, steps, history = 0, 0, []
        text_replans = 0
        startup_rereads = 0
        text_feedback = []
        known_transitions = []
        blocked_intents = []
        replan_state = None
        cancelled_routes, unchanged_selections, route_feedback, milestones = [], [], [], []
        terminal_feedback = {}
        parent_scopes, rejected_scopes, scope_reviews = [], [], []
        parent_kinds = []
        closed_windows = []
        scope, last = options.scope, {}

        def remember_closed(scope, screen, action=None):
            if options.remember_closed_windows:
                past = closed_window(scope, screen, action, values=options.values)
                if past is not None:
                    closed_windows.append(past)
                    del closed_windows[:-4]

        def consume_call():
            nonlocal calls
            if calls >= self.limits.calls:
                raise BudgetStop(f"stopped at the {self.limits.calls} model call budget")
            calls += 1

        def ask(request):
            consume_call()
            if route_feedback or terminal_feedback or milestones:
                request = {**request, "state": dict(request["state"])}
                if route_feedback:
                    request["state"]["route_feedback"] = route_feedback[-6:]
                if terminal_feedback:
                    request["state"]["terminal_feedback"] = list(terminal_feedback.values())
                if milestones:
                    request["state"]["observed_milestones"] = milestones[:]
            attach_window_memory(request, closed_windows)
            return self.model.ask(request)

        def observe_before_dispatch(target_scope, unused_value=None):
            nonlocal scope, options
            try:
                return self.desktop.observe(target_scope), False
            except DesktopError as error:
                if unused_value is not None:
                    supply.return_unconsumed(unused_value)
                if parent_scopes and error.code == "WINDOW_NOT_FOUND":
                    remember_closed(target_scope, last)
                    scope = parent_scopes.pop()
                    options = release_modal_refusals(options, scope)
                    yield {"recovery": parent_kinds.pop(), "scope": scope.__dict__, "screen": last}
                    return None, False
                stop = WINDOW_STOPS.get(error.code) if target_scope.window_id else None
                if stop is None:
                    raise
                yield {"stop": stop, "screen": last, "history": history}
                return None, True

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
                        if parent_scopes and error.code == "WINDOW_NOT_FOUND":
                            remember_closed(scope, last)
                            scope = parent_scopes.pop()
                            options = release_modal_refusals(options, scope)
                            yield {"recovery": parent_kinds.pop(), "scope": scope.__dict__, "screen": last}
                            continue
                        if options.follow_owned_dialogs and scope.window_id:
                            child = owned_dialog(self.desktop, scope, parent_scopes)
                            if child is not None:
                                parent_scopes.append(scope)
                                parent_kinds.append("owned_dialog_closed")
                                scope = child
                                yield {
                                    "recovery": "follow_owned_dialog",
                                    "scope": scope.__dict__,
                                    "screen": last,
                                }
                                continue
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
                    if steps == 0 and startup_rereads < options.startup_observation_retries and empty_document(seen):
                        startup_rereads += 1
                        yield {"diagnostic": "empty_startup_document", "attempt": startup_rereads, "screen": screen}
                        self.sleep(0.5)
                        continue
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
                    before = copy.deepcopy((fingerprint(nodes), screen.get("window"), screen.get("surface"), screen.get("context", [])))
                    owner = (screen.get("app"), screen.get("window_id"), scope.root)
                    complete_window = (
                        bool(owner[0] and owner[1]) and screen.get("complete") is True
                        and not screen.get("unclassified_surfaces")
                    )
                    current_state = (owner, complete_window, before)
                    if current_state != replan_state:
                        replan_state = current_state
                        cancelled_routes.clear()
                        unchanged_selections.clear()
                        route_feedback.clear()
                        terminal_feedback.clear()
                    generate_text = options.text_model is not None and not supply.available()
                    space = action_space(
                        nodes,
                        drillable=not scope.root,
                        typable=supply.available() or generate_text,
                        values=options.values,
                        headed=screen.get("headed_text", False),
                        max_elements=max(1, len(nodes)),
                    )
                    extend_space(space, screen, options)
                    live_scope = replace(scope, window_id=scope.window_id or screen.get("window_id"))
                    filter_intents(space, live_scope, seen.nodes, blocked_intents + unchanged_selections)
                    if options.follow_application_windows and not scope.root:
                        extend_windows(space, self.desktop.application_windows(scope), options)
                    if options.review_goal_scope or options.replan_no_effect or options.replan_safe_failures:
                        filter_rejections(space, seen, rejected_scopes + cancelled_routes)
                    cycle_feedback = []
                    if options.replan_no_effect and screen.get("complete") is True and not screen.get("unclassified_surfaces"):
                        recurrent = [
                            item for item in known_transitions
                            if item["before"] == before
                            and item["owner"] == owner
                        ]
                        filter_rejections(space, seen, [item["key"] for item in recurrent])
                        filter_intents(space, live_scope, seen.nodes, [
                            item["intent"] for item in known_transitions
                            if item.get("intent") and item["before"] == before
                            and item["owner"] == owner
                        ])
                        cycle_feedback = [
                            {"operation": item["key"]["operation"], "target": item["key"]["target"],
                             "result": item.get("reason") or "This operation already returned an acknowledged outcome in this same complete UI state. "
                             "The repeated transition or already-confirmed focus is withheld. "
                             "Choose another observed route; this does not establish task completion."}
                            for item in recurrent
                        ]
                    observed_space, page = space, 0
                    page_history = []
                    while True:
                        page_history.append(page)
                        space = target_page(observed_space, page, values=options.values)
                        request = build_request(goal, screen, space, history, values=options.values, compact=True)
                        add_page_history(request, page_history)
                        if scope_reviews:
                            request["state"]["goal_scope_reviews"] = scope_reviews[-10:]
                        if text_feedback:
                            request["state"]["text_generation_feedback"] = text_feedback[-3:]
                        if cycle_feedback:
                            request["state"]["cycle_feedback"] = cycle_feedback
                        if generate_text:
                            offer_text_model(request)
                        else:
                            request["state"]["text_input"] = supply.preview(values=options.values)
                        target = validate_choice(
                            ask(request).get("answers", {}).get("target"),
                            request["questions"]["target"]["criteria"],
                        )
                        if target["choice"] not in ("NEXT_PAGE", "PREVIOUS_PAGE"):
                            break
                        page += 1 if target["choice"] == "NEXT_PAGE" else -1
                    selected = space["by_target"].get(target["choice"])
                    operation = target["choice"]
                    if options.follow_owned_dialogs and operation in {
                        "WAIT",
                        "DONE",
                        "BLOCKED",
                        "INFEASIBLE",
                    }:
                        child = owned_dialog(self.desktop, scope, parent_scopes)
                        if child is not None:
                            parent_scopes.append(scope)
                            parent_kinds.append("owned_dialog_closed")
                            scope = child
                            yield {
                                "recovery": "follow_owned_dialog",
                                "scope": scope.__dict__,
                                "screen": screen,
                            }
                            continue
                    if operation in ("DONE", "BLOCKED", "INFEASIBLE"):
                        stop = operation.lower()
                        completion_evidence = None
                        if options.verify_terminal:
                            alternatives = bool(observed_space["by_target"] or scope.root)
                            if operation == "DONE":
                                evidence = terminal_feedback.get("DONE")
                                if evidence is None:
                                    choice = "unverified"
                                    if complete_window:
                                        question = completion_request(request["state"])
                                        answer = ask(question).get("answers", {}).get("completion")
                                        choice = validate_choice(
                                            answer, question["questions"]["completion"]["criteria"]
                                        )["choice"]
                                    evidence = {"proposal": "DONE", "assessment": choice}
                                    yield {"diagnostic": "completion_review", "verification": evidence, "screen": screen}
                                    if choice != "satisfied" and alternatives:
                                        evidence["guidance"] = (
                                            "Completion lacks supporting evidence in this state. Advance an unmet "
                                            "condition or obtain the needed readback before proposing DONE again."
                                        )
                                        terminal_feedback["DONE"] = evidence
                                        yield {"recovery": "replan_unverified_completion", "screen": screen}
                                        continue
                                completion_evidence = evidence
                                if evidence["assessment"] != "satisfied":
                                    stop = "completion_unverified"
                            elif complete_window and alternatives and "obstruction" not in terminal_feedback:
                                terminal_feedback["obstruction"] = {
                                    "proposal": operation,
                                    "guidance": (
                                        "Check other observed candidates and scoped keyboard/navigation routes. "
                                        "An unavailable option or refused pointer route does not exhaust the window."
                                    ),
                                }
                                yield {"recovery": "reconsider_terminal_obstruction", "screen": screen}
                                continue
                        yield {
                            "stop": stop,
                            **({"verification": completion_evidence} if completion_evidence else {}),
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
                        if scope_reviews:
                            request["state"]["goal_scope_reviews"] = scope_reviews[-10:]
                        if generate_text:
                            offer_text_model(request)
                        else:
                            request["state"]["text_input"] = supply.preview(values=options.values)
                        answer = validate_choice(
                            ask(request).get("answers", {}).get("operation"), selected["operations"]
                        )
                        operation = answer["choice"]
                        if operation in ("SELECT", "ACTIVATE_OPTION"):
                            choices = selected["selection_options"] if operation == "SELECT" else selected["activation_options"]
                            can_replan = options.replan_no_effect and complete_window
                            selection = choose_option(
                                ask, request, choices, activate=operation == "ACTIVATE_OPTION", replan=can_replan
                            )
                            if selection is None:
                                if can_replan:
                                    cancelled_routes.append(state_key(seen, operation, node))
                                    feedback = {
                                        "operation": operation,
                                        "target": criterion(node, target["choice"], values=options.values),
                                        "result": "No useful observed option was chosen. No input was sent. "
                                        "Choose another route; inspect window keyboard navigation when appropriate.",
                                    }
                                    route_feedback.append(feedback)
                                    yield {"recovery": "replan_cancelled_selection", "feedback": feedback, "screen": screen}
                                    continue
                                yield {"stop": "selection_cancelled", "screen": screen, "history": history}
                                return
                            if operation == "ACTIVATE_OPTION":
                                node = activation_node(node, selection)
                                request["state"]["selected_option"] = selection
                        elif operation == "SELECT_ITEM":
                            owner = node.get("selection_container")
                            label = owner["label"] if owner else node["name"]
                            selection = {"label": label, "role": node["role"], "ref": node["ref_id"]}
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
                        if operation == "SWITCH_WINDOW":
                            destination = switch_window(self.desktop, scope, node)
                            if destination is None:
                                yield {"recovery": "application_window_changed", "screen": screen}
                                continue
                            ancestors = [
                                i
                                for i, parent in enumerate(parent_scopes)
                                if parent.window_id == destination.window_id
                            ]
                            if ancestors:
                                del parent_scopes[ancestors[-1] :]
                                del parent_kinds[ancestors[-1] :]
                            else:
                                if len(parent_scopes) >= 8:
                                    yield {
                                        "stop": "application window navigation depth exceeded",
                                        "screen": screen,
                                    }
                                    return
                                parent_scopes.append(scope)
                                parent_kinds.append("application_window_closed")
                            scope = destination
                            steps += 1
                            navigation_turn = {
                                "step": steps,
                                "operation": operation,
                                "target": node.get("name"),
                                "ok": True,
                                "delivery": "looked",
                                "changed": True,
                            }
                            history.append(navigation_turn)
                            yield {
                                "turn": navigation_turn,
                                "scope": scope.__dict__,
                                "screen": screen,
                            }
                            continue
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
                    native_intent = intent(target_scope, operation, node, value, seen.nodes)
                    cycle_intent = {**native_intent, "exact_state": True} if native_intent else None
                    if any(matches(previous, native_intent) for previous in blocked_intents) or any(
                        blocked.get("target") in (target_name, "*")
                        and blocked.get("window_id") in (None, target_scope.window_id)
                        and (
                            not blocked.get("destination_identity")
                            or (
                                operation.startswith("DRAG_TO:")
                                and blocked.get("source_identity") == target_identity(node)
                                and blocked["destination_identity"]
                                == target_identity(node["drag_candidates"][operation.partition(":")[2]])
                            )
                        )
                        and (
                            blocked.get("operation") in (operation, "*")
                            or (
                                str(blocked.get("operation", "")).startswith("DRAG_TO:")
                                and operation.startswith("DRAG_TO:")
                                and not blocked.get("destination_identity")
                            )
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
                                    reference_context=copy.deepcopy(options.reference_context),
                                ),
                                supply,
                            )
                        else:
                            value = supply.take(describe(node, True), history)
                    history = redact(history, supply.known)
                    if operation in TEXT_OPERATIONS and (not isinstance(value, str) or not value.strip()):
                        if (
                            value is None
                            and generate_text
                            and options.replan_safe_failures
                            and text_replans < options.text_replans
                        ):
                            text_replans += 1
                            for declined in TEXT_OPERATIONS:
                                rejected_scopes.append(state_key(seen, declined, node))
                            feedback = {
                                "target": target_name,
                                "operation": operation,
                                "result": "The text model supplied no value. No input was dispatched. "
                                "Choose another target or operation to advance the goal; do not invent field content.",
                            }
                            text_feedback.append(feedback)
                            yield {"recovery": "replan_declined_text", "feedback": feedback, "screen": screen}
                            continue
                        yield {
                            "stop": f"no value was supplied for {criterion(node, '?')}",
                            "screen": screen,
                            "history": history,
                        }
                        return
                    review_state = capture_text_state(seen)
                    fresh = None
                    unused_value = value if operation in TEXT_OPERATIONS and not generate_text else None
                    if options.review_goal_scope and operation.startswith("PRESS_KEY:") and preparation_operation(operation, node):
                        fresh, finished = yield from observe_before_dispatch(target_scope)
                        if fresh is None:
                            if finished:
                                return
                            continue
                        if not same_complete_scope(seen, fresh) or reported_focus(seen.screen) != reported_focus(fresh.screen):
                            yield {"recovery": "replan_changed_keyboard_focus", "screen": fresh.screen}
                            continue
                    if options.review_goal_scope and not preparation_operation(operation, node):
                        reviewed = validate_choice(
                            ask(review_request(request, operation, node, value))
                            .get("answers", {})
                            .get("goal_scope"),
                            ("within_request", "broader_or_unproven"),
                        )
                        review = {
                            "operation": operation,
                            "target": target_name,
                            "choice": reviewed["choice"],
                            "confidence": reviewed["confidence"],
                        }
                        scope_reviews.append(review)
                        yield {"diagnostic": "goal_scope_review", "review": review, "screen": screen}
                        if reviewed["choice"] != "within_request":
                            if operation in TEXT_OPERATIONS and not generate_text:
                                supply.return_unconsumed(value)
                            rejected_operations = ("TYPE_TEXT", "INSERT_TEXT", "CLEAR") if operation in {*TEXT_OPERATIONS, "CLEAR"} else (operation,)
                            for rejected_operation in rejected_operations:
                                rejected_scopes.append(state_key(seen, rejected_operation, node))
                            yield {"recovery": "replan_operation_scope", "review": review, "screen": screen}
                            continue
                        fresh, finished = yield from observe_before_dispatch(target_scope, unused_value)
                        if fresh is None:
                            if finished:
                                return
                            continue
                        if operation not in TEXT_OPERATIONS and capture_text_state(fresh) != review_state:
                            yield {"recovery": "replan_changed_review_context", "screen": fresh.screen}
                            continue
                    if operation in TEXT_OPERATIONS and generate_text:
                        target_scope = replace(
                            target_scope, window_id=scope.window_id or screen.get("window_id")
                        )
                        if fresh is None:
                            fresh, finished = yield from observe_before_dispatch(target_scope)
                            if fresh is None:
                                if finished:
                                    return
                                continue
                        if capture_text_state(fresh) != text_state:
                            same_target = unchanged_text_target(seen, fresh, node)
                            can_replan = same_target or (
                                options.replan_changed_text_targets and same_text_window(seen, fresh)
                            )
                            if text_replans < options.text_replans and can_replan:
                                text_replans += 1
                                yield {
                                    "recovery": "discard_text_and_replan",
                                    "screen": fresh.screen,
                                    "reason": (
                                        "Context changed while the selected field remained unchanged"
                                        if same_target
                                        else "The field changed before delivery; discard text and select again"
                                    ),
                                    "text_replans": text_replans,
                                }
                                continue
                            yield {
                                "stop": "screen_changed_during_text_generation",
                                "screen": fresh.screen,
                                "history": history,
                            }
                            return
                    if (
                        operation in TEXT_OPERATIONS
                        and not generate_text
                        and fresh is not None
                        and capture_text_state(fresh) != review_state
                    ):
                        yield {
                            "stop": "screen_changed_during_scope_review",
                            "screen": fresh.screen,
                            "history": history,
                        }
                        return
                    if options.replan_no_effect and operation not in {"WAIT", "DRILL", "WIDEN"}:
                        if fresh is None:
                            fresh, finished = yield from observe_before_dispatch(target_scope, unused_value)
                            if fresh is None:
                                if finished:
                                    return
                                continue
                        if capture_text_state(fresh) != review_state:
                            if unused_value is not None:
                                supply.return_unconsumed(unused_value)
                            yield {"recovery": "replan_changed_action_context", "screen": fresh.screen}
                            continue
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
                    if "semantic_attempt" in outcome:
                        turn["semantic_attempt"] = outcome["semantic_attempt"]
                    if "focus_evidence" in outcome:
                        turn["focus_evidence"] = outcome["focus_evidence"]
                    if operation in TEXT_OPERATIONS:
                        turn["text_source"] = "text_model" if generate_text else "supplied"
                        turn["verification_scope"] = (outcome.get("details") or {}).get(
                            "verification_scope", "native_action"
                        )
                        turn["application_commit"] = (outcome.get("details") or {}).get(
                            "application_commit", "not_verified"
                        )
                    if selection:
                        turn["selection"] = selection
                        turn["text_source"] = "observed_option"
                    turn = redact(turn, supply.known)
                    history.append(turn)
                    try:
                        self.sleep(1)
                    except Exception:
                        yield turn_event(turn, screen)
                        raise
                    if not outcome["ok"]:
                        yield turn_event(turn, screen)
                        if (
                            options.reconcile_observed_popups and operation.startswith("OPEN_RESOURCE:")
                            and outcome.get("delivery") == "delivered_unverified"
                        ):
                            # A launcher can hand off and exit. Never replay its acknowledged request.
                            fresh = self.desktop.observe(target_scope)
                            if (
                                seen.screen.get("complete") is True and fresh.screen.get("complete") is True
                                and not seen.screen.get("unclassified_surfaces")
                                and not fresh.screen.get("unclassified_surfaces")
                                and all(seen.screen.get(k) == fresh.screen.get(k) for k in ("app", "window_id", "root", "surface"))
                                and (fingerprint(seen.nodes) != fingerprint(fresh.nodes)
                                     or seen.screen.get("window") != fresh.screen.get("window"))
                            ):
                                blocked = {"operation": operation, "target": "*", "window_id": None}
                                options = replace(options, blocked_actions=(*options.blocked_actions, blocked))
                                yield {"recovery": "observe_after_resource_handoff", "screen": fresh.screen,
                                       "blocked_action": blocked,
                                       "verification": "Observed UI changed; resource identity and task completion remain unverified"}
                                continue
                        if (
                            options.replan_no_effect
                            and options.replan_safe_failures
                            and screen.get("complete") is True
                            and screen.get("app")
                            and screen.get("window_id")
                            and not screen.get("unclassified_surfaces")
                            and shared_receiver_refusal(outcome, operation)
                        ):
                            for alias in POINTER_OPERATIONS:
                                known_transitions.append({
                                    "before": before,
                                    "owner": (screen.get("app"), screen.get("window_id"), scope.root),
                                    "key": state_key(seen, alias, node),
                                    "reason": "The native tool proved that no input was sent because this receiver's geometry or sibling separation was unproven. "
                                    "Physical actions on this same unchanged target are withheld. Semantic focus and window keys remain available; inspect their reported receiver.",
                                })
                            yield {
                                "recovery": "reobserve_after_unproven_pointer",
                                "withheld_operations": list(POINTER_OPERATIONS),
                                "screen": screen,
                            }
                            continue
                        selection_read = needs_selection_observation(outcome, operation)
                        value_read = needs_value_observation(outcome, operation)
                        destroyed_read = needs_destroyed_target_observation(outcome, operation)
                        if options.reconcile_observed_popups and (selection_read or value_read or destroyed_read):
                            read_kind = "selection" if selection_read else "destroyed_target" if destroyed_read else "value"
                            try:
                                fresh = self.desktop.observe(target_scope)
                                evidence = input_changed(seen, fresh)
                                if not evidence and (selection_read or destroyed_read):
                                    evidence = changed_selection_context(seen, fresh)
                            except DesktopError:
                                fresh, evidence = None, None
                            yield {
                                "diagnostic": "observation_after_unverified_" + read_kind,
                                "screen": fresh.screen if fresh else screen,
                                "reconciled": bool(evidence),
                            }
                            if evidence:
                                if native_intent:
                                    blocked_intents.append(native_intent)
                                blocked = {"operation": operation, "target": target_name, "window_id": target_scope.window_id}
                                options = replace(options, blocked_actions=(*options.blocked_actions, blocked))
                                yield {
                                    "recovery": "observed_input_change_after_" + read_kind,
                                    "blocked_action": blocked,
                                    "evidence": evidence,
                                    "screen": fresh.screen,
                                }
                                continue
                        if options.reconcile_observed_popups and needs_popup_observation(
                            outcome, operation, node
                        ):
                            fresh = self.desktop.observe(target_scope)
                            evidence = observed_popup(seen, fresh, node)
                            yield {
                                "diagnostic": "observation_after_unverified_opener",
                                "screen": fresh.screen,
                                "reconciled": bool(evidence),
                            }
                            if evidence:
                                blocked = {
                                    "operation": "*",
                                    "target": target_name,
                                    "window_id": target_scope.window_id,
                                }
                                blocked = mark_modal_refusal(blocked, outcome)
                                options = replace(
                                    options, blocked_actions=(*options.blocked_actions, blocked)
                                )
                                yield {
                                    "recovery": "observed_popup_after_unverified_opener",
                                    "blocked_action": blocked,
                                    "evidence": evidence,
                                    "screen": fresh.screen,
                                }
                                continue
                        if options.follow_owned_dialogs:
                            child = owned_dialog(self.desktop, target_scope, parent_scopes)
                            if child is not None:
                                blocked = {
                                    "operation": "*",
                                    "target": target_name,
                                    "window_id": target_scope.window_id,
                                }
                                blocked = mark_modal_refusal(blocked, outcome)
                                options = replace(
                                    options, blocked_actions=(*options.blocked_actions, blocked)
                                )
                                parent_scopes.append(target_scope)
                                parent_kinds.append("owned_dialog_closed")
                                scope = child
                                yield {
                                    "recovery": "follow_owned_dialog_after_failed_action",
                                    "scope": scope.__dict__,
                                    "blocked_action": blocked,
                                    "screen": screen,
                                }
                                continue
                        disposition = (outcome.get("error") or {}).get("disposition", {})
                        if (
                            options.replan_safe_failures
                            and operation in TEXT_OPERATIONS
                            and generate_text
                            and text_replans < options.text_replans
                            and outcome.get("delivery") == "not_delivered"
                            and disposition.get("delivery") == "not_delivered"
                            and disposition.get("retry") == "safe"
                        ):
                            text_replans += 1
                            for refused in (*TEXT_OPERATIONS, "CLEAR"):
                                rejected_scopes.append(state_key(seen, refused, node))
                            feedback = {
                                "target": target_name,
                                "operation": operation,
                                "result": "The native tool proved that no text input was dispatched. "
                                "This field's text operations are withheld for the unchanged state. "
                                "Choose another observed route; any later text must be generated afresh.",
                                "native_error": outcome.get("error"),
                            }
                            text_feedback.append(feedback)
                            yield {
                                "recovery": "replan_after_not_delivered_text",
                                "feedback": feedback,
                                "screen": screen,
                            }
                            continue
                        if (
                            options.replan_safe_failures
                            and operation not in TEXT_OPERATIONS
                            and outcome.get("delivery", disposition.get("delivery")) == "not_delivered"
                            and disposition.get("delivery") == "not_delivered"
                            and disposition.get("retry") == "safe"
                        ):
                            blocked = {
                                "operation": operation,
                                "target": target_name,
                                "window_id": target_scope.window_id,
                            }
                            blocked = mark_modal_refusal(blocked, outcome)
                            if operation.startswith("DRAG_TO:"):
                                blocked.update(
                                    source_identity=target_identity(node),
                                    destination_identity=target_identity(
                                        node["drag_candidates"][operation.partition(":")[2]]
                                    ),
                                )
                            if blocked.get("native_modal_refusal") is True:
                                options = replace(options, blocked_actions=(*options.blocked_actions, blocked))
                            else:
                                rejected_scopes.append(state_key(seen, operation, node))
                            yield {
                                "recovery": "reobserve_after_not_delivered",
                                "blocked_action": blocked,
                                "screen": screen,
                            }
                            continue
                        yield {
                            "stop": "action_failed",
                            "screen": screen,
                            "error": outcome.get("error"),
                            "history": history,
                        }
                        return
                    after = None
                    selection_withheld = False
                    if "root" in outcome:
                        scope = replace(scope, root=outcome["root"])
                        turn["changed"] = True
                    else:
                        try:
                            after = self.desktop.observe(scope)
                        except DesktopError as error:
                            if parent_scopes and error.code == "WINDOW_NOT_FOUND":
                                remember_closed(scope, screen, turn)
                                turn["changed"] = True
                                yield turn_event(turn, screen)
                                scope = parent_scopes.pop()
                                options = release_modal_refusals(options, scope)
                                yield {
                                    "recovery": parent_kinds.pop(),
                                    "scope": scope.__dict__,
                                    "screen": screen,
                                }
                                continue
                            stop = WINDOW_STOPS.get(error.code) if scope.window_id else None
                            if stop is None:
                                yield turn_event(turn, screen)
                                raise
                            turn["changed"] = True
                            yield turn_event(turn, screen)
                            yield {"stop": stop, "screen": screen, "history": history}
                            return
                        turn["changed"] = (
                            fingerprint(after.nodes),
                            after.screen.get("window"),
                            after.screen.get("surface"),
                            after.screen.get("context", []),
                        ) != before
                        if options.verify_terminal:
                            mark = milestone(turn, seen, after, values=options.values)
                            if mark:
                                milestones.append(mark)
                                del milestones[:-12]
                        if (
                            options.replan_no_effect
                            and operation in {"SELECT", "SELECT_ITEM"}
                            and not turn["changed"]
                            and outcome.get("delivery") == "delivered_verified"
                            and cycle_intent and cycle_intent["receiver"] is not None
                            and same_complete_scope(seen, after)
                        ):
                            unchanged_selections.append(cycle_intent)
                            selection_withheld = True
                            feedback = {
                                "operation": operation,
                                "target": target_name,
                                "result": "This exact selection was verified but left the same complete UI unchanged. "
                                "Other choices remain available. Inspect the selected item or use a different navigation route.",
                            }
                            route_feedback.append(feedback)
                            yield {"diagnostic": "verified_selection_without_observed_change", "feedback": feedback, "screen": after.screen}
                        known_focus = (
                            operation == "FOCUS_WINDOW"
                            and outcome.get("focus_evidence") == {"window_id": screen.get("window_id"), "is_focused": True}
                        ) or (
                            operation == "FOCUS"
                            and "focused" in node.get("states", [])
                            and outcome.get("delivery") in {"not_delivered", "delivered_verified", "delivered_unverified"}
                        )
                        changed_click = (
                            turn["changed"]
                            and operation in {"CLICK", "POINTER_CLICK", "ACTIVATE_OPTION"}
                            and outcome.get("delivery") == "delivered_unverified"
                        )
                        unverified_focus = operation == "FOCUS" and outcome.get("delivery") == "delivered_unverified"
                        if (
                            options.replan_no_effect
                            and (changed_click or (known_focus or unverified_focus) and not turn["changed"])
                            and seen.screen.get("complete") is True
                            and after.screen.get("complete") is True
                            and not screen.get("unclassified_surfaces")
                            and not after.screen.get("unclassified_surfaces")
                            and after.screen.get("window_id") == screen.get("window_id")
                        ):
                            for alias in (("CLICK", "POINTER_CLICK", "ACTIVATE_OPTION") if changed_click else (operation,)):
                                known_transitions.append({
                                    "before": before,
                                    "owner": (screen.get("app"), screen.get("window_id"), scope.root),
                                    "key": state_key(seen, alias, node),
                                    "intent": cycle_intent,
                                    "reason": (
                                        "The native tool acknowledged a focus request but did not verify target focus. "
                                        "Repeating this request on the same unchanged screen is withheld; choose another observed route."
                                    ) if unverified_focus else None,
                                })
                        if (
                            options.replan_no_effect
                            and not turn["changed"]
                            and seen.screen.get("complete") is True
                            and after.screen.get("complete") is True
                            and not screen.get("unclassified_surfaces")
                            and not after.screen.get("unclassified_surfaces")
                            and after.screen.get("window_id") == screen.get("window_id")
                            and (operation in {"CLICK", "POINTER_CLICK", "ACTIVATE_OPTION"} or activation_key(operation, node))
                            and outcome.get("delivery") == "delivered_unverified"
                        ):
                            known_transitions.append({
                                "before": before,
                                "owner": (screen.get("app"), screen.get("window_id"), scope.root),
                                "key": state_key(seen, operation, node),
                                "intent": cycle_intent,
                            })
                            yield {
                                "diagnostic": "unverified_activation_without_observed_change" if activation_key(operation, node) else "unverified_click_without_observed_change",
                                "operation": operation,
                                "target": target_name,
                                "screen": after.screen,
                            }
                    yield turn_event(turn, screen, after.screen if after is not None else None)
                    recent = history[-self.limits.stalled_turns :]
                    if not selection_withheld and len(recent) == self.limits.stalled_turns and all(
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
