"""planner：一步决策（LLM）。从旧仓库 planning.py 按契约移植；prompt 全文保持一致。"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from ..gui.focus import input_and_menu_context


def plan_step(
    client,
    instruction,
    screenshot: Path,
    targets,
    recent,
    accessibility_tree,
    verified_focus=None,
    requirements=None,
    remaining_steps=None,
    allow_cli=False,
    route_policy="auto",
):
    input_controls, menu_paths = input_and_menu_context(accessibility_tree)
    context = " ".join(
        (
            instruction,
            *(
                str(item.get("goal") or "") + " " + str(item.get("expected_observation") or "")
                for item in recent[-2:]
            ),
        )
    )
    context_words = set(re.findall(r"[a-z]+|\d+", context.lower()))
    menu_paths = sorted(
        menu_paths,
        key=lambda item: len(set(re.findall(r"[a-z]+|\d+", item["path"].lower())) & context_words),
        reverse=True,
    )[:32]
    ordered_targets = sorted(
        enumerate(targets),
        key=lambda pair: (
            len(set(re.findall(r"[a-z]+|\d+", pair[1].name.lower())) & context_words),
            bool(pair[1].focused),
            -pair[0],
        ),
        reverse=True,
    )
    planner_targets = [pair for pair in ordered_targets if pair[1].scope != "browser_chrome"][:28]
    planner_targets += [pair for pair in ordered_targets if pair[1].scope == "browser_chrome"][:4]
    selected = {id(pair) for pair in planner_targets}
    planner_targets += [pair for pair in ordered_targets if id(pair) not in selected][: 32 - len(planner_targets)]
    prompt = {
        "task": instruction,
        "requirements": requirements or [],
        "remaining_decisions": remaining_steps,
        "visible_controls": [
            {
                "id": target.id,
                "role": target.role,
                "name": target.name,
                "bounds": [target.x, target.y, target.width, target.height],
                "focused": bool(target.focused),
                "description": target.description,
                "value": target.value,
                "scope": target.scope,
                "nearby_text": target.nearby_text,
            }
            for _, target in planner_targets
        ],
        "recent_actions": recent[-4:],
        "focused_inputs": (
            [verified_focus] if verified_focus else [item for item in input_controls if item["focused"]]
        ),
        "menu_paths": [
            {"name": item["name"], "path": item["path"], "role": item["role"]} for item in menu_paths
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You plan one GUI state transition at a time. Inspect the screenshot and visible controls. "
                "Compare the latest screenshot with the last expected_observation before choosing the next goal. "
                "Return only a JSON object with done (boolean), task_status "
                "(running, completed, or infeasible), current_goal (one immediate UI change), "
                "expected_observation (what should change after one action), and action_proposals "
                "(one or two typed alternatives). Each proposal must have an operation field, "
                "never a free-form action. Operations are click, double_click, scroll_up, "
                "scroll_down, type_text, keypress, set_slider, or wait. For a visible control, copy "
                "target_id, target_name, and target_role exactly from the same visible_controls entry. "
                "Never substitute a similarly named control: Slide 5 is not the Slide menu. "
                "For type_text include exact text and input_mode insert (at the cursor) or replace "
                "(replace the field/cell contents). Editable combo_box and table_cell targets are supported. "
                "If focused_inputs confirms one active input, type_text may omit target_id to type at its "
                "existing keyboard focus; this includes a visually verified native file-dialog location "
                "field after Ctrl+L even when it is absent from XML. Use insert for terminals. "
                "For a visually verified native file-dialog path entry, omit target_id and do not click "
                "an underlying editor. If a native dialog's Open/Save button has no A11y ID, use its "
                "visible keyboard mnemonic (such as Alt+O for Open or Alt+S for Save). An Enter key "
                "may navigate into a child directory instead of opening the requested directory. "
                "For set_slider include value from 0 to 1. "
                'For keypress include key_sequence as arrays of keys, e.g. [["f"]] or '
                '[["ctrl","k"],["ctrl","s"]]; use target fields for Delete on a selected control. '
                "F1-F12, Shift+F10, Ctrl+Plus and Ctrl+Equal are supported. Use one key at a time for "
                "navigation; up to eight bounded navigation/chord presses are supported. "
                "For scrolling identify a visible item in the scrollable region. "
                "A populated search field alone is not an applied search: submit it before expecting filtered results. "
                "If the target is not listed, do not invent an ID or choose a near match; "
                "propose visual_fallback. For a menu or submenu without an ID, copy its name, role and "
                "menu_path from menu_paths so its exact keyboard path can be used. "
                "Do not give coordinates or invoke shell execution tools. Commands required by the task "
                "may be entered as type_text into a confirmed focused terminal. Never repeat the last clicked control when it "
                "merely toggled a menu or checkbox. Examine the independent verification in recent_actions. "
                "If an action failed or its result is uncertain, do not assume it succeeded; prefer a "
                "grounded keyboard/menu alternative if screenshot and A11y click positions disagree. "
                "Set done=true and task_status=completed only when the full task is visibly completed "
                "and any required Save action has happened. A new Untitled document is not the user's "
                "existing file, and a saved filename alone does not prove the right document was saved. "
                "For a task that cannot be completed under its stated constraints, set done=true and "
                "task_status=infeasible with the observed reason in current_goal. Do not confuse a "
                "missing harness candidate with an intrinsically infeasible task. Otherwise use "
                "done=false and task_status=running. If the latest recent action has outcome "
                "no_action, no UI action was executed and the screenshot is unchanged; choose a different "
                "executable proposal instead of repeating the rejected goal and candidates."
                " When no_state_change is reported twice, choose a different method or target. "
                "Already-selected settings should not be selected again just to be certain. "
                "Preserve an existing text selection by typing at its confirmed focus without another click. "
                "Track all supplied requirements. Reserve the last eight decisions for finishing and saving "
                "the requested output, not optional cosmetic changes; never drop required conditions."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(screenshot.read_bytes()).decode("ascii")
                    },
                },
            ],
        },
    ]
    if allow_cli:
        messages[0]["content"] = messages[0]["content"].replace(
            "Do not give coordinates or invoke shell execution tools. Commands required by the task "
            "may be entered as type_text into a confirmed focused terminal.",
            "GUI proposals must not contain coordinates or shell execution tools.",
        )
        messages[0]["content"] += (
            " You may instead choose route=cli for deterministic work on public task files. "
            "For route=cli, return done=false, task_status=running, current_goal, "
            "expected_observation and command (one executable Bash script); action_proposals "
            "are not required. The command runs in /home/user in the SAME task VM with a "
            "configured command timeout and actual stdout/stderr feedback. For GUI use route=gui and "
            "the existing typed action_proposals; JEV retains the GUI choice. CLI must not "
            "read evaluators, reference answers, rewards or unrelated credentials, alter "
            "scoring, perform broad deletion, or install packages unless the task requires it. "
            "After editing a document already open in an app, reload/reopen it and inspect "
            "the actual application before completion. Make one bounded transition per turn."
        )
    if route_policy == "cli":
        messages[0]["content"] += " For this CLI-only profile use route=cli for every nonterminal action."

    def valid_plan(plan):
        if plan.get("done") is True:
            return plan.get("task_status") in {"completed", "infeasible"}
        if not plan.get("done") and plan.get("route") == "cli":
            return (
                allow_cli
                and plan.get("done") is False
                and plan.get("task_status") == "running"
                and all(
                    isinstance(plan.get(key), str) and plan[key].strip()
                    for key in ("current_goal", "expected_observation", "command")
                )
            )
        if plan.get("route", "gui") != "gui":
            return False
        if route_policy == "cli":
            return False
        return isinstance(plan.get("done"), bool) and (
            plan.get("task_status") in {"completed", "infeasible"}
            if plan["done"]
            else all(key in plan for key in ("current_goal", "expected_observation", "action_proposals"))
        )

    return client.json_request(messages, valid_plan, role="plan_step")
