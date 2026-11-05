"""One bounded stage per slow-model call; plans are internal, not wire-protocol actions."""

from __future__ import annotations

import base64
import json

from ..gui.focus import input_and_menu_context

OPERATIONS = {"click", "double_click", "type_text", "keypress", "scroll_up", "scroll_down",
              "set_slider", "wait"}


def valid_stage(value, *, allow_cli=True, route_policy="auto"):
    if not isinstance(value, dict) or type(value.get("done")) is not bool:
        return False
    if value["done"]:
        return (value.get("task_status") in {"completed", "infeasible"}
                and isinstance(value.get("current_goal"), str) and bool(value["current_goal"].strip()))
    milestones = value.get("milestones")
    if (not isinstance(value.get("stage_goal"), str) or not value["stage_goal"].strip()
            or not isinstance(milestones, list) or not 1 <= len(milestones) <= 8):
        return False
    for item in milestones:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key].strip()
            for key in ("current_goal", "expected_observation")
        ):
            return False
        route = item.get("route")
        if route == "cli":
            if (not allow_cli or route_policy == "gui" or not isinstance(item.get("command"), str)
                    or not item["command"].strip()):
                return False
        elif route == "gui" and route_policy != "cli":
            proposals = item.get("action_proposals")
            if not isinstance(proposals, list) or not 1 <= len(proposals) <= 4:
                return False
            for proposal in proposals:
                if not isinstance(proposal, dict) or proposal.get("operation") not in OPERATIONS:
                    return False
                if "parent_contains" in proposal and not isinstance(proposal["parent_contains"], str):
                    return False
                if "target_name" in proposal and not all(
                    isinstance(proposal.get(key), str) for key in ("target_name", "target_role")
                ):
                    return False
                operation = proposal["operation"]
                if operation in {"click", "double_click", "scroll_up", "scroll_down", "set_slider"}:
                    if not all(isinstance(proposal.get(key), str) and proposal[key]
                               for key in ("target_name", "target_role")):
                        return False
                if operation == "type_text" and (
                    not isinstance(proposal.get("text"), str)
                    or proposal.get("input_mode") not in {"insert", "replace"}
                ):
                    return False
                if operation == "keypress":
                    sequence = proposal.get("key_sequence")
                    if (not isinstance(sequence, list) or not 1 <= len(sequence) <= 8
                            or not all(isinstance(keys, list) and 1 <= len(keys) <= 4
                                       and all(isinstance(key, str) for key in keys) for keys in sequence)):
                        return False
                if operation == "set_slider" and (
                    type(proposal.get("value")) not in (int, float) or not 0 <= proposal["value"] <= 1
                ):
                    return False
        else:
            return False
        checks = item.get("checks", [])
        if not isinstance(checks, list) or len(checks) > 4:
            return False
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("target"), dict):
                return False
            if not all(isinstance(check["target"].get(key), str) for key in ("name", "role")):
                return False
            if ("parent_contains" in check["target"]
                    and not isinstance(check["target"]["parent_contains"], str)):
                return False
            prop = check.get("property")
            if prop not in {"visible", "value", "checked", "selected", "focused", "expanded"}:
                return False
            if prop == "value":
                if not isinstance(check.get("expected"), str):
                    return False
            elif type(check.get("expected")) is not bool:
                return False
    return True


