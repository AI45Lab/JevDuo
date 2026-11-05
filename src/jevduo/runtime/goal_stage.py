"""Goal-stage loop: receipts/observations each step, one semantic verifier at stage end."""

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ..gui.goal_candidates import (
    application_mismatch,
    foreground_tree,
    goal_candidates,
    launcher_input,
    portal_needs_grounding,
)
from ..gui.goal_candidates import (
    document_mismatch as _document_mismatch,
)
from ..gui.interaction import target_effect as _target_effect
from ..gui.interaction import visual_changed as _visual_changed
from ..gui.interaction import visual_identity
from ..memory.context import execution_context
from ..models.ranking import ranking_dict
from ..models.slow import ModelResponseError
from ..protocol.models import CliAction, FailAction
from .episode import handle_done, resolve_done_probe


class ObservationRequired(Exception):
    """Refresh without executing/replaying an action or consuming an action budget."""


def _state_key(targets):
    return tuple(sorted((t.role, t.name, t.x, t.y, t.value, bool(t.focused), str(t.checked), str(t.selected))
                        for t in _progress_targets(targets)))


def _progress_targets(targets):
    # Shell display text (clock/battery) is not task progress. Keep shell controls and focused input.
    return [t for t in targets if not (
        (t.application == "gnome-shell" or "gnome-shell" in t.parent_context)
        and t.role in {"text", "label", "static"} and not t.focused)]


def _loop_state(targets, tree):
    root = ET.fromstring(tree or "<empty/>")
    return (root.get("foreground_title", ""), root.get("foreground_class", ""),
            tuple(sorted((t.role, t.semantic_key if "dom" in t.source else _control_name(t),
                          0 if "dom" in t.source else t.x, 0 if "dom" in t.source else t.y,
                          "" if t.role == "slider" else t.value,
                          str(t.checked)) for t in _progress_targets(targets)
                         if t.application != "gnome-shell" and "gnome-shell" not in t.parent_context)),
            portal_needs_grounding(tree))


def _control_name(target):
    if target.role != "page_tab":
        return target.name
    name = re.sub(r"\s+- Memory usage - .+$", "", target.name)
    return re.sub(r"\s*[–-]\s*(?:Audio playing|Video playing in picture-in-picture mode)$", "", name)


def _cycle_actions(recent, state):
    for period in (2, 3, 4):
        if len(recent) < period * 2:
            continue
        first, second = recent[-period * 2:-period], recent[-period:]
        if first == second and any(a != first[0] for a in first):
            return {a["key"] for a in second if a["state"] == state}
    return set()


def _action_key(candidate, targets=()):
    target = next((t for t in targets if t.id == candidate.target_id), None)
    name = ((target.semantic_key if "dom" in target.source else
             f"{target.role}|{_control_name(target)}|{target.x}|{target.y}") if target else
            visual_identity(candidate) or candidate.metadata.get("semantic_key") or candidate.description)
    if candidate.operation == "keypress":
        name += repr([(t.role, t.name, t.x, t.y) for t in targets if t.focused])
    parameters = {k: candidate.metadata[k]
                  for k in ("text", "input_mode", "fraction", "key_sequence", "visual_point")
                  if k in candidate.metadata and not (k == "visual_point" and visual_identity(candidate))}
    return (candidate.operation, name, json.dumps(parameters, sort_keys=True, ensure_ascii=False))


def _navigation_key(candidate, target, work, state):
    if candidate.operation == "scroll" or candidate.id in {"KEYPRESS_PAGE_UP", "KEYPRESS_PAGE_DOWN"}:
        return (work, state[1], "browse", "up" if "UP" in candidate.id.upper() else "down")
    if candidate.source == "a11y_menu":
        return (work, state[1], "menu_navigation", candidate.description)
    if target and target.scope == "browser_chrome" and candidate.operation in {"click", "double_click"}:
        return (work, state[1], "browser_navigation", target.name.casefold())
    if target and (target.application == "gnome-shell" or "gnome-shell" in target.parent_context):
        return (work, state[1], "shell_navigation", "system_controls")
    if target and re.search(r"\b(?:zoom|pan)\b", target.name, re.I):
        return (work, state[1], candidate.operation, target.name.casefold())
    return None


