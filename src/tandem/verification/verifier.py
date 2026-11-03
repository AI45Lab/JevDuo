"""verifier：独立验证与需求抽取（LLM）；从旧仓库 verification.py 按契约移植。"""

from __future__ import annotations

import base64
import json
from pathlib import Path


def as_text(value) -> str:
    """把模型可能给出的多种 evidence 形状压成一段文本。

    同一句 "evidence"，不同厂商给的形状不一样：字符串、字符串列表、字典都出现过
    （MiniMax 2026-10-23 实测给了列表与字典）。校验器只认字符串，所以这里统一压平，
    免得因为形状差异把整题判失败——大脑是可换的，形状不该是失败原因。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(f"- {as_text(item)}" for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {as_text(item)}" for key, item in value.items())
    return "" if value is None else str(value)


def verify_observation(
    client,
    instruction,
    screenshot: Path,
    *,
    previous: Path | None = None,
    expected: str = "",
    action=None,
    recent=None,
    terminal_claim=None,
    control_state_check=None,
    requirements=None,
    output_files=None,
    execution_result=None,
    task_memory=None,
):
    context = {
        "task": instruction,
        "expected_change": expected,
        "action": action,
        "recent_actions": (recent or [])[-4:],
        "terminal_claim": terminal_claim,
        "control_state_check": control_state_check,
        "requirements": requirements or [],
        "public_output_files": output_files,
    }
    if execution_result is not None:
        context["execution_result"] = execution_result
    if task_memory is not None:
        context["verified_task_memory"] = task_memory
    content = [{"type": "text", "text": json.dumps(context, ensure_ascii=False)}]
    for label, path in (("BEFORE", previous), ("CURRENT", screenshot)):
        if path is not None:
            content.extend(
                [
                    {"type": "text", "text": label},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(path.read_bytes()).decode("ascii")
                        },
                    },
                ]
            )
    messages = [
        {
            "role": "system",
            "content": (
                "You independently verify a desktop GUI action from BEFORE and CURRENT screenshots. "
                "Do not trust the planner's claim. Return JSON with outcome (achieved, not_achieved, or "
                "uncertain), evidence (specific visible observations), task_status (completed, incomplete, "
                "or infeasible), and path_input_focused (boolean). Compare the expected change with the "
                "actual current UI, including the clicked control's position. A toolbar change is not "
                "evidence of formatting; cross-check supplied A11y control_state_check. An unchanged "
                "toggle means that clicking it did not toggle the setting, even if selection highlights "
                "look like formatting. Respect failed control-state checks in recent_actions. "
                "proof of the requested effect. Use not_achieved for visible contradictory evidence; "
                "use uncertain when an invisible caret or a blinking cursor prevents verification. "
                "A filename/title alone is not evidence that "
                "the correct original document and all requirements were preserved. An Untitled blank "
                "document cannot satisfy saving the user's existing slides. Mark the task completed only "
                "with evidence for all user requirements; otherwise incomplete. Mark infeasible only for "
                "a demonstrated limitation under the user's constraints, not absent A11y targets or "
                "missing candidate actions. path_input_focused may be true only when a native Open/Save/"
                "Install file dialog is visibly open and its location/path entry visibly has keyboard "
                "focus after Ctrl+L; do not infer this from an underlying editor's stale A11y focus."
                " Also return state_changed (boolean): did the action cause a meaningful UI/content change, "
                "excluding cursor movement, hover highlights, blinking carets, or reselecting the same value? "
                "For a terminal claim return requirement_checks, one object per supplied requirement with "
                "id, status (met, not_met, uncertain), and evidence. Judge the full original task, not the "
                "planner's narrower goal. Existing before-state is not proof of a successful change. "
                "A query URL parameter does not prove a persistent preference or actual result count. "
                "Do not infer application semantics solely from a plausible preference name. "
                "If requested output files are supplied, absence prevents completion; existence alone "
                "does not prove correct contents. Never use hidden evaluator rules."
            ),
        },
        {"role": "user", "content": content},
    ]
    if execution_result is not None:
        messages[0]["content"] += (
            " For a CLI action also inspect its command, actual exit code and captured stdout/stderr. "
            "A successful exit or an Actor's printed success claim is not proof of task completion. "
            "A CLI read-only inspection can be achieved without a GUI change; preserve state_changed=false "
            "in that case. Report contradictory command failures instead of assuming the intended edit happened."
        )
    if task_memory is not None:
        messages[0]["content"] += (
            " For whole-task verification, use supplied independently verified stage evidence and "
            "confirmed source facts as historical evidence; do not require every prior page or source "
            "to appear simultaneously in the current screenshot. The ledger is not infallible: current "
            "contradictions override it. A pending publication or unsaved working copy is not delivery."
        )
    result = client.json_request(
        messages,
        lambda value: (
            value.get("outcome") in {"achieved", "not_achieved", "uncertain"}
            and value.get("task_status") in {"completed", "incomplete", "infeasible"}
            and isinstance(value.get("path_input_focused"), bool)
            and isinstance(value.get("evidence"), (str, list, dict))
            and isinstance(value.get("state_changed"), bool)
            and (
                not terminal_claim
                or isinstance(value.get("requirement_checks"), list)
                and all(
                    isinstance(item, dict)
                    and item.get("status") in {"met", "not_met", "uncertain"}
                    and isinstance(item.get("evidence"), (str, list, dict))
                    for item in value["requirement_checks"]
                )
            )
        ),
        role="verify_observation",
    )
    # 形状归一：evidence 一律压成字符串（下游 control_state_check 要拼字符串）
    result["evidence"] = as_text(result.get("evidence"))
    for item in result.get("requirement_checks", []) or []:
        if isinstance(item, dict):
            item["evidence"] = as_text(item.get("evidence"))
    if control_state_check is not None:
        result["control_state_check"] = control_state_check
        if not control_state_check["changed"]:
            result.update(
                outcome="not_achieved",
                task_status="incomplete",
                evidence=f"A11y toggle state did not change: {control_state_check}. " + result["evidence"],
            )
    if terminal_claim and result["task_status"] == "completed":
        by_id = {item.get("id"): item for item in result.get("requirement_checks", [])}
        if any(by_id.get(item["id"], {}).get("status") != "met" for item in requirements or []) or any(
            not exists for exists in (output_files or {}).values()
        ):
            result.update(task_status="incomplete", outcome="not_achieved")
        if task_memory and any(a.get("delivery_pending") for a in task_memory["artifacts"]):
            result.update(task_status="incomplete", outcome="not_achieved",
                          evidence=result["evidence"] + " Working-copy publication remains pending.")
    return result


def task_requirements(client, instruction):
    value = client.json_request(
        [
            {
                "role": "system",
                "content": (
                    "Extract 1-12 independently checkable requirements from the original desktop task. "
                    "Return JSON {requirements:[{id,requirement,output_path}]}. IDs are R1, R2, etc. "
                    "Include all explicit conditions, preservation constraints, and required saving. "
                    "Do not prescribe actions or invent application preferences, hidden grading rules, "
                    "or new requirements. output_path is an absolute output file path only when explicitly "
                    "given in the task, otherwise null. Provided input files are not requested outputs."
                ),
            },
            {"role": "user", "content": instruction},
        ],
        lambda value: isinstance(value.get("requirements"), list)
        and 1 <= len(value["requirements"]) <= 12
        and all(
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("requirement"), str)
            for item in value["requirements"]
        ),
        role="task_requirements",
    )
    for item in value["requirements"]:
        path = item.get("output_path")
        if not isinstance(path, str) or not path.startswith("/") or path not in instruction:
            item["output_path"] = None
    return value["requirements"]
