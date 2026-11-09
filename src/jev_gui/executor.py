"""Bounded, evidence-bearing JEV execution independent of VM lifecycle."""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

from .bindings import LocalBindings, target_identity
from .operations import operation_request
from .policy import local_candidates


@dataclass
class LocalFrame:
    image: bytes
    path: str
    captured_at: float
    width: int
    height: int
    targets: list
    surface: str = ""


class LocalExecutor:
    """Every physical child action remains in the environment owner's ledger."""

    def __init__(
        self,
        selector,
        observe,
        perform,
        emit,
        *,
        max_frame_age=1.0,
        failed_actions=None,
        initial_frame=None,
        input_history=None,
    ):
        self.selector, self.observe, self.perform, self.emit = selector, observe, perform, emit
        self.max_frame_age = max_frame_age
        self.failed_actions = failed_actions if failed_actions is not None else {}
        self.initial_frame = initial_frame
        self.input_history = input_history if input_history is not None else {}

    async def run(self, request):
        """Run a card without calling the slow model between its operations."""
        if not request.get("operations"):
            return await self._run_one(request)
        started = time.monotonic()
        reports, total = [], 0
        limit = request.get("max_actions", 64)
        result = {}
        for index, operation in enumerate(request["operations"]):
            if total >= limit:
                result.update(stop_reason="local_action_limit", goal_status="incomplete")
                break
            current = operation_request(request, operation, index, limit - total)
            self.emit(
                {
                    "route": "local_operation_started",
                    "operation_index": index,
                    "operation": operation,
                    "parent_goal": request["goal"],
                }
            )
            result = await self._run_one(current)
            total += result["actions"]
            reports.append(
                {
                    "index": index,
                    "kind": operation["kind"],
                    "intent": operation["intent"],
                    "stop_reason": result["stop_reason"],
                    "actions": result["actions"],
                    "input_readback": result["input_readback"],
                }
            )
            self.emit({"route": "local_operation_ended", **reports[-1]})
            if result["stop_reason"] not in {
                "operation_completed",
                "operation_acknowledged",
                "completion_requested",
                "submission_acknowledged",
            }:
                break
        else:
            result.update(stop_reason="card_completed_needs_review", goal_status="needs_review")
        result.update(
            actions=total,
            seconds=time.monotonic() - started,
            operation_results=reports,
            text=f"GUI card stopped: {result['stop_reason']}; {total} physical actions. "
            "Read operation evidence. Unobservable values and completion requests are NOT verified success.",
        )
        result["text"] += self._feedback(result)
        return result

    async def _run_one(self, request):
        reactive = request.get("mode") == "reactive"
        bindings = LocalBindings(request.get("inputs", []), self.input_history)
        control_identity = None
        if self.initial_frame is not None and request.get("control_id"):
            old = [t for t in self.initial_frame.targets if t.id == request["control_id"]]
            if len(old) == 1:
                control_identity = target_identity(old[0])
        if self.initial_frame is not None and not reactive:
            bindings.observe(self.initial_frame.targets, self.initial_frame.surface)
        recent, actions, stale, idle = [], 0, 0, 0
        started = time.monotonic()
        frame = previous = initial = last_winner = None
        reason = "local_action_limit"
        limit = request.get("max_actions", 24)
        while actions < limit:
            try:
                frame = await self.observe(reactive)
            except Exception as error:
                if not actions:
                    raise
                self.emit({"route": "observation_failed", "error_type": type(error).__name__,
                           "action_status": "acknowledged", "action_replayed": False})
                frame, reason = None, "observation_unavailable_after_ack"
                break
            if initial is None:
                initial = frame
            if reactive and (frame.surface, frame.width, frame.height) != (
                initial.surface,
                initial.width,
                initial.height,
            ):
                reason = "reactive_surface_changed"
                break
            if request.get("surface") and request["surface"] != frame.surface:
                reason = "surface_changed"
                break
            if previous is not None and not reactive:
                changed = self._ui_changed(previous, frame)
                recent[-1]["state_changed"] = changed
                idle = 0 if changed else idle + 1
                if not changed:
                    self.failed_actions[self._action_key(previous, last_winner)] = self._controls(previous)
                if idle >= 3:
                    reason = "repeated_action_without_change"
                    break
            bindings.observe(frame.targets, frame.surface)
            if bindings.rows and not bindings.pending and request.get("operation_kind") in {"fill", "select"}:
                reason = "operation_completed"
                break
            if any(row["status"] == "readback_mismatch" for row in bindings.rows):
                reason = "input_readback_mismatch"
                break
            ambiguous = any(row["status"] == "ambiguous" for row in bindings.rows)
            writable = any(bindings.rows[i]["status"] == "pending" for i in bindings.targets)
            if ambiguous and not writable:
                reason = "input_binding_ambiguous"
                break
            if (
                bindings.pending
                and bindings.targets
                and all(
                    row["status"] == "verified"
                    for i, row in enumerate(bindings.rows)
                    if i in bindings.targets
                )
            ):
                reason = "input_target_missing"
                break
            visual_valid = reactive or (
                frame.surface == initial.surface
                and (frame.width, frame.height) == (initial.width, initial.height)
                and not self._ui_changed(initial, frame)
            )
            control_id = request.get("control_id", "")
            if control_identity is not None:
                matches = [t for t in frame.targets if target_identity(t) == control_identity]
                control_id = matches[0].id if len(matches) == 1 and (
                    frame.surface == self.initial_frame.surface) else "missing_control_identity"
            candidates = local_candidates(
                request["goal"],
                frame.targets,
                bindings=bindings,
                visual_targets=request.get("visual_targets", []) if visual_valid else [],
                allowed_keys=request.get("allowed_keys", []),
                reactive=reactive,
                commit_targets=request.get("commit_targets", []),
                screen_size=(frame.width, frame.height),
                operation_kind=request.get("operation_kind", ""),
                command_keys=request.get("command_keys", []),
                file_paths=request.get("file_paths", []),
                drag_path=request.get("drag_path", []),
                control_hint=request.get("control_hint", ""),
                control_id=control_id,
            )
            suppressed = []
            if not reactive:
                suppressed = [
                    c
                    for c in candidates
                    if self.failed_actions.get(self._action_key(frame, c)) == self._controls(frame)
                ]
                candidates = [c for c in candidates if c not in suppressed]
            goal = request["goal"] + "\nCompletion evidence: " + request.get("success_condition", "")
            if request.get("parent_goal"):
                goal += "\nStage context (execute ONLY the current operation): " + request["parent_goal"]
            if bindings.rows:
                goal += "\nInput progress (do not submit until verified): " + json.dumps(
                    bindings.report(), ensure_ascii=False
                )
            winner, ranking = self.selector.choose(
                goal=goal, screenshot=frame.image, candidates=candidates, recent=recent, reactive=reactive
            )
            age = time.monotonic() - frame.captured_at
            expired = reactive and age > self.max_frame_age
            self.emit(
                {
                    "route": "gui",
                    "mode": "northcua_local_" + request.get("mode", "semantic"),
                    "local_goal": request,
                    "screenshot": frame.path,
                    "surface": frame.surface,
                    "input_bindings": bindings.report(),
                    "visual_targets_valid": visual_valid,
                    "suppressed_candidates": [c.id for c in suppressed],
                    "candidates": [c.compact() for c in candidates],
                    "ranking": ranking,
                    "frame_age_seconds": age,
                    "action_discarded": expired or winner is None,
                }
            )
            if expired or winner is None:
                stale += 1
                if stale >= 3:
                    reason = "reactive_latency_exceeded"
                    break
                continue
            stale = 0
            if winner.operation in {"done", "return_to_planner"}:
                reason = "completion_requested" if winner.id == "done" else "blocked"
                break
            commands = winner.metadata["commands"]
            if actions + len(commands) > limit:
                break
            for operation, parameters in commands:
                parameters = dict(parameters)
                if "pixel_point" in parameters:
                    x, y = parameters.pop("pixel_point")
                    if not (0 <= x < frame.width and 0 <= y < frame.height):
                        raise ValueError("Observed target is outside the current screenshot")
                    parameters.update(x=x / frame.width * 1000, y=y / frame.height * 1000)
                result = await self.perform(operation, parameters, reactive)
                actions += 1
                if result.get("action_status") != "acknowledged":
                    reason = "execution_not_acknowledged"
                    break
            if reason == "execution_not_acknowledged":
                break
            if "input_index" in winner.metadata:
                bindings.wrote(winner.metadata["input_index"])
            recent.append(
                {
                    "action": winner.description,
                    "identity": self._action_key(frame, winner)[1],
                    "surface": frame.surface,
                }
            )
            previous, last_winner = frame, winner
            if winner.metadata.get("one_shot"):
                reason = "operation_acknowledged"
                break
            if winner.metadata.get("commit"):
                reason = "submission_acknowledged"
                break
        # A result image must follow the last mutation, never precede it.
        if actions and reason != "observation_unavailable_after_ack":
            try:
                frame = await self.observe(reactive)
                bindings.observe(frame.targets, frame.surface)
            except Exception as error:
                self.emit({"route": "observation_failed", "error_type": type(error).__name__,
                           "action_status": "acknowledged", "action_replayed": False})
                frame, reason = None, "observation_unavailable_after_ack"
        if reason == "local_action_limit" and bindings.rows and not bindings.pending and (
                request.get("operation_kind") in {"fill", "select"}):
            reason = "operation_completed"
        result = self._result(reason, actions, started, frame, recent)
        result["input_readback"] = bindings.report()
        if reason == "observation_unavailable_after_ack":
            result.update(action_status="acknowledged", observation_status="failed", screenshot_policy="none")
            result["text"] += (
                " Input was acknowledged. Refresh observation only; do not replay prior actions.")
        result["text"] += self._feedback(result)
        return result

    @staticmethod
    def _feedback(result):
        """NorthCUA forwards text and images, not arbitrary result dictionary fields."""
        def readback(rows):
            return [{"field": r["field"], "status": r["status"], "target_id": r.get("target_id"),
                     "observed": str(r.get("observed", ""))[:240]} for r in rows]

        operations = [{"index": r["index"], "kind": r["kind"], "intent": r["intent"],
            "stop_reason": r["stop_reason"], "actions": r["actions"],
            "readback": readback(r["input_readback"])} for r in result.get("operation_results", [])]
        controls = [{**c, "name": c["name"][:240], "description": c["description"][:160],
                     "value": str(c["value"])[:240]} for c in result.get("current_ui_controls", [])[:24]]
        return "\nExecution evidence (claims are not task success): " + json.dumps({
            "operations": operations, "readback": readback(result.get("input_readback", [])),
            "surface": result.get("surface"), "current_ui_controls": controls}, ensure_ascii=False)

    @staticmethod
    def _controls(frame):
        return tuple((t.role, t.name, t.value, t.x, t.y) for t in frame.targets if t.y > 40)

    @staticmethod
    def _action_key(frame, candidate):
        key = candidate.metadata.get("semantic_key", candidate.id)
        return frame.surface, f"{candidate.operation}:{key}"

    @staticmethod
    def _ui_changed(before, after):
        """Ignore the desktop clock; scrolling may be legitimate visual progress."""
        if before.surface != after.surface or (before.width, before.height) != (after.width, after.height):
            return True
        if LocalExecutor._controls(before) != LocalExecutor._controls(after):
            return True
        with Image.open(io.BytesIO(before.image)) as left, Image.open(io.BytesIO(after.image)) as right:
            box = (0, min(40, left.height // 4), left.width, left.height)
            diff = ImageChops.difference(left.convert("RGB").crop(box), right.convert("RGB").crop(box))
            return sum(ImageStat.Stat(diff).mean) / 3 > 1.5

    @staticmethod
    def _result(reason, actions, started, frame, recent):
        needs_review = reason in {
            "completion_requested",
            "submission_acknowledged",
            "operation_completed",
            "operation_acknowledged",
        }
        controls = (
            sorted(frame.targets, key=lambda t: "type_text" in t.operations, reverse=True) if frame else []
        )
        return {
            "text": f"Local execution stopped: {reason}; {actions} physical actions. "
            "Tool completion is NOT goal/task success. Inspect evidence; resolve the reported "
            "blocker before delegating the same task again.",
            "goal_status": "needs_review"
            if needs_review
            else ("incomplete" if reason == "local_action_limit" else "blocked"),
            "stop_reason": reason,
            "actions": actions,
            "seconds": time.monotonic() - started,
            "recent_actions": recent[-6:],
            "surface": frame.surface if frame else "",
            "screenshot_path": frame.path if frame else "",
            "screenshot_format": "local_path",
            "screenshot_policy": "raw_only",
            "current_ui_controls": [
                {
                    key: getattr(t, key, "")
                    for key in (
                        "id",
                        "role",
                        "name",
                        "description",
                        "value",
                    "focused",
                    "selected",
                    "checked",
                    "input_type",
                    "date_order",
                        "parent_context",
                        "application",
                        "operations",
                        "x",
                        "y",
                        "width",
                        "height",
                    )
                }
                for t in controls[:48]
            ],
        }