class GoalExecution:
    def __init__(self, memory, max_actions, interaction):
        self.memory = memory
        self.max_actions = max_actions
        self.interaction = interaction
        self.pending = None
        self.history = []
        self.end_reason = None
        self.visual = []
        self.visual_frame = None
        self.recovery_stage = None
        self.recovery_calls = 0
        self.recovery_seconds = 0.0
        self.attempts = []
        self.failed_actions = {}
        self.grounded_states = {}
        self.no_effect = 0
        self.empty_stages = 0

    def _emit_state(self, session, step):
        session.events.append({"route": "task_memory", "step": step,
                               "state": self.memory.snapshot()})

    def _receive(self, session, observation, targets, tree):
        if self.pending is None:
            return
        receipt = observation.last_receipt
        if receipt is None or not receipt.acknowledged:
            raise RuntimeError("Missing execution receipt; action is not replayed")
        session.consecutive_no_action = 0
        old = self.pending
        self.pending = None
        signature = _state_key(targets)
        changed = old["kind"] == "cli" or signature != old["state"] or _visual_changed(
            old["before_screenshot"], observation.screenshot.path)
        if old.get("effect_bounds") and old["operation"] == "type_text":
            changed = changed or _visual_changed(old["before_screenshot"], observation.screenshot.path,
                                                 old["effect_bounds"])
        effect = _target_effect(old, targets)
        if effect == "met":
            self.recovery_calls, self.recovery_seconds = 0, 0.0
        relevant_change = (old["kind"] == "cli" or effect == "met"
                           or effect != "not_met" and old["loop_state"] != _loop_state(targets, tree))
        if effect == "unknown" and (not targets or old.get("visual_action")):
            relevant_change = changed
        if old.get("overlay_bounds") and effect != "met":
            relevant_change = _visual_changed(old["before_screenshot"], observation.screenshot.path,
                                              old["overlay_bounds"])
        self.interaction.receive(old, effect, changed, receipt.returncode, targets=targets,
                                 evidence_ref=f"A{observation.step_idx}")
        self.memory.active_stage["interaction"] = self.interaction.snapshot()
        if old["kind"] != "cli":
            key = (self.memory.active_stage["work_item_id"], old["loop_state"], old["candidate_key"])
            if not relevant_change:
                self.failed_actions[key] = self.failed_actions.get(key, 0) + 1
            self.attempts.append({"work": key[0], "state": old["loop_state"], "key": old["candidate_key"]})
            self.attempts = self.attempts[-16:]
        self.no_effect = 0 if relevant_change else self.no_effect + 1
        record = {"step": observation.step_idx, "evidence_ref": f"A{observation.step_idx}",
                  "kind": old["kind"], "operation": old["operation"], "target": old["target"],
                  "action": old["action"], "returncode": receipt.returncode,
                  "stdout": receipt.stdout, "stderr": receipt.stderr,
                  "full_output_ref": receipt.full_output_ref, "screenshot": observation.screenshot.path,
                  "observed_controls": [t.compact() for t in targets][:40],
                  "source_frame": self.memory.active_stage["purpose"] == "prepare",
                  "signature": signature, "observed_change": changed,
                  "relevant_change": relevant_change, "target_effect": effect,
                  "interaction_changed": changed, "local_effect": self.interaction.local_effect,
                  "interaction": self.interaction.snapshot(),
                  "application": ET.fromstring(tree or "<empty/>").get("foreground_class", ""),
                  "navigation_key": old.get("navigation_key")}
        self.history.append(record)
        self.memory.record_action(record)
        session.recent.append({"operation": old["operation"], "target": old["target"],
                               "goal": self.memory.active_stage["goal"],
                               "outcome": "changed" if relevant_change else "no_effect",
                               "returncode": receipt.returncode,
                               "evidence_ref": record["evidence_ref"]})
        session.events.append({"route": "goal_action", "step": observation.step_idx,
                               "record": record, "effect_verified": False})
        if receipt.returncode != 0:
            self.end_reason = "execution_error"
        elif old["kind"] == "cli":
            self.end_reason = "cli_returned"
        elif self.memory.active_stage["actions"] >= self.memory.active_stage["max_actions"]:
            self.end_reason = "stage_action_limit"
        elif self.interaction.checkpoint_due:
            self.end_reason = "execution_checkpoint_stalled"
        elif self.no_effect >= 3 and (session.components.grounder is None
                or (self.memory.active_stage["work_item_id"], _loop_state(targets, tree))
                in self.grounded_states):
            self.end_reason = "repeated_action_without_observable_progress"

    def _finish(self, session, observation):
        reference = f"{self.memory.active_stage['id']}:O{observation.step_idx}"
        evidence = {h["evidence_ref"]: h for h in self.history}
        evidence[reference] = {"screenshot": observation.screenshot.path}
        started = time.perf_counter()
        result = session.components.stage_verifier(
            session.instruction, Path(observation.screenshot.path), memory=self.memory,
            history=[{k: v for k, v in h.items() if k != "signature"} for h in self.history],
            end_reason=self.end_reason, evidence=evidence)
        stage_id = self.memory.active_stage["id"]
        work = self.memory.active_stage["work_item_id"]
        source_progress = (self.memory.stats["new_facts"], self.memory.stats["source_parts"])
        delivery = (self.memory.stats["delivery_advances"], self.memory.stats["milestone_advances"])
        self.interaction.finish(self.memory, result, self.history)
        advanced = self.memory.apply(result, evidence)
        acquired = source_progress != (self.memory.stats["new_facts"], self.memory.stats["source_parts"])
        delivered = delivery != (self.memory.stats["delivery_advances"],
                                  self.memory.stats["milestone_advances"])
        if delivered:
            self.recovery_calls, self.recovery_seconds = 0, 0.0
        self.empty_stages = self.empty_stages + 1 if not self.history and not advanced and not acquired else 0
        if self.memory.work_items[work]["status"] == "done":
            self.failed_actions = {k: v for k, v in self.failed_actions.items() if k[0] != work}
            self.attempts = [a for a in self.attempts if a["work"] != work]
            self.memory.execution_constraints = [c for c in self.memory.execution_constraints
                                                 if c["work_item_id"] != work or c.get("global_scope")]
        for rejected in self.memory.last_rejections:
            session.events.append({"route": "memory_update_rejected", "stage_id": stage_id, **rejected})
        session.events.append({"route": "stage_verified", "step": observation.step_idx,
                               "stage_id": stage_id, "end_reason": self.end_reason,
                               "evidence": {k: {"screenshot": v["screenshot"],
                                                "full_output_ref": v.get("full_output_ref")}
                                            for k, v in evidence.items()},
                               "verification": result, "memory_advanced": advanced,
                               "progress_delta": {"source": acquired, "delivery": delivered},
                               "control_decision": "completed_unit" if self.memory.work_items[work]["status"]
                               == "done" else "recover_execution" if self.memory.work_items[work].get(
                                   "recovery_required") else "continue_increment" if advanced else "replan",
                               "seconds": time.perf_counter() - started})
        session.recent.append({"operation": "stage_verified", "stage_id": stage_id,
                               "outcome": result["stage_outcome"], "evidence": result["summary"],
                               "remaining": result["remaining"]})
        self.history, self.end_reason, self.visual = [], None, []
        self.visual_frame = None
        self.no_effect = 0
        self._emit_state(session, observation.step_idx)
        if result.get("failure_kind") == "policy_violation":
            session.events.append({"route": "goal_policy_violation", "step": observation.step_idx,
                                   "reason": result["policy_violation"]})
            return FailAction(reason="policy_violation: " + result["policy_violation"])
        if self.empty_stages >= 3:
            session.events.append({"route": "goal_stalled", "step": observation.step_idx,
                                   "reason": "three_consecutive_stages_without_an_action",
                                   "blocking_reason": result.get("failure_kind", "")})
            return FailAction(reason="three_consecutive_stages_without_an_action")
        raise ObservationRequired("stage_verification_returned")

    def act(self, session, observation):
        tree = session._read_tree(observation)
        if tree:
            tree = foreground_tree(tree)
        screenshot = Path(observation.screenshot.path)
        targets = session.components.target_builder.build(
            tree, f"t{observation.step_idx}", 128, infer_button_labels=True, include_sliders=True,
            context=" ".join(str(v) for k, v in (self.memory.active_stage or {}).items()
                             if k in {"goal", "completion_criteria", "method", "guidance"}
                             ) or session.instruction, filter_occluded=True,
        ) if tree else []
        targets = session.components.semantic_enricher(
            targets, tree, (observation.screenshot.width, observation.screenshot.height),
            session._read_browser_snapshot(observation))
        self._receive(session, observation, targets, tree)
        if self.end_reason:
            return self._finish(session, observation)
        if session.done_probe is not None:
            result = resolve_done_probe(session, observation, tree)
            if result is not None:
                return result
            raise ObservationRequired("terminal_rejected")
        if self.memory.active_stage is None:
            started = time.perf_counter()
            try:
                plan = session.components.planner(
                    session.instruction, screenshot, targets, session.recent, tree or "<empty/>",
                    requirements=session.requirements, remaining_steps=observation.budget.steps_remaining,
                    memory=self.memory, remaining_seconds=observation.budget.wall_seconds_remaining)
            except ModelResponseError as error:
                session.events.append({"route": "planner_output_invalid", "step": observation.step_idx,
                                       "reason": str(error)})
                return FailAction(reason="planner_output_invalid")
            session.events.append({"route": "planner", "step": observation.step_idx,
                                   "mode": "goal", "plan": plan,
                                   "seconds": time.perf_counter() - started})
            if plan["done"]:
                result = handle_done(session, plan, observation, tree)
                if result is not None:
                    return result
                raise ObservationRequired("terminal_rejected")
            plan["max_actions"] = min(plan["max_actions"], self.max_actions)
            self.memory.start(plan, observation.step_idx)
            self._emit_state(session, observation.step_idx)
            raise ObservationRequired("planner_returned")
        stage = self.memory.active_stage
        document = stage.get("target_document")
        stage["document_recovery"] = bool(document and _document_mismatch(document, tree, targets))
        app = stage.get("target_application")
        stage["application_recovery"] = bool(app and application_mismatch(app, tree))
        if stage["route"] == "cli":
            action = CliAction(command=stage["command"], timeout_s=session.config.cli_timeout_seconds,
                               want_observation="screenshot")
            self.pending = {"kind": "cli", "operation": "cli", "target": stage["method"],
                            "action": action.to_dict(), "state": _state_key(targets),
                            "before_screenshot": observation.screenshot.path}
            session.events.append({"route": "cli", "step": observation.step_idx,
                                   "stage_id": stage["id"], "action": action.to_dict()})
            return action
        return self._gui(session, observation, targets, tree, screenshot)

    def _gui(self, session, observation, targets, tree, screenshot):
        context = execution_context(self.memory)
        stage = self.memory.active_stage
        state = _loop_state(targets, tree)
        work = stage["work_item_id"]
        cached = self.grounded_states.get((work, state))
        if cached and (cached[1] != stage["id"] or _visual_changed(cached[0], screenshot)):
            self.grounded_states.pop((work, state))
            session.events.append({"route": "recovery_cache_invalidated", "step": observation.step_idx,
                                   "stage_id": stage["id"], "reason": "scene_or_subgoal_changed"})
        if self.visual and not self.interaction.reusable_visual(self.visual_frame, state, screenshot):
            self.visual, self.visual_frame = [], None
            self.grounded_states.pop((work, state), None)
            session.events.append({"route": "recovery_invalidated", "step": observation.step_idx,
                                   "stage_id": stage["id"], "reason": "last_action_invalidated_coordinates"
                                   if self.interaction.invalidate_visual else "task_scene_changed"})
        recent = [a for a in self.attempts if a["work"] == work][-8:]
        cycle_keys = _cycle_actions(recent, state)
        cycle = bool(cycle_keys)
        blocked = {key[2] for key, count in self.failed_actions.items()
                   if key[:2] == (work, state) and count >= 2}
        if cycle:
            blocked.update(cycle_keys)
            for key in blocked:
                failure_key = (work, state, key)
                self.failed_actions[failure_key] = max(2, self.failed_actions.get(failure_key, 0))
        for key in blocked:
            constraint = {"work_item_id": work, "window": list(state[:2]), "action_key": list(key),
                          "reason": "Repeated action/cycle in the same observed controls. Try a different "
                                    "interaction; unrelated animation or new facts do not clear it."}
            if constraint not in self.memory.execution_constraints:
                self.memory.execution_constraints.append(constraint)
        context["avoid_actions"] = [list(key) for key in blocked]
        semantic_missing = (not any("gnome-shell" not in t.parent_context for t in targets)
                            or bool(portal_needs_grounding(tree)))
        space = session.components.candidate_builder.build(
            targets, 96, action_inputs=[], context=" ".join(
                context[k] for k in ("goal", "completion_criteria", "method", "guidance")))
        space = goal_candidates(space, targets, tree, context["inputs"],
                                (observation.screenshot.width, observation.screenshot.height))
        if self.visual:
            space.target_heads["visual"] = self.visual
        if (work, state) in self.grounded_states:
            space.controls.pop("visual_fallback", None)
        for operation in space.target_heads:
            space.target_heads[operation] = [c for c in space.target_heads[operation]
                if _action_key(c, targets) not in blocked]
        space.controls = {k: c for k, c in space.controls.items() if _action_key(c, targets) not in blocked}
        context.update(self.interaction.finalize(space, targets, stage, self.memory, tree,
                       observation_ref=f"{stage['id']}:O{observation.step_idx}"))
        control = self.interaction.control
        if self.recovery_stage != (stage["id"], json.loads(control["scope_key"])[2]):
            self.recovery_stage = (stage["id"], json.loads(control["scope_key"])[2])
            self.recovery_calls, self.recovery_seconds = 0, 0.0
        context["grounding_exhausted"] = (
            self.recovery_calls >= session.config.goal_recovery_max_calls
            or self.recovery_seconds >= session.config.goal_recovery_max_seconds
            or control.get("ground_calls", 0) >= session.config.goal_recovery_max_calls
            or control.get("ground_seconds", 0) >= session.config.goal_recovery_max_seconds)
        if context["grounding_exhausted"]:
            space.controls.pop("visual_fallback", None)
            self.memory.block("capability_exhausted", control["scope_key"],
                              "No further visual grounding in this unchanged input scope; "
                              "use offered observed actions or return for another route.",
                              f"{stage['id']}:O{observation.step_idx}")
            context["active_blocker"] = self.memory.active_blocker
        stage["interaction"] = self.interaction.snapshot()
        missing = context["unbound_inputs"] and not any(c.source == "goal_input"
                    or c.metadata.get("commit_action") for c in space.candidates())
        need_grounding = ((semantic_missing or self.no_effect >= 2 or cycle or missing)
                          and not self.visual and not context["grounding_exhausted"]
                          and (work, state) not in self.grounded_states)
        if need_grounding and session.components.grounder is not None:
            if not self._ground(session, observation, screenshot, context, state):
                return self._finish(session, observation)
            if self.visual:
                raise ObservationRequired("local_visual_recovery_returned")
        if missing and not self.visual and stage["actions"] >= 4:
            self.end_reason = "capability_missing: " + ", ".join(context["unbound_inputs"])
            return self._finish(session, observation)
        if context["conflicting_fields"] and not any(c.source == "goal_input" for c in space.candidates()):
            self.end_reason = "stage_input_conflict: " + ", ".join(context["conflicting_fields"])
            return self._finish(session, observation)
        if (stage["purpose"] == "produce" and stage["actions"] >= 4
                and context["missing_required_fields"] and not context["next_intent"]):
            self.end_reason = "stage_inputs_missing: " + ", ".join(context["missing_required_fields"])
            return self._finish(session, observation)
        choice_context = self.interaction.choice_context(context)
        current_goal = choice_context["objective"]
        ranking = session.components.selector.rank(
            stage_goal=current_goal, intent=json.dumps(choice_context, ensure_ascii=False),
            active_window=ET.fromstring(tree).get("foreground_title", "") if tree else "",
            targets=targets, action_space=space,
            recent_actions=session.recent[-8:], screenshot=screenshot)
        winner = ranking.winner
        operation = winner.operation if winner else "return_to_planner"
        target = next((t for t in targets if winner and t.id == winner.target_id), None)
        action = session.components.compile_gui(winner, target) if winner else None
        session.events.append({"route": "gui", "step": observation.step_idx, "mode": "goal",
                               "selector_backend": session.config.fast_backend,
                               "stage_id": self.memory.active_stage["id"],
                               "plan": {"stage_goal": context["goal"], "current_goal": current_goal},
                               "input_frontier": {k: context[k] for k in (
                                   "next_intent", "missing_required_fields", "unbound_inputs",
                                   "conflicting_fields", "satisfied_inputs")},
                               "recovery_scope": {
                                   k: context[k] for k in ("blocking_overlay", "overlay_bounds",
                                                          "document_recovery", "application_recovery")
                                   if k in context},
                               "candidates": [c.compact() for c in space.candidates()],
                               "winner": winner.compact() if winner else None,
                               "avoid_actions": context["avoid_actions"],
                               "execution_control": context["execution_control"],
                               "ranking": ranking_dict(ranking),
                               "action": action.to_dict() if action else None})
        if operation == "visual_fallback" and session.components.grounder is not None:
            if (work, state) not in self.grounded_states:
                if not self._ground(session, observation, screenshot, context, state):
                    return self._finish(session, observation)
                if self.visual:
                    raise ObservationRequired("visual_grounder_returned")
        if action is None:
            self.end_reason = "stage_done_requested" if operation == "done" else "returned_to_planner"
            return self._finish(session, observation)
        self.pending = {"kind": action.kind, "operation": operation,
                        "target": winner.description, "action": action.to_dict(),
                        "state": _state_key(targets), "loop_state": state,
                        "candidate_key": _action_key(winner, targets),
                        "before_screenshot": observation.screenshot.path,
                        "expected_text": winner.metadata.get("text"),
                        "input_mode": winner.metadata.get("input_mode"),
                        "target_semantic": target.semantic_key if target else None,
                        "field_label": winner.metadata.get("field_label"),
                        "navigation_key": _navigation_key(winner, target, work, state),
                        "visual_action": operation == "scroll" or winner.source == "visual_grounder"
                                         or target is not None and target.role == "canvas",
                        "fraction": winner.metadata.get("fraction")}
        self.pending["overlay_bounds"] = context.get("overlay_bounds")
        self.pending.update(control_scope=context["execution_control"]["scope_key"],
                            control_state=self.interaction.content_state)
        self.pending.update({k: winner.metadata.get(k) for k in (
            "interaction_binding", "commit_action", "key_sequence", "execution_family")})
        self.pending["effect_bounds"] = winner.metadata.get("visual_bounds") or (
            [target.x, target.y, target.width, target.height] if target else None)
        return action

    def _ground(self, session, observation, screenshot, context, state):
        control = self.interaction.control
        if (self.recovery_calls >= session.config.goal_recovery_max_calls
                or self.recovery_seconds >= session.config.goal_recovery_max_seconds
                or control is not None and (control.get("ground_calls", 0)
                    >= session.config.goal_recovery_max_calls or control.get("ground_seconds", 0)
                    >= session.config.goal_recovery_max_seconds)):
            self.end_reason = "capability_missing: visual grounding budget exhausted"
            self.memory.block("capability_exhausted", control["scope_key"], self.end_reason,
                              f"{self.memory.active_stage['id']}:O{observation.step_idx}")
            session.events.append({"route": "recovery_exhausted", "step": observation.step_idx,
                                   "stage_id": self.memory.active_stage["id"],
                                   "calls": self.recovery_calls, "seconds": round(self.recovery_seconds, 3)})
            return False
        started = time.monotonic()
        self.recovery_calls += 1
        self.grounded_states[(self.memory.active_stage["work_item_id"], state)] = (
            str(screenshot), self.memory.active_stage["id"])
        plan = {**self.interaction.choice_context(context),
                "current_goal": context.get("next_intent") or context["completion_criteria"],
                "stage_goal": context["completion_criteria"],
                "action_proposals": [{"operation": "type_text", **v} for v in context["inputs"]
                                     if not launcher_input(v) or state[0] == "GNOME Overview"]}
        self.visual = session.components.grounder(session.instruction, plan, screenshot)
        elapsed = time.monotonic() - started
        self.recovery_seconds += elapsed
        if control is not None:
            control.update(ground_calls=control.get("ground_calls", 0) + 1,
                           ground_seconds=control.get("ground_seconds", 0.0) + elapsed)
        self.visual_frame = {"step": observation.step_idx, "state": state, "screenshot": str(screenshot)}
        # The new frame was located after the invalidating action, not before it.
        self.interaction.invalidate_visual = False
        session.events.append({"route": "recovery", "step": observation.step_idx,
                               "stage_id": self.memory.active_stage["id"],
                               "method": "visual_grounding", "candidate_count": len(self.visual),
                               "avoid_actions": context.get("avoid_actions", []),
                               "attempt": self.recovery_calls})
        return True
