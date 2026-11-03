"""compile：胜出候选 + 目标 → 协议动作（声明式）。

旧仓库 action_code 产出 pyautogui 代码字符串；协议化后 harness 输出声明式 GuiAction，
pyautogui 代码生成移到 tandem_eval 的执行层（M2）。坐标计算逻辑保持与旧实现一致
（中心点、slider 分数位置、SCROLL_UP/DOWN ±5 clicks）。"""

from __future__ import annotations

from ..protocol.models import GuiAction, WaitAction
from .candidates import ActionCandidate
from .focus import KEYS
from .targets import UITarget


def gui_action(
    winner: ActionCandidate, target: UITarget | None
) -> GuiAction | WaitAction | None:
    operation = winner.operation
    metadata = winner.metadata
    if operation in {"click", "double_click", "right_click"} and (
        target is not None or metadata.get("visual_point")
    ):
        point = metadata.get("visual_point") if target is None else [
            target.x + target.width // 2, target.y + target.height // 2]
        return GuiAction(
            action_type=operation,
            parameters={"x": point[0], "y": point[1]},
            target_id=winner.target_id,
            description=winner.description,
        )
    if operation == "set_slider" and target is not None:
        fraction = metadata["fraction"]
        return GuiAction(
            action_type="click",
            parameters={
                "x": target.x + round((target.width - 1) * fraction),
                "y": target.y + target.height // 2,
            },
            target_id=winner.target_id,
            description=winner.description,
        )
    if operation == "keypress":
        if metadata.get("commit_action") and metadata.get("focus_point"):
            return GuiAction(action_type="type", target_id=winner.target_id, description=winner.description,
                             parameters={"text": "\n", "input_mode": "insert",
                                         "click": metadata["focus_point"]})
        sequence = metadata.get("key_sequence") or (
            [list(KEYS[winner.id])] if winner.id in KEYS else None
        )
        if sequence:
            return GuiAction(
                action_type="hotkey",
                parameters={"sequence": [list(keys) for keys in sequence]},
                target_id=winner.target_id,
                description=winner.description,
            )
        return None
    if operation == "scroll":
        clicks = 5 if winner.id.startswith("SCROLL_UP") else -5
        return GuiAction(
            action_type="scroll",
            parameters={"x": metadata["scroll_x"], "y": metadata["scroll_y"], "dy": clicks},
            target_id=winner.target_id,
            description=winner.description,
        )
    if operation == "type_text":
        click = metadata.get("visual_point")
        text = metadata["text"]
        press_f2 = False
        if target is not None and not metadata.get("focused_input"):
            click = [target.x + target.width // 2, target.y + target.height // 2]
            press_f2 = target.role in {"table_cell", "grid_cell"}
        if target is not None and target.input_type == "date":
            return None  # Native date segments are separate observed inputs, not a tab-delimited textbox.
        if metadata.get("choice_input"):
            text = text.rstrip("\r\n") + "\n"
        return GuiAction(
            action_type="type",
            parameters={
                "text": text,
                "input_mode": ("insert" if metadata.get("choice_input")
                               else metadata.get("input_mode", "replace")),
                "click": click,
                "press_f2": press_f2,
            },
            target_id=winner.target_id,
            description=winner.description,
        )
    if operation == "wait":
        return WaitAction()
    return None
