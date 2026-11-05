"""Host-owned field/commit state; no model calls, VM execution or semantic success claims."""

import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .candidates import ActionCandidate
from .goal_candidates import (
    _input_target,
    _keys,
    application_mismatch,
    document_frontier,
    input_frontier,
    input_satisfied,
    launcher_input,
    matches_field,
    modal_frontier,
    portal_needs_grounding,
)
from .semantics import unrepresented_window


def visual_changed(before, after, bounds=None):
    frames, sizes = [], []
    for path in (before, after):
        with Image.open(path) as image:
            sizes.append(image.size)
            box = ((bounds[0], bounds[1], bounds[0] + bounds[2], bounds[1] + bounds[3]) if bounds
                   else (0, min(32, image.height // 4), image.width, image.height))
            frames.append(image.crop(box).convert("L").resize((160, 90)))
    threshold = 0.1 if bounds else 1.0
    return sizes[0] != sizes[1] or ImageStat.Stat(ImageChops.difference(*frames)).mean[0] > threshold


def visual_identity(candidate):
    """A field's input attempt does not become new because its detected box shifts a few pixels."""
    if candidate.source == "visual_grounder" and candidate.operation == "type_text":
        return "visual_input|" + " ".join(candidate.metadata.get("target_hint", "").casefold().split())
    return None


def _binding(value):
    return json.dumps([value.get("target_hint", value["label"]).casefold(), value["text"].rstrip()],
                      ensure_ascii=False)


def _command_input(value):
    return value.get("kind") == "command" or value.get("commit") == "enter" or bool(
        re.search(r"\b(input|command|console)\b", value.get("target_hint", value["label"]), re.I)
        and re.match(r"\w+\s*\(", value["text"]))


def target_effect(old, targets):
    """Observed field/slider effects, never semantic task acceptance."""
    if old["operation"] == "type_text":
        focus = [t for t in targets if t.focused and t.role != "password_text"
                 and (not old.get("field_label") or matches_field(old["field_label"], t.name))]
        intended = [t for t in targets if old.get("target_semantic")
                    and old["target_semantic"] == t.semantic_key]
        if not intended and old.get("field_label"):
            intended = [t for t in targets if matches_field(old["field_label"], t.name)
                        and t.role in {"entry", "text", "textbox", "spin_button", "searchbox",
                                       "password_text"}]
        if intended:
            focus = intended
        if len(focus) == 1 and focus[0].role == "password_text":
            return "unknown"  # Login, not a dispatch or masked value, verifies this input.
        if len(focus) == 1 and (focus[0].value or old.get("field_label")):
            if old.get("field_label") and not matches_field(old["field_label"], focus[0].name):
                return "not_met"
            matches = input_satisfied(
                {"text": old["expected_text"], "input_mode": old["input_mode"]}, focus[0])
            return "met" if matches else "not_met"
    if old["operation"] == "set_slider":
        target = next((t for t in targets if t.semantic_key == old["target_semantic"]), None)
        if (target and target.value and target.range_min is not None and target.range_max is not None
                and target.range_max > target.range_min):
            fraction = (float(target.value) - target.range_min) / (target.range_max - target.range_min)
            return "met" if abs(fraction - old["fraction"]) <= 0.04 else "not_met"
    return "unknown"


def _content_state(targets):
    """Task controls/values, not focus, coordinates, shell clocks or pixel animation."""
    return tuple(sorted((t.role, re.sub(r" - Memory usage - .+$", "", t.name),
                         "" if t.role == "slider" else t.value, str(t.checked)) for t in targets
                        if t.application != "gnome-shell" and "gnome-shell" not in t.parent_context
                        and not t.name.startswith(("Focused ", "Unnamed "))))


def _navigation_family(operation, sequence):
    if operation == "scroll":
        return "scroll"
    if sequence in ([["tab"]], [["shift", "tab"]]):
        return "focus_traversal"
    if sequence in ([["up"]], [["down"]], [["left"]], [["right"]]):
        return "direction_keys"
    if sequence in ([["enter"]], [["esc"]], [["escape"]]):
        return "activation_keys"
    return None


class GuiInteractionController:
    def __init__(self):
        self.stage_id = None
        self.fields = {}
        self.invalidate_visual = False
        self.control = None
        self.checkpoint_due = False
        self.local_effect = "not_assessed"
        self.content_state = ()
        self.scenes = {}
        self.memory = None
        self.observation_ref = ""
        self.rejected_states = {}

    def receive(self, pending, effect, changed, returncode, *, targets=(), evidence_ref=""):
        """An acknowledged dispatch is not proof of the field value or command's success."""
        self.local_effect = "not_assessed"
        key = pending.get("interaction_binding")
        if key in self.fields:
            field = self.fields[key]
            if returncode:
                field["phase"] = "needs_input"
            elif pending.get("commit_action"):
                field["phase"] = "submitted_unconfirmed"
            elif effect == "not_met":
                field["phase"] = "needs_input"
            elif field["commit"]:
                field["phase"] = "needs_commit"
            else:
                field["phase"] = "confirmed" if effect == "met" else "typed_unconfirmed"
            field.update(observed_change=changed, effect=effect)
        sequence = pending.get("key_sequence")
        self.invalidate_visual = (pending["operation"] == "scroll" or sequence in (
            [["alt", "tab"]], [["alt", "f4"]], [["ctrl", "o"]], [["ctrl", "l"]]))
        if self.control is None or pending.get("control_scope") != self.control["scope_key"]:
            return
        current = _content_state(targets)
        scenes = self.scenes.setdefault(self.control["scope_key"], [pending["control_state"]])
        revisited = current in scenes
        scenes.append(current)
        del scenes[:-16]
        local = not returncode and effect != "not_met" and (
            effect == "met" or current != pending["control_state"])
        self.local_effect = "confirmed_input" if effect == "met" else (
            "controls_changed" if local else "unknown" if changed else "no_observed_effect")
        if self.memory is not None:
            blocker = self.memory.active_blocker
            if (blocker and blocker["owner"] == self.control["scope_key"]
                    and blocker["kind"] in {"rejected_completion", "no_effect"}
                    and (effect == "met" or local and not revisited
                         or pending.get("visual_action") and changed)):
                self.memory.resolve_blocker(blocker["owner"], evidence_ref)
                self.control.pop("completion_rejected", None)
            elif not local and (self.control["without_effect"] or returncode):
                self.memory.block("no_effect", self.control["scope_key"],
                    "The last interaction did not advance the delegated objective", evidence_ref,
                    attempt={"action": pending["operation"], "result": "no_effect"})
        if not returncode and effect == "met":
            self.control.update(attempts={}, without_effect=0, visited_focus=[], ground_calls=0,
                                ground_seconds=0.0)
            self.checkpoint_due = False
            return
        if local and not revisited:
            if not self.control["recovery_required"]:
                self.control.update(ground_calls=0, ground_seconds=0.0)
            self.checkpoint_due = False
            return  # New content permits exploration, but does not release previously failed paths.
        self.control["without_effect"] += 1
        sequence = pending["action"].get("parameters", {}).get("sequence")
        family = pending.get("execution_family") or _navigation_family(pending["operation"], sequence)
        if family and not pending.get("commit_action"):
            attempts = self.control["attempts"]
            attempts[family] = attempts.get(family, 0) + 1
            focused = [t.semantic_key or t.name for t in targets if t.focused]
            focus = "|".join(focused)
            visited = self.control["visited_focus"]
            cycle = bool(focus and focus in visited)
            if family == "focus_traversal" and focus and focus not in visited:
                visited.append(focus)
                self.checkpoint_due = False  # Visiting another known field is finite exploration.
            else:
                self.checkpoint_due = attempts[family] >= (3 if family.startswith("target:") else 4) and (
                    family != "focus_traversal" or not focus or cycle)
        elif self.control["without_effect"] >= 8:
            self.checkpoint_due = True

    def reusable_visual(self, frame, state, screenshot):
        if not frame:
            return False
        return (not self.invalidate_visual and frame["state"] == state
                and not visual_changed(frame["screenshot"], screenshot))


    def finalize(self, space, targets, stage, memory, tree, observation_ref=""):
        """All producers run before the final scope/failed-path checks. No action executes here."""
        self.memory, self.observation_ref = memory, observation_ref
        def scope():
            result = modal_frontier(space, targets, tree)
            if result:
                return result  # Resolve the actual input owner before recovering an underlying document.
            if stage.get("document_recovery") or stage.get("application_recovery"):
                result.update(document_frontier(space, targets, stage.get("target_document"),
                                                stage.get("target_application")))
            return result

        scope()
        interaction = self.constrain(space, targets, stage, memory, tree=tree)
        limits = scope()  # Commit candidates must obey the same scope as every other producer.
        root = ET.fromstring(tree or "<empty/>")
        overlay = limits.get("blocking_overlay", "")
        is_dialog = "blocking_overlay" in limits
        bounds = limits.get("overlay_bounds")
        owner = portal_needs_grounding(tree) or unrepresented_window(root) or (
            "dialog@" + str([round(v / 32) for v in bounds]) if bounds else overlay)
        key = json.dumps([root.get("foreground_class", ""), root.get("foreground_title", ""),
                          owner], ensure_ascii=False)
        blocker = memory.active_blocker
        if (blocker and blocker["status"] != "resolved" and blocker["kind"] == "input_obstructed"
                and not is_dialog and json.loads(blocker["owner"])[0] == root.get("foreground_class", "")):
            memory.resolve_blocker(blocker["owner"], observation_ref)
            for old in memory.execution_constraints:
                if old.get("scope_key") == blocker["owner"]:
                    old.update(recovery_required=False, blocked_families=[],
                               ground_calls=0, ground_seconds=0.0)
            memory.work_items[blocker["work_item_id"]]["recovery_required"] = False
        control = next((c for c in memory.execution_constraints
                        if c.get("kind") == "execution_checkpoint" and c["scope_key"] == key), None)
        if control is None:
            control = {"kind": "execution_checkpoint", "scope_key": key,
                       "work_item_id": stage["work_item_id"], "global_scope": bool(owner),
                       "attempts": {}, "blocked_families": [], "without_effect": 0,
                       "visited_focus": [], "failed_stages": 0, "recovery_required": False}
            memory.execution_constraints.append(control)
        self.control, self.content_state = control, _content_state(targets)
        by_id = {t.id: t for t in targets}
        for operation, candidates in space.target_heads.items():
            for candidate in candidates:
                target = by_id.get(candidate.target_id)
                identity = target.semantic_key if target else candidate.metadata.get("target_label", "")
                candidate.metadata["execution_family"] = _navigation_family(
                    candidate.operation, _keys(candidate))
                if candidate.operation in {"click", "double_click"} and identity:
                    candidate.metadata["execution_family"] = "target:" + identity.casefold()
            space.target_heads[operation] = [c for c in candidates if c.metadata.get("commit_action")
                or c.metadata.get("execution_family") not in control["blocked_families"]]
        active = targets
        if bounds := limits.get("overlay_bounds"):
            x, y, width, height = bounds
            active = [t for t in targets if None not in (t.x, t.y, t.width, t.height)
                      and x <= t.x + t.width // 2 < x + width and y <= t.y + t.height // 2 < y + height]
        elif limits:
            ids = {c.target_id for c in space.candidates() if c.target_id}
            active = [t for t in targets if t.id in ids]
        active_inputs = [v for v in stage["inputs"]
                         if self.fields.get(_binding(v), {}).get("phase") != "consumed"]
        frontier = input_frontier(space, active, active_inputs)
        typing = next((c for c in space.candidates() if c.operation == "type_text"), None)
        specific = next((c.description for c in space.candidates() if c.metadata.get("commit_action")),
                        typing.description + f" Supplied text: {typing.metadata['text']!r}." if typing
                        else frontier["next_intent"])
        intent = specific or interaction.get("next_intent", "") or stage["goal"]
        if is_dialog:
            intent = f"Within the active {overlay!r} dialog: " + intent
        requested = any(c.source == "goal_input" or c.metadata.get("commit_action")
                        for c in space.candidates()) or any(
            not launcher_input(v) and matches_field(v["text"], node.get("name", ""))
            for v in stage["inputs"] for node in root.iter())
        recovery = bool(is_dialog and limits.get("overlay_kind") != "task_dialog" and not requested
                        and (limits.get("overlay_kind") == "obstruction" or control["recovery_required"]))
        if recovery:
            memory.block("input_obstructed", key, overlay, observation_ref)
            intent = (f"Restore input access blocked by the active {overlay!r} dialog. Use its visible "
                      "dismissal controls; do not edit the background or repeat failed shortcuts.")
        if limits and not is_dialog:
            intent = limits["next_intent"] + (" Next legal input: " + specific if specific else "")
        if control.get("completion_rejected"):
            if self.rejected_states.get(key) == self.content_state:
                space.controls.pop("done", None)
            else:
                control.pop("completion_rejected", None)
                memory.resolve_blocker(key, observation_ref)
        if frontier["unbound_inputs"] and not any(c.source == "goal_input" for c in space.candidates()):
            memory.block("target_unbound", key, ", ".join(frontier["unbound_inputs"]), observation_ref)
        return {**interaction, **frontier, **limits, "next_intent": intent,
                "scope_recovery": recovery,
                "active_blocker": deepcopy(memory.active_blocker),
                "execution_control": deepcopy(control)}

    def choice_context(self, context):
        """One current objective, not the original planner's speculative navigation script."""
        scoped = "blocking_overlay" in context or context.get("document_recovery") or context.get(
            "application_recovery")
        recovery = context.get("scope_recovery", False)
        restoring = recovery or context.get("document_recovery") or context.get("application_recovery")
        return {"objective": context.get("next_intent") if restoring else context["goal"],
                "delegated_goal": context["goal"], "next_action_hint": context.get("next_intent", ""),
                "method": "" if restoring else context.get("method", ""),
                "guidance": "" if restoring else context.get("guidance", ""),
                "exit_condition": ("The obstruction is gone; refresh and resume the delegated objective."
                                   if recovery else context["completion_criteria"]),
                "input_owner": context.get("blocking_overlay", context.get("target_application")),
                "constraints": context.get("constraints", []),
                "inputs": [] if recovery else [v for v in context["inputs"]
                           if v["label"] not in context.get("unbound_inputs", [])] if scoped
                          else [v for v in context["inputs"]
                                if self.fields.get(_binding(v), {}).get("phase") != "consumed"],
                "unbound_inputs": [] if recovery else context.get("unbound_inputs", []),
                "confirmed_inputs": context.get("satisfied_inputs", []),
                "failed_paths": context.get("execution_control", {}).get("blocked_families", []),
                "failures": [{k: f[k] for k in ("error", "count") if k in f}
                             for f in context.get("failures", [])[-2:]],
                "active_blocker": context.get("active_blocker"),
                "grounding_exhausted": context.get("grounding_exhausted", False),
                "scope_rule": ("Interact only with the active input owner. A business dialog may be "
                               "the task itself; do not dismiss it just because it is a dialog."
                               if scoped else "Use the current observation, not historical UI state."),
                "actions_remaining": context.get("actions_remaining")}

    def finish(self, memory, result, history):
        """Only a resolved execution condition releases a failed path; unrelated facts cannot."""
        stage = memory.active_stage
        if stage["route"] != "gui" or self.control is None:
            return
        control = self.control
        if not history and result["stage_outcome"] != "met":
            control["completion_rejected"] = True
            self.rejected_states[control["scope_key"]] = self.content_state
            memory.block("rejected_completion", control["scope_key"], result["summary"],
                         next(iter(result["evidence_refs"]), ""),
                         attempt={"action": "done", "result": result["stage_outcome"]})
        elif not history and result["stage_outcome"] == "met":
            return  # A genuinely satisfied initial state need not be modified.
        needed = {f["key"] for f in stage.get("fact_targets", [])} & memory.source_keys
        current_refs = {h["evidence_ref"] for h in history}
        source_complete = (stage["purpose"] == "prepare" and not control["global_scope"]
                           and result["stage_outcome"] == "met"
                           and any(f["key"] in needed and f["status"] == "confirmed"
                                   and set(f["evidence_refs"]) & current_refs for f in result["facts"]))
        resolved = result["stage_outcome"] == "met" and result.get("progress_kind") in {
            "navigation", "content", "save", "inspection"}
        if source_complete or result["unit_met"] or resolved:
            control.update(attempts={}, without_effect=0, visited_focus=[], failed_stages=0,
                           ground_calls=0, ground_seconds=0.0, blocked_families=[], recovery_required=False)
        else:
            blocked = [family for family, count in control["attempts"].items()
                       if count >= (3 if family.startswith("target:") else 4)]
            control["failed_stages"] += 1
            if blocked or control["failed_stages"] >= 2:
                control["blocked_families"] = sorted(set(control["blocked_families"] + blocked))
                control["recovery_required"] = True
                control["last_evidence_ref"] = (history[-1]["evidence_ref"] if history
                                                else next(iter(result["evidence_refs"]), ""))
                memory.work_items[stage["work_item_id"]]["recovery_required"] = True
        if not control["recovery_required"]:
            memory.work_items[stage["work_item_id"]]["recovery_required"] = False
        self.checkpoint_due = False

    def constrain(self, space, targets, stage, memory, tree=""):
        if self.stage_id != stage["id"]:
            self.stage_id, self.fields = stage["id"], {}
        self.invalidate_visual = False
        by_id = {t.id: t for t in targets}
        application = stage.get("target_application")
        for operation, candidates in space.target_heads.items():
            space.target_heads[operation] = [c for c in candidates if not (
                application and (t := by_id.get(c.target_id)) and t.application == "gnome-shell"
                and t.role in {"push_button", "button"} and t.name.casefold() != "files"
                and application_mismatch(application,
                    ET.tostring(ET.Element("desktop-frame", foreground_class=t.name), encoding="unicode"),
                    known_only=True))]
        available = {}
        for candidate in space.candidates():
            if candidate.operation != "type_text":
                continue
            for value in stage["inputs"]:
                if (candidate.metadata.get("text", "").rstrip() != value["text"].rstrip()
                        or launcher_input(value)):
                    continue
                if not matches_field(value.get("target_hint", value["label"]),
                                     candidate.metadata.get("target_hint", "")):
                    continue
                key = _binding(value)
                field = self.fields.setdefault(key, {
                    "target": value.get("target_hint", value["label"]),
                    "expected_value": value["text"].rstrip(),
                    "phase": "needs_input", "commit": "enter" if _command_input(value) else None})
                target = by_id.get(candidate.target_id)
                point = ([target.x + target.width // 2, target.y + target.height // 2] if target
                         else candidate.metadata.get("visual_point"))
                available[key] = point
                candidate.metadata["interaction_binding"] = key
                if field["commit"]:
                    candidate.metadata["text"] = candidate.metadata["text"].rstrip("\r\n")
        # Satisfied semantic fields need no typing candidate but may still need an explicit commit.
        for value in stage["inputs"]:
            key = _binding(value)
            target = _input_target(value, targets)
            field = self.fields.get(key)
            title = ET.fromstring(tree or "<empty/>").get("foreground_title", "")
            if (field and value["text"].startswith("/") and field["phase"] in {
                    "needs_commit", "submitted_unconfirmed", "typed_unconfirmed"}
                    and Path(value["text"]).name.casefold() in title.casefold()):
                # The opened document is evidence; a vanished path box is expected.
                field["phase"] = "consumed"
            if key in self.fields and target is not None:
                available[key] = [target.x + target.width // 2, target.y + target.height // 2]
                if input_satisfied(value, target) and not self.fields[key]["commit"]:
                    self.fields[key]["phase"] = "confirmed"
        commits = []
        for key, field in self.fields.items():
            if field["phase"] == "needs_commit" and available.get(key):
                commits.append(ActionCandidate(
                    id="COMMIT:" + key, operation="keypress", source="interaction",
                    description=f"Focus {field['target']!r} and press ENTER to submit the previously typed "
                                "command. Its result still needs observation; do not request completion yet.",
                    metadata={"key_sequence": [["enter"]], "focus_point": available[key],
                              "interaction_binding": key, "commit_action": True}))
        for operation, candidates in space.target_heads.items():
            space.target_heads[operation] = [c for c in candidates if not (
                (key := c.metadata.get("interaction_binding")) in self.fields
                and self.fields[key]["phase"] != "needs_input")]
        if commits:
            space.target_heads["keypress"] = [c for c in space.target_heads["keypress"]
                                               if _keys(c) != [["enter"]]] + commits
        pending = [c for c in space.candidates() if c.operation == "type_text"]
        for operation, candidates in space.target_heads.items():
            space.target_heads[operation] = [c for c in candidates if not (
                c.operation == "type_text" and memory.blocked_command(
                    c.metadata.get("text", ""), application))]
        pending = [c for c in pending if not memory.blocked_command(c.metadata.get("text", ""), application)]
        if pending or commits:
            space.controls.pop("done", None)
        uncertain = [v["target"] for v in self.fields.values() if v["phase"] == "typed_unconfirmed"]
        label = (pending[0].metadata.get("field_label") or pending[0].metadata.get("target_hint", "field")
                 if pending else "")
        typing = (f"Enter supplied {pending[0].metadata['text']!r} into the observed "
                  f"{label!r}."
                  if pending else "")
        if pending and pending[0].metadata.get("choice_input"):
            typing = pending[0].description
        intent = (commits[0].description if commits else typing if pending else
                  "Input dispatched but value unconfirmed for " + ", ".join(uncertain)
                  + ". Inspect the visible result and continue: " + stage["goal"] if uncertain else "")
        return {"interaction": self.snapshot(), "next_intent": intent} if intent else {
            "interaction": self.snapshot()}

    def snapshot(self):
        return {"stage_id": self.stage_id, "fields": deepcopy(list(self.fields.values())),
                "execution_control": deepcopy(self.control), "local_effect": self.local_effect}
