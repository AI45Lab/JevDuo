"""Session-local stage progress; every action still uses current observations and receipts."""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path

from ..verification.stage_rules import rule_verification


def _norm(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _role_matches(actual: str, expected: str) -> bool:
    actual, expected = actual.replace("-", "_"), expected.replace("-", "_")
    if actual == expected:
        return True
    groups = (
        {"button", "push_button", "toggle_button"},
        {"menu", "menu_item"},
        {"list_item", "icon", "tree_item"},
        {"text", "link"},
        {"entry", "textbox", "searchbox"},
        {"table_cell", "grid_cell"},
        {"tab", "page_tab"},
        {"checkbox", "check_box"},
    )
    return any(actual in group and expected in group for group in groups)


def _bind_proposal(proposal, targets):
    name = proposal.get("target_name")
    parent = proposal.get("parent_contains")

    def parent_matches(target):
        return not parent or any(_norm(parent) in _norm(item) for item in target.parent_context)

    exact = [
        target for target in targets
        if target.name == name and target.role == proposal.get("target_role") and parent_matches(target)
    ]
    if len(exact) == 1:
        return exact[0]
    normalized = [
        target for target in targets if _norm(target.name) == _norm(name) and parent_matches(target)
    ]
    if len(normalized) == 1:
        return normalized[0]
    compatible = [
        target for target in normalized if _role_matches(target.role, proposal.get("target_role", ""))
    ]
    return compatible[0] if len(compatible) == 1 else None


def recovery_scopes(proposals, targets) -> set[str]:
    scopes = set()
    for proposal in proposals:
        role = str(proposal.get("target_role") or "").replace("-", "_")
        if role in {"tab", "page_tab"}:
            scopes.add("browser_chrome")
        elif role in {"menu", "menu_item"}:
            scopes.add("menu")
    if not scopes:
        if any(target.scope == "web_content" for target in targets):
            scopes.add("web_content")
        else:
            scopes.update(target.scope for target in targets if target.scope != "browser_chrome")
    return scopes


def _target_summaries(targets, scopes) -> list[dict]:
    return [
        {"id": target.id, "name": target.name, "role": target.role, "scope": target.scope}
        for target in targets if target.scope in scopes
    ][:12]


class StageExecution:
    def __init__(self, max_actions):
        self.max_actions = max_actions
        self.milestones = []
        self.index = 0
        self.actions = 0
        self.goal = ""
        self.reason = "initial_stage"

    def invalidate(self, session, reason, step, *, details=None):
        session.events.append({"route": "stage_replan", "step": step, "reason": reason,
                               "stage_goal": self.goal, "milestone": self.index,
                               **(details or {})})
        session.recent.append({"operation": "stage_replan", "outcome": "no_action",
                               "goal": self.goal, "reason": reason,
                               **(details or {}),
                               "verified_milestones": [item["current_goal"]
                                                       for item in self.milestones[:self.index]]})
        self.milestones = []
        self.reason = reason

    def plan(self, session, observation, targets, tree, screenshot):
        called = False
        if self.milestones and self.actions >= self.max_actions:
            self.invalidate(session, "stage_action_limit", observation.step_idx)
        if not self.milestones:
            started = time.perf_counter()
            plan = session.components.planner(
                session.instruction, screenshot, targets, session.recent, tree or "<empty/>",
                session.verified_focus, session.requirements, observation.budget.steps_remaining,
            )
            session.events.append({"route": "planner", "step": observation.step_idx,
                                   "plan": plan, "reason": self.reason, "mode": "stage",
                                   "seconds": time.perf_counter() - started})
            called = True
            if plan["done"]:
                return plan
            self.goal = plan["stage_goal"]
            self.milestones, self.index, self.actions = plan["milestones"], 0, 0
        milestone = self.milestones[self.index]
        plan = {**milestone, "done": False, "task_status": "running",
                "stage_goal": self.goal, "milestone": self.index, "planner_called": called}
        if plan["route"] == "gui":
            bound = []
            unbound = []
            for proposal in plan["action_proposals"]:
                proposal = {key: value for key, value in proposal.items() if key != "target_id"}
                if proposal.get("target_name") is not None and proposal["operation"] != "wait":
                    target = _bind_proposal(proposal, targets)
                    if target is None:
                        unbound.append(proposal)
                        continue
                    proposal["target_id"] = target.id
                bound.append(proposal)
            if not bound:
                scopes = recovery_scopes(unbound, targets)
                details = {
                    "unbound_proposals": unbound,
                    "available_targets": _target_summaries(targets, scopes),
                    "recovery_scopes": sorted(scopes),
                }
                self.invalidate(session, "target_unbound_or_ambiguous", observation.step_idx, details=details)
                plan.update(action_proposals=[], checks=[], recovery=True,
                            recovery_scopes=sorted(scopes))
                return plan
            plan["action_proposals"] = bound
        session.events.append({"route": "stage_local", "step": observation.step_idx,
                               "stage_goal": self.goal, "milestone": self.index, "plan": plan})
        return plan

    def verify(self, session, observation, tree, pending, **kwargs):
        started = time.perf_counter()
        result = rule_verification(pending, observation.last_receipt, tree)
        if result is None:
            result = session.components.verifier(
                session.instruction, Path(observation.screenshot.path), **kwargs,
            )
            result = {**result, "source": "model"}
        result = {**result, "seconds": time.perf_counter() - started}
        self.actions += 1
        if result["outcome"] == "achieved":
            session.events.append({"route": "stage_progress", "step": observation.step_idx,
                                   "stage_goal": self.goal, "milestone": self.index,
                                   "source": result["source"]})
            self.index += 1
            if self.index == len(self.milestones):
                self.milestones = []
                self.reason = "stage_completed"
        elif not result.get("continue_local"):
            self.invalidate(session, "execution_failed_or_unverified", observation.step_idx)
        return result