def _stage_error(value, *, allow_cli=True, route_policy="auto") -> str | None:
    """Locate common schema failures without weakening valid_stage."""
    if valid_stage(value, allow_cli=allow_cli, route_policy=route_policy):
        return None
    if not isinstance(value, dict):
        return "stage: expected a JSON object"
    if type(value.get("done")) is not bool:
        return "done: expected a boolean"
    if not isinstance(value.get("milestones"), list):
        return "milestones: expected a list containing 1-8 items"
    for index, item in enumerate(value["milestones"]):
        path = f"milestones[{index}]"
        if not isinstance(item, dict):
            return f"{path}: expected an object"
        for key in ("current_goal", "expected_observation"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                return f"{path}.{key}: required non-empty string"
        proposals = item.get("action_proposals")
        if item.get("route") == "cli":
            if not isinstance(item.get("command"), str) or not item["command"].strip():
                if isinstance(proposals, list) and any(
                    isinstance(proposal, dict) and proposal.get("operation") == "cli"
                    for proposal in proposals
                ):
                    return (
                        f"{path}.command: CLI milestone requires a top-level command, "
                        "not action_proposals with operation=cli"
                    )
                return f"{path}.command: required non-empty string for route=cli"
        if item.get("route") == "gui" and not isinstance(proposals, list):
            return f"{path}.action_proposals: expected a list containing 1-4 items"
        for proposal_index, proposal in enumerate(proposals or []):
            proposal_path = f"{path}.action_proposals[{proposal_index}]"
            if not isinstance(proposal, dict):
                return f"{proposal_path}: expected an object"
            operation = proposal.get("operation")
            if operation in {"click", "double_click", "scroll_up", "scroll_down", "set_slider"}:
                for key in ("target_name", "target_role"):
                    if not isinstance(proposal.get(key), str) or not proposal[key]:
                        return f"{proposal_path}.{key}: required non-empty string for {operation}"
            if operation == "type_text" and (
                not isinstance(proposal.get("text"), str)
                or proposal.get("input_mode") not in {"insert", "replace"}
            ):
                return f"{proposal_path}: type_text requires text and input_mode insert/replace"
            if operation == "keypress" and not isinstance(proposal.get("key_sequence"), list):
                return f"{proposal_path}.key_sequence: expected 1-8 lists of 1-4 strings"
        checks = item.get("checks", [])
        if isinstance(checks, list):
            for check_index, check in enumerate(checks):
                if not isinstance(check, dict) or not isinstance(check.get("target"), dict):
                    return f"{path}.checks[{check_index}].target: expected an object"
    return "stage response does not match the required schema"


def plan_stage(client, instruction, screenshot, targets, recent, accessibility_tree,
               verified_focus=None, requirements=None, remaining_steps=None, *,
               allow_cli=True, route_policy="auto"):
    inputs, menus = input_and_menu_context(accessibility_tree)
    context = {
        "task": instruction, "requirements": requirements or [], "remaining_actions": remaining_steps,
        "visible_controls": [
            {"name": target.name, "role": target.role, "value": target.value,
             "parents": target.parent_context, "focused": target.focused,
             "checked": target.checked, "selected": target.selected,
             "description": target.description, "scope": target.scope,
             "nearby_text": target.nearby_text} for target in targets
        ],
        "focused_inputs": [verified_focus] if verified_focus else inputs,
        "menu_paths": menus, "recent_actions_and_evidence": recent[-6:],
        "allow_cli": allow_cli, "route_policy": route_policy,
    }
    prompt = """Plan the next bounded desktop stage, not just one action and not the entire unseen task.
Return only JSON. For a running stage use:
{"done":false,"stage_goal":"...","milestones":[{"route":"gui",
"current_goal":"one concrete transition","expected_observation":"specific resulting state",
"action_proposals":[{"operation":"click","target_name":"exact name","target_role":"push_button"}],
"checks":[{"target":{"name":"result control","role":"entry"},"property":"focused","expected":true}]}]}.
Prefer 2-4 milestones for the CURRENT stage, with a hard maximum of eight. If the whole task needs
more, return only its first bounded stage; subsequent calls will plan the rest from fresh evidence.
Do not append the rest of the task or extra audits beyond that limit. Stop at an uncertain boundary instead of
inventing unseen UI. Each milestone is one action, except a bounded scroll-until-visible transition.
action_proposals are alternatives executable from the SAME before-state, never an ordered sequence.
Opening an input, typing into it, and submitting are separate milestones, not three proposals in one.
A populated search field alone is not an applied search. Add a separate submit milestone before any
milestone that expects filtered results or opens a matching result.
Future targets need exact semantic names/roles, optional parent_contains for duplicate names, never
cached target_id or coordinates. Targets are rebound from each new observation. GUI operations:
click, double_click, scroll_up, scroll_down, type_text, keypress, set_slider, wait.
scroll_up/scroll_down require target_name and target_role copied from visible_controls; never emit
{"operation":"scroll_down"} or {"operation":"scroll_up"} without those fields.
type_text requires exact text and input_mode insert/replace. Omit target fields only for a confirmed
keyboard-focused input. keypress requires key_sequence, e.g. [["ctrl","l"]]. set_slider needs value 0..1.
checks are optional; use [] when independent model verification is needed. The property field must
be exactly one of ["visible","value","checked","selected","focused","expanded"], never "exact value".
Use property="value" with expected:string for exact text, otherwise expected:boolean, on an exact target
{name,role,parent_contains?}. Check the RESULT, not the pre-existing button being clicked.
Unknown or duplicate controls cannot prove success. Do not invent conditions to bypass verification.
For CLI use route=cli with command at the MILESTONE level, current_goal, expected_observation,
checks:[], for example {"route":"cli","command":"python3 script.py","current_goal":"one concrete result",
"expected_observation":"specific evidence","checks":[]}.
CLI milestones never use action_proposals. Do not encode CLI as
action_proposals:[{"operation":"cli","command":"..."}].
Actual stdout/stderr will be verified; exit=0 or printed success alone proves nothing.
When CLI is allowed under auto routing, prefer it for deterministic public-file/data edits or audits;
use GUI for application state and inspect the real application after CLI document edits.
Honor allow_cli/route_policy. Never inspect evaluators, reference answers, rewards, unrelated secrets,
or modify scoring. Do not install packages or broadly delete files unless the task requires it.
After CLI document edits, reopen/reload and inspect the real application before completion.
On a repair request, use the fresh observation and recorded failure, preserve verified work, and
replace only remaining work. Do not repeat a failed method unchanged. Missing candidates do not
make a task infeasible. An unexpected UI requires a revised grounded plan, not invented controls.
When a recent stage_replan contains unbound_proposals and available_targets, use that evidence:
do not repeat the failed name/role unchanged. Choose an offered visible target or a different method.
Normalized names and compatible roles are rebound only when unique; otherwise a bounded recovery
candidate set and RETURN_TO_PLANNER are offered instead of guessing.
Preserve the user's exact scope and literal values. Do not introduce unrequested matching modes,
word boundaries, selection filters, case restrictions, or exceptions. A narrower self-chosen goal
cannot satisfy a broader original requirement. Check commands against the original instruction,
not just their own success messages; all occurrences means all literal occurrences unless specified.
The original task is authoritative; extracted requirements are derived notes, not permission to
reinterpret the task or add restrictions. Resolve any mismatch in favor of the original wording.
Use {"done":true,"task_status":"completed" or "infeasible","current_goal":"observed reason"}
only when the original task is fully satisfied (including saving) or demonstrably infeasible.
Completion is independently verified; a stage finishing is NOT proof of task completion."""
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": [
        {"type": "text", "text": json.dumps(context, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
         + base64.b64encode(screenshot.read_bytes()).decode("ascii")}},
    ]}]
    def validate(value):
        error = _stage_error(value, allow_cli=allow_cli, route_policy=route_policy)
        if error:
            # Give the next attempt a precise correction, not the same generic rejection.
            correction = (
                "CLI milestones require a top-level command and no action_proposals. "
                "Move operation=cli out of action_proposals and set route=cli with command."
                if "operation=cli" in error
                else "scroll_up/scroll_down require target_name and target_role copied from "
                "visible_controls; never emit a bare scroll operation."
            )
            messages.extend([
                {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
                {"role": "user", "content": (
                    f"Invalid stage schema at {error}. {correction} "
                    "Return the corrected complete stage JSON, preserving the ORIGINAL task scope "
                    "and all other valid milestones."
                )},
            ])
        return error is None

    return client.json_request(messages, validate, role="plan_stage")
