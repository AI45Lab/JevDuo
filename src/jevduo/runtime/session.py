"""JevDuoSession：planner_led 决策状态机（obs → action）。

旧仓库 runtime.run_episode 的决策循环按消息层协议重表达：环境生命周期/预算强制/落盘/
评分归 jevduo_eval；回执验证与终态裁决在同包 episode.py。行为对齐旧实现，
差异见 DEVIATIONS.md。
"""

from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path

from ..components import Components
from ..config import SessionConfig
from ..gui.focus import prune_stalled
from ..models.ranking import ranking_dict
from ..protocol.models import Action, CliAction, FailAction, GuiAction, Observation, WaitAction
from .episode import handle_done, resolve_done_probe, verify_pending
from .goal_stage import GoalExecution
from .stages import StageExecution, recovery_scopes
from .stopping import NO_ACTION_LIMIT, budget_exhausted


class JevDuoSession:
    def __init__(self, components: Components, *, config: SessionConfig | None = None):
        self.components = components
        self.config = config or SessionConfig()
        self.events: list[dict] = []
        self.begin(requirements=[])  # 构造即重置，但不触发 requirements 抽取

    def begin(self, instruction: str = "", requirements: list | None = None) -> None:
        self.goal = None
        self.instruction = instruction
        # requirements=None（真实 run）时走组件抽取（旧 runtime 同款）；
        # 显式 []（mock 链路）跳过 LLM 调用
        if requirements is None:
            requirements = self.components.requirements(instruction)
        self.requirements = requirements
        self.recent: list[dict] = []
        self.consecutive_no_action = 0
        self.verified_focus = None
        self.failed_toggles: dict[str, tuple[str, object]] = {}
        self.pending: dict | None = None
        self.done_probe: dict | None = None
        self.stage = (
            StageExecution(self.config.stage_max_actions) if self.config.execution_mode == "stage" else None
        )
        if self.config.execution_mode == "goal":
            self.goal = GoalExecution(self.components.memory_factory(instruction, requirements),
                                      self.config.stage_max_actions, self.components.interaction_factory())

    # ------------------------------------------------------------------ 主入口
    def act(self, observation: Observation) -> Action:
        if self.goal is not None:
            return self.goal.act(self, observation)
        tree = self._read_tree(observation)
        if self.pending is not None:
            verify_pending(self, observation, tree)
        if self.done_probe is not None:
            terminal = resolve_done_probe(self, observation, tree)
            if terminal is not None:
                return terminal
        reason = budget_exhausted(observation.budget)
        if reason:
            return FailAction(reason=reason)
        screenshot = Path(observation.screenshot.path)
        raw_targets = (
            self.components.target_builder.build(
                tree, f"t{observation.step_idx}", 128, infer_button_labels=True, include_sliders=True
            )
            if tree is not None
            else []
        )
        targets = self.components.semantic_enricher(
            raw_targets,
            tree,
            (observation.screenshot.width, observation.screenshot.height),
            self._read_browser_snapshot(observation),
        )
        # 同一观察内最多重规划 NO_ACTION_LIMIT+3 次：完成被拒/无候选会递增 no_action 并重规划，
        # 与旧循环 continue 语义一致（旧版重取 obs，本帧无动作时内容等价）。
        for _ in range(NO_ACTION_LIMIT + 3):
            if self.stage is not None:
                plan = self.stage.plan(self, observation, targets, tree, screenshot)
                if plan is None:
                    self.consecutive_no_action += 1
                    if self.consecutive_no_action >= NO_ACTION_LIMIT:
                        return FailAction(reason="no_action_limit")
                    continue
            else:
                plan = self.components.planner(
                    self.instruction, screenshot, targets, self.recent,
                    tree if tree is not None else "<empty/>", self.verified_focus,
                    self.requirements, observation.budget.steps_remaining,
                )
                self.events.append({"step": observation.step_idx, "route": "planner", "plan": plan})
            if plan["done"]:
                terminal = handle_done(self, plan, observation, tree)
                if terminal is not None:
                    return terminal
                continue
            if plan.get("route", "gui") == "cli":
                return self._emit_cli(plan, observation)
            action = self._plan_gui(plan, observation, targets, tree, screenshot)
            if action is not None:
                return action
        return FailAction(reason="no_action_limit")

    # ------------------------------------------------------------------ CLI 路由
    def _emit_cli(self, plan: dict, observation: Observation) -> CliAction:
        action = CliAction(
            command=plan["command"],
            timeout_s=max(1, min(self.config.cli_timeout_seconds, 120)),
            want_observation="screenshot",
        )
        self.pending = {
            "kind": "cli",
            "action": action.to_dict(),
            "plan": plan,
            "before_screenshot": observation.screenshot.path,
            "before_tree": self._read_tree(observation),
        }
        self.consecutive_no_action = 0
        self.events.append(
            {"step": observation.step_idx, "route": "cli", "action": action.to_dict(), "plan": plan}
        )
        return action

    # ------------------------------------------------------------------ GUI 主路径
    def _plan_gui(
        self, plan, observation, targets, tree, screenshot
    ) -> GuiAction | WaitAction | None:
        local_goal = str(plan["current_goal"])
        space = self.components.candidate_builder.build(
            targets, 96, action_inputs=[], context=local_goal
        )
        blocked_action = (
            (self.recent[-1]["operation"], self.recent[-1]["target"])
            if self.recent and self.recent[-1].get("outcome") == "achieved"
            else None
        )
        space = self.components.focus_candidates(
            space,
            targets,
            tree if tree is not None else "<empty/>",
            plan["action_proposals"],
            blocked_action,
            self.verified_focus,
            recovery=plan.get("recovery", False),
            recovery_scopes=plan.get("recovery_scopes"),
        )
        # visual fallback（grounder）按计划延后：组件存在时才走视觉分支（DEVIATIONS.md #1）。
        if self.components.grounder is not None:
            if any(
                item.get("operation") == "visual_fallback" for item in plan["action_proposals"]
            ) or not any(
                self.components.compile_gui(
                    item, next((t for t in targets if t.id == item.target_id), None)
                )
                for item in space.candidates()
            ):
                visual = self.components.grounder(self.instruction, plan, screenshot)
                space.target_heads["visual"] = visual
                if visual:
                    space.controls.pop("visual_fallback", None)
        else:
            space.controls.pop("visual_fallback", None)
        if not plan.get("recovery") and not self._has_executable_candidate(space, targets):
            scopes = recovery_scopes(plan["action_proposals"], targets)
            recovery_direction = self._recovery_direction(plan)
            plan = {
                **plan,
                "recovery": True,
                "recovery_scopes": sorted(scopes),
                "recovery_reason": "no_executable_candidate",
            }
            space = self.components.candidate_builder.build(
                targets, 96, action_inputs=[], context=local_goal
            )
            space = self.components.focus_candidates(
                space,
                targets,
                tree if tree is not None else "<empty/>",
                [],
                blocked_action,
                self.verified_focus,
                recovery=True,
                recovery_scopes=scopes,
                recovery_direction=recovery_direction,
            )
        prune_stalled(space, self.recent)
        ranking = None
        for service_retry in range(3):
            try:
                ranking = self.components.selector.rank(
                    stage_goal=plan.get("stage_goal", self.instruction),
                    intent=local_goal,
                    active_window="",
                    targets=targets,
                    action_space=space,
                    recent_actions=self.recent,
                    screenshot=screenshot,
                )
                break
            except urllib.error.URLError:
                if service_retry == 2:
                    raise
                time.sleep(45)
        winner = ranking.winner
        target = next((item for item in targets if winner and item.id == winner.target_id), None)
        compiled = self.components.compile_gui(winner, target) if winner else None
        self.events.append(
            {
                "step": observation.step_idx,
                "route": "gui",
                "plan": plan,
                "blocked_action": blocked_action,
                "candidates": [candidate.compact() for candidate in space.candidates()],
                "winner": winner.compact() if winner else None,
                "ranking": ranking_dict(ranking),
                "action": compiled.to_dict() if compiled is not None else None,
            }
        )
        if compiled is None:
            if self.stage is not None:
                self.stage.invalidate(self, "no_executable_candidate", observation.step_idx)
            self.consecutive_no_action += 1
            self.recent.append(
                {
                    "operation": winner.operation if winner else "no_choice",
                    "target": winner.id if winner else "none",
                    "goal": local_goal,
                    "expected_observation": plan["expected_observation"],
                    "outcome": "no_action",
                    "rejected_candidates": [
                        candidate.description for candidate in space.candidates()
                    ],
                }
            )
            if self.consecutive_no_action >= NO_ACTION_LIMIT:
                return FailAction(reason="no_action_limit")
            return None
        self.consecutive_no_action = 0
        self.pending = {
            "kind": "gui",
            "plan": plan,
            "winner": winner,
            "target": target,
            "before_screenshot": observation.screenshot.path,
            "before_tree": tree,
        }
        return compiled

    # ------------------------------------------------------------------ 辅助
    def _read_tree(self, observation: Observation) -> str | None:
        a11y = observation.accessibility_tree
        if not a11y.present or not a11y.path:
            return None
        try:
            return Path(a11y.path).read_text(encoding="utf-8")
        except OSError:
            return None

    def _has_executable_candidate(self, space, targets) -> bool:
        by_id = {target.id: target for target in targets}
        return any(
            self.components.compile_gui(candidate, by_id.get(candidate.target_id))
            for candidate in space.candidates()
        )

    @staticmethod
    def _recovery_direction(plan) -> str | None:
        operations = {proposal.get("operation") for proposal in plan["action_proposals"]}
        if "scroll_up" in operations:
            return "up"
        if "scroll_down" in operations:
            return "down"
        goal = str(plan.get("current_goal") or "").lower()
        if any(text in goal for text in ("upward", "scroll up", "move up")):
            return "up"
        if any(text in goal for text in ("downward", "scroll down", "move down")):
            return "down"
        return None

    def _read_browser_snapshot(self, observation: Observation) -> dict | None:
        reference = observation.browser_snapshot
        if reference is None or not reference.present or not reference.path:
            return None
        try:
            value = json.loads(Path(reference.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None
