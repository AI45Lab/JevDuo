"""回合的验证与终态阶段（从 session 按职责拆出；显式接收 session 状态，风格与旧 runtime 一致）。

- verify_pending：用新观察验证上一步（回执 + BEFORE/AFTER 截图 + a11y 对照），更新 recent /
  failed_toggles / verified_focus；
- terminal_decision / done probe：完成声明的终态裁决与输出文件存在性探针。
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from ..gui.focus import action_key, toggle_transition
from ..memory.context import verification_context
from ..protocol.models import CliAction, FailAction, FinishAction, Observation
from .stopping import NO_ACTION_LIMIT


def existence_probe(paths: list[str]) -> str:
    listing = json.dumps(paths, ensure_ascii=False)
    script = (
        f"import os,json;print(json.dumps({{p: os.path.isfile(p)"
        f" for p in {listing}}}, ensure_ascii=False))"
    )
    return "python3 -c " + shlex.quote(script)


def handle_done(session, plan, observation: Observation, tree: str | None):
    """planner 声称完成：有 output_path 需求时先发 CLI 存在性探针，否则直接终态裁决。"""
    output_paths = [item["output_path"] for item in session.requirements if item.get("output_path")]
    if output_paths:
        session.done_probe = plan
        probe = CliAction(command=existence_probe(output_paths), timeout_s=30, want_observation="none")
        session.events.append(
            {"step": observation.step_idx, "route": "done_probe", "command": probe.command}
        )
        return probe
    return terminal_decision(session, plan, observation, tree, output_files=None)


def resolve_done_probe(session, observation: Observation, tree: str | None):
    """探针回执 → output_files → 终态裁决。"""
    plan = session.done_probe
    output_files = None
    receipt = observation.last_receipt
    if receipt is not None and receipt.returncode == 0:
        try:
            output_files = json.loads(receipt.stdout.strip() or "{}")
        except ValueError:
            output_files = None
    return terminal_decision(session, plan, observation, tree, output_files)


def terminal_decision(session, plan, observation: Observation, tree, output_files):
    """终态裁决：completed → Finish(infeasible → Fail)；被拒则记 recent 并返回 None 继续规划。"""
    session.done_probe = None
    unchanged = (
        next(
            (
                check
                for reference, control in session.failed_toggles.values()
                if (check := toggle_transition(reference, tree or "<empty/>", control)) is not None
                and not check["changed"]
            ),
            None,
        )
        if tree is not None
        else None
    )
    verification = session.components.verifier(
        session.instruction,
        Path(observation.screenshot.path),
        recent=session.recent,
        terminal_claim=plan,
        control_state_check=unchanged,
        requirements=session.requirements,
        output_files=output_files,
        **({"task_memory": verification_context(session.goal.memory)} if session.goal is not None else {}),
    )
    session.events.append(
        {
            "step": observation.step_idx,
            "route": "terminal",
            "planner": plan,
            "completion_check": verification,
            "output_files": output_files,
        }
    )
    status = verification["task_status"]
    if status == "completed":
        return FinishAction(
            outputs=tuple(
                item["output_path"] for item in session.requirements if item.get("output_path")
            )
        )
    if status == "infeasible":
        return FailAction(reason=str(plan.get("current_goal") or "infeasible"))
    session.recent.append(
        {
            "operation": "completion_rejected",
            "target": "none",
            "goal": plan.get("current_goal", ""),
            "outcome": "no_action",
            "verification": verification,
            "expected_observation": "Complete all remaining task requirements.",
        }
    )
    session.consecutive_no_action += 1
    if session.consecutive_no_action >= NO_ACTION_LIMIT:
        return FailAction(reason="no_action_limit")
    return None


def verify_pending(session, observation: Observation, tree: str | None) -> None:
    """验证上一步：CLI 走 execution_result；GUI 走 BEFORE/AFTER + toggle 对照。"""
    pending = session.pending
    session.pending = None
    receipt = observation.last_receipt
    if receipt is None:
        raise RuntimeError("环境未返回上一步执行回执（协议违例）")
    screenshot_before = Path(pending["before_screenshot"])
    if pending["kind"] == "cli":
        verification = _verify_action(
            session, observation, tree, pending,
            previous=screenshot_before,
            expected=pending["plan"]["expected_observation"],
            action=pending["action"],
            recent=session.recent,
            requirements=session.requirements,
            execution_result=receipt.to_dict(),
        )
        if receipt.returncode != 0:
            verification.update(outcome="not_achieved", task_status="incomplete")
        session.recent.append(
            {
                "operation": "cli",
                "target": "model_generated",
                "action": pending["action"],
                "execution": receipt.to_dict(),
                "goal": pending["plan"]["current_goal"],
                "expected_observation": pending["plan"]["expected_observation"],
                "outcome": verification["outcome"],
                "verification": verification,
                "state_changed": verification["state_changed"],
            }
        )
        session.verified_focus = None
        session.events.append(
            {
                "step": observation.step_idx,
                "route": "cli_verified",
                "execution": receipt.to_dict(),
                "verification": verification,
            }
        )
        return
    winner = pending["winner"]
    target = pending["target"]
    before_tree = pending["before_tree"]
    control_check = (
        toggle_transition(before_tree, tree or "<empty/>", target)
        if winner.operation == "click"
        and target is not None
        and before_tree is not None
        and tree is not None
        else None
    )
    verification = _verify_action(
        session, observation, tree, pending,
        previous=screenshot_before,
        expected=pending["plan"]["expected_observation"],
        action=winner.compact(),
        recent=session.recent,
        control_state_check=control_check,
        requirements=session.requirements,
    )
    if receipt.returncode != 0:
        verification.update(outcome="not_achieved", task_status="incomplete")
    if control_check is not None:
        if control_check["changed"]:
            session.failed_toggles.pop(target.semantic_key, None)
        else:
            session.failed_toggles[target.semantic_key] = (before_tree, target)
    session.verified_focus = (
        {
            "name": "Verified file-dialog location",
            "role": "entry",
            "focused": True,
            "source": "visual_verifier",
            "bounds": None,
        }
        if winner.operation == "keypress"
        and winner.metadata.get("key_sequence") == [["ctrl", "l"]]
        and verification["path_input_focused"]
        else None
    )
    session.recent.append(
        {
            "operation": winner.operation,
            "target": winner.metadata.get("semantic_key") or winner.id,
            "goal": pending["plan"]["current_goal"],
            "expected_observation": pending["plan"]["expected_observation"],
            "outcome": verification["outcome"],
            "verification": verification,
            "action_key": action_key(winner),
            "state_changed": verification.get("state_changed"),
        }
    )
    session.events.append(
        {
            "step": observation.step_idx,
            "route": "gui_verified",
            "execution": receipt.to_dict(),
            "verification": verification,
            "control_state_check": control_check,
        }
    )


def _verify_action(session, observation, tree, pending, **kwargs):
    if session.stage is not None:
        return session.stage.verify(session, observation, tree, pending, **kwargs)
    return session.components.verifier(
        session.instruction, Path(observation.screenshot.path), **kwargs,
    )
