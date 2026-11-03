"""One requirement-bound semantic verification per ended goal stage, never per GUI action."""

import base64
import json
from copy import deepcopy
from pathlib import Path

from ..models.slow import ModelResponseError

PROMPT = """Independently verify the ENDED desktop subtask against its fixed work unit and ORIGINAL
requirements. Do not judge each intermediate action, demand later pages earlier, or trust the actor's
printed success. Return only JSON:
{stage_outcome:"met|not_met|uncertain",unit_met:false,delivery_state:"none|implemented|saved|verified",
summary:"specific observed result",remaining:["concrete missing part"],evidence_refs:["supplied id"],
facts:[{key:"registered or concrete reusable key",value:"exact content including numbers/units",
status:"confirmed|uncertain",scope:"task|stage",source_ref:"actual file/page/location",
evidence_refs:["supplied id"],complete:true}],
materials:[{source_ref:"actual file/url",covered_parts:["page/section"],
summary:"reusable source content",evidence_refs:["supplied id"]}],
requirement_checks:[{id:"R1",status:"met|not_met|uncertain",evidence_refs:["supplied id"]}],
artifacts:[{target_path:"actual final path",working_path:"actual working path",save_state:"modified|saved",
evidence_refs:["supplied id"]}],invalidated_work_item_ids:[],
milestones:[{key:"stable target/section identifier",summary:"actual delivered increment",
evidence_refs:["current supplied id"]}],policy_violation:"",
progress_kind:"none|navigation|source|content|save|inspection",observed_errors:[],
unit_assessment:{work_item_id:"exact fixed id",status:"met|not_met|uncertain",
evidence_refs:[],remaining:[]}}.
For purpose=prepare, OMIT unit_assessment: the host records not_assessed without asking you to judge
the delivery unit. Never accept a delivery unit during preparation.
Use [] when no update is established. Reference only the supplied evidence IDs. Preserve original
requirements and source facts. Extract essential facts NOW from real observed material/CLI output so
later stages do not reread it; unobserved contents remain unknown. Preparation uses delivery_state=none.
Use the exact facts_needed key when observed information answers that key. Keep partial source coverage
in materials even if not all required facts have been acquired. Opening a reference image, a contact
sheet, an audit or a script
that reports failure is not delivery progress. A finished stage need not satisfy a whole requirement.
unit_met=true ONLY when the full fixed work unit is satisfied; for delivery it requires verified level
and concrete evidence. Saved requires actual persistence to the target, not a working copy or title.
File existence/compilation does not prove content or function. Do not invent a demand to repeat every
successful intermediate action. Mark unmet/uncertain criteria explicitly and preserve actual progress.
Later contradictions invalidate affected units; do not invalidate old work from an uncertain screenshot
alone. Do not read evaluators, hidden grading rules, answers or rewards. Be concise and evidence-based."""

PROMPT += """
Check protected state explicitly: opening a reference must not destroy an unsaved task form/document.
Do not claim another tab, preserved fields or a prior save still exists without current supporting evidence.
When current evidence proves a form reset, edits lost or the wrong artifact replaced the working result,
return the affected delivery work_item_id in invalidated_work_item_ids, even in a prepare stage.
Keep stable source facts, but withdraw stale UI/delivery milestones. A temporary working copy is not the
user's final deliverable: artifacts.target_path names that deliverable, working_path names the actual copy.
When publishing/reopening a previous working copy at its final path, include replaces_working_path with
the previous copy's exact path so its pending publication can be retired.
For file opening, inspect the resulting document instead of demanding its now-closed path entry.
A pending Open/Import confirmation is not a completed file handoff, unless a preview alone was requested.
"""

PROMPT += '''
delivery_state describes ONLY the CURRENT stage's new delivery, never an artifact's historical level.
For navigation/source/no new work use delivery_state=none; the host retains earlier saved/verified state.
Registered source-key updates belong to purpose=prepare acquisition stages, not form-state snapshots
during editing. Keep current form values in stage-scope field-specific notes, never overwrite a source key.
scope=task means reusable SOURCE CONTENT, not whole-task completion. scope=stage is only transient UI
state. A confirmed paragraph or data field is task-scope even while the rest of its source is unread.
Not seeing a source again does not contradict its recorded facts. Retain confirmed source values;
change one only with actual contradictory evidence and a nonempty correction_reason on that fact.
Use the precise registered source key when it is fully answered; store partial sections as separate
reusable facts/materials. Do not mark an entire packet confirmed when required fields are missing.
remaining must describe THIS stage's unmet exit conditions, not a list of every later task obligation.
Only mark unit_met when the original full unit is satisfied. Historical registered evidence may support
source facts; stage completion must also cite at least one CURRENT evidence ID.
Use the request's response_template for the mandatory keys and exact work_item_id; populate it with
observed evidence, not empty defaults. facts[].value is ALWAYS a JSON STRING, including numbers, dates,
lists and tables: use "25", not 25, and readable text, not an object or array. Optional facts[].entries
maps string keys to STRING values; store numbers as strings there too. A stage purpose=prepare is
source acquisition only even when attached to a delivery unit; it cannot finish that delivery unit.
For lists/tables/options, include entries:{"stable item/row/position":"observed value"} and complete:false
until the requested coverage is actually observed. New entries extend the old collection; conflicting
entries or scalar changes require current evidence and correction_reason. Never label a partial list
as a complete registered dependency. Temporary UI state is not required source content.
This request contains selected evidence, not the whole execution log. output_omitted_chars marks an
explicit excerpt: do not claim to have checked omitted contents or a complete source packet. Extract
only supported fields; ask for the missing section rather than another full-document audit.
For prepare, answer fact_targets using their EXACT registered keys and record partial coverage;
unit_met=false, delivery_state=none, and do not assess unrelated delivery requirements.
For produce/save/verify, check only the current stage exit condition and its fixed delivery unit.
Record partial implementation/saving without demanding all future task units. Return only memory deltas,
not a rewrite of existing facts/materials, and no new optional research or inspection requirements.
Missing source fields do not invalidate an independently useful, correctly implemented partial stage.
If that bounded exit condition is met, report stage_outcome=met with unit_met=false and the observed
implemented/saved level. Keep the full unit incomplete and preserve its missing values for later work.
Incomplete prose facts may be appended across stages: complete:false preserves earlier sections without
claiming a complete source. Mark complete:true only when the exact registered field/section is fully answered.
Never replace a known inventory with the contents of a different document under the same scalar key;
prefer document/field-specific facts and preserve source_ref. Do not guess identity or ownership.
For a real delivered increment, record a milestone with a stable artifact/section/field-group key.
Reuse the same key from fixed_work_item.milestones for the same increment; never key by stage ID,
rewording, research, audit files, navigation, or opening a source. New source facts are not delivery.
milestones:[] when nothing was actually implemented, saved or independently checked.
CLI actor assertions, ZIP/XML validity and marker strings establish neither visual fidelity nor working
animations. They can support saving only. Current GUI inspection must demonstrate visual requirements;
a blocking dialog is not evidence that the underlying content was checked. Do not weaken failed checks.
Respect ORIGINAL interaction restrictions. Filling website fields is not permission to edit the web
application's source, inject reload scripts, add missing options or remove validation. If the actions
violate these boundaries, return policy_violation with the specific observed action and do not accept it.
Planner method/guidance and invented workflow preferences are NOT user policy. Wrong application,
wrong shortcut, wrong navigation and incomplete work are recoverable stage errors, not policy violations.
For a genuine violation also return policy_constraint and policy_evidence_refs. policy_constraint must
be an EXACT quote of an ORIGINAL explicit prohibition, or one of website_source_tampering,
evaluator_access, credential_access for the host's integrity boundaries. policy_evidence_refs must cite
CURRENT actions demonstrating the violation, not an old screenshot or the planner's proposed method.
Do not infer tampering from ordinary CLI editing of a requested output document. Opening an unintended
application is not a prohibition violation unless the user explicitly forbids opening it.
Keep the existing working_path for the same target_path. When current evidence demonstrates a real
working-copy migration preserving prior increments, include replaces_working_path with the old exact
working path on that artifact. A fresh source copy or unrelated per-section file is not such a migration.
progress_kind and observed_errors are mandatory. For produce/save/verify also return unit_assessment
with the exact fixed work_item_id, status met|not_met|uncertain, evidence_refs and remaining arrays.
stage_outcome and remaining judge ONLY stage_acceptance. unit_assessment judges ONLY unit_acceptance,
NOT all sibling units or the overall requirement. The entire task is NOT being accepted here.
If a save/open unit is fully supported, mark that UNIT met even when sibling animation/content units
remain pending. Do not demand their contents in this unit. If unit_assessment.status=met, return
unit_met=true and delivery_state=verified with current evidence. Preparation omits unit_assessment.
Opening/maximizing windows, menus, scrolling, and dismissing dialogs are navigation: delivery_state=none,
unit_met=false,milestones:[], even if their bounded preparation stage succeeded. Source reading is source,
not content delivery. A saved partial artifact may be save progress without finishing its whole unit.
observed_errors is [{message:"EXACT error visible in an action receipt or current screenshot",
application:"observed application",command_name:"exact command if explicitly named, otherwise empty",
evidence_refs:["CURRENT id"]}]. Do not replace the error with remaining steps or invent errors.
Unknown command means the command was not recognized; extra arguments are not evidence of support.
Corrective advice belongs in remaining, never in observed_errors or confirmed source facts.
'''


def result_error(value):
    """Precise contract failures; never invent evidence or coerce unsupported results."""
    for key, choices in {"stage_outcome": {"met", "not_met", "uncertain"},
                         "delivery_state": {"none", "implemented", "saved", "verified"}}.items():
        if value.get(key) not in tuple(choices):
            return key + " must be one of " + str(sorted(choices))
    for key, kind in {"unit_met": bool, "summary": str}.items():
        if type(value.get(key)) is not kind:
            return key + " must be " + kind.__name__
    lists = ("remaining", "evidence_refs", "facts", "materials", "requirement_checks", "artifacts",
             "invalidated_work_item_ids")
    for key in lists:
        if not isinstance(value.get(key), list):
            return key + " must be an array (use [] for no updates)"
    for key in ("remaining", "evidence_refs", "invalidated_work_item_ids"):
        if any(not isinstance(x, str) for x in value[key]):
            return key + " must contain only strings"
    if value.get("progress_kind", "none") not in {
            "none", "navigation", "source", "content", "save", "inspection"}:
        return "progress_kind must describe observed navigation, source, content, save or inspection"
    if not isinstance(value.get("observed_errors", []), list):
        return "observed_errors must be an array"
    for error in value.get("observed_errors", []):
        if (not isinstance(error, dict) or any(not isinstance(error.get(k), str) for k in (
                "message", "application", "command_name")) or not error["message"].strip()
                or not isinstance(error.get("evidence_refs"), list)
                or any(not isinstance(ref, str) for ref in error["evidence_refs"])):
            return "observed_errors need exact message, application, command_name and evidence_refs"
    check = value.get("unit_assessment")
    if "unit_assessment" in value and (not isinstance(check, dict)
            or not isinstance(check.get("work_item_id"), str)
            or check.get("status") not in {"met", "not_met", "uncertain", "not_assessed"}
            or any(not isinstance(check.get(k), list) or any(not isinstance(x, str) for x in check[k])
                   for k in ("remaining", "evidence_refs"))):
        return "unit_assessment needs work_item_id, status, remaining and evidence_refs"
    if not isinstance(value.get("policy_violation", ""), str):
        return "policy_violation must be a string"
    if not isinstance(value.get("policy_constraint", ""), str):
        return "policy_constraint must be a string"
    if (not isinstance(value.get("policy_evidence_refs", []), list)
            or any(not isinstance(ref, str) for ref in value.get("policy_evidence_refs", []))):
        return "policy_evidence_refs must be an array of strings"
    if not isinstance(value.get("milestones", []), list):
        return "milestones must be an array"
    for record in value.get("milestones", []):
        if (not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k].strip()
                                               for k in ("key", "summary"))
                or not isinstance(record.get("evidence_refs"), list)
                or any(not isinstance(ref, str) for ref in record["evidence_refs"])):
            return "milestones need a stable key, summary and evidence_refs array of strings"
    fields = {"facts": ("key", "value", "source_ref"), "materials": ("source_ref", "summary"),
              "requirement_checks": ("id",), "artifacts": ("target_path", "working_path")}
    enums = {"facts": {"status": {"confirmed", "uncertain"}, "scope": {"task", "stage"}},
             "requirement_checks": {"status": {"met", "not_met", "uncertain"}},
             "artifacts": {"save_state": {"modified", "saved"}}}
    for collection, strings in fields.items():
        for index, record in enumerate(value[collection]):
            path = f"{collection}[{index}]"
            if not isinstance(record, dict):
                return path + " must be an object"
            for key in strings:
                if not isinstance(record.get(key), str):
                    return path + "." + key + " must be a string"
            arrays = ("evidence_refs", "covered_parts") if collection == "materials" else ("evidence_refs",)
            for key in arrays:
                if not isinstance(record.get(key), list) or any(not isinstance(x, str) for x in record[key]):
                    return path + "." + key + " must be an array of strings"
            for key, choices in enums.get(collection, {}).items():
                if record.get(key) not in tuple(choices):
                    return path + "." + key + " must be one of " + str(sorted(choices))
            if collection == "facts":
                if "complete" in record and type(record["complete"]) is not bool:
                    return path + ".complete must be a boolean"
                if "entries" in record and (not isinstance(record["entries"], dict) or any(
                        not isinstance(k, str) or not isinstance(v, str)
                        for k, v in record["entries"].items())):
                    return path + ".entries must map strings to strings"
    return ""


def valid_result(value):
    return not result_error(value)


def _excerpt(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[... omitted source content; not inspected ...]\n" + text[-limit // 2:]


def _actions(history, output_budget):
    """Keep receipts and semantic deltas, not repeated snapshot IDs/ancestry/whole trees."""
    actions = []
    previous = {}
    output_records = max(1, sum(bool(r.get("full_output_ref") or r.get("stdout") or r.get("stderr"))
                                for r in history))
    limit = max(1000, output_budget // output_records)
    for record in history:
        action = {k: record[k] for k in ("evidence_ref", "kind", "operation", "target", "returncode",
                                        "observed_change", "relevant_change", "target_effect",
                                        "full_output_ref") if k in record}
        if record.get("kind") != "cli" and record.get("action"):
            action["action"] = record["action"]
        output = dict(record)
        if record.get("full_output_ref"):
            output.update(json.loads(Path(record["full_output_ref"]).read_text(encoding="utf-8")))
        omitted = {}
        for key in ("stdout", "stderr"):
            text = output.get(key, "")
            if text:
                action[key] = _excerpt(text, limit)
                if len(text) > limit:
                    omitted[key] = len(text) - limit
        if omitted:
            action["output_omitted_chars"] = omitted
        changes, current = [], {}
        for control in record.get("observed_controls", []):
            key = (control.get("role"), control.get("name"), control.get("x"), control.get("y"))
            value = {k: control[k] for k in ("role", "name", "value", "focused", "checked", "selected")
                     if k in control}
            current[key] = value
            if value != previous.get(key):
                changes.append(value)
        previous = current
        if changes:
            action["control_changes"] = changes
        actions.append(action)
    return actions


def _failure_result(memory, history, end_reason, evidence, error=None):
    """No semantic success claim from a known error or an entirely ineffective local attempt."""
    pure_no_effect = (history and all(h.get("kind") == "gui"
                                     and not h.get("relevant_change", h.get("observed_change"))
                                     for h in history))
    skip = (error is not None or end_reason == "execution_error" or end_reason.startswith((
                "stage_inputs_missing:", "stage_input_conflict:", "capability_missing:"))
            or not history and end_reason != "stage_done_requested"
            or pure_no_effect and not any(h.get("operation") == "type_text" for h in history)
            and memory.active_stage["purpose"] not in {"save", "verify"})
    if not skip:
        return None
    error = error or next((h.get("stderr") or h.get("stdout") for h in reversed(history)
                          if h.get("returncode")), "")
    return {"stage_outcome": "uncertain", "unit_met": False, "delivery_state": "none",
            "summary": "No semantic acceptance: " + end_reason
                       + (". Observed error: " + error[-1200:] if error else ""),
            "remaining": [end_reason + ": inspect partial effects and change method; do not repeat it."],
            "evidence_refs": list(evidence)[-1:], "facts": [], "materials": [],
            "requirement_checks": [], "artifacts": [], "invalidated_work_item_ids": [],
            "check_mode": "local_failure"}


def verify_goal(client, instruction, screenshot, *, memory, history, end_reason, evidence):
    local = _failure_result(memory, history, end_reason, evidence)
    if local is not None:
        local["failure_kind"] = end_reason
        return local
    stage = memory.active_stage
    work = memory.work_items[stage["work_item_id"]]
    targets = stage.get("fact_targets", work["facts_needed"])
    keys = set(stage["input_fact_keys"]) | {f["key"] for f in targets}
    budget = client.config.stage_verify_max_chars
    actions = _actions(history, budget // 2)
    preparing = stage["purpose"] == "prepare"
    template = {"stage_outcome": "uncertain", "unit_met": False, "delivery_state": "none",
                "summary": "Describe current observed evidence", "remaining": [], "evidence_refs": [],
                "facts": [], "materials": [], "requirement_checks": [], "artifacts": [],
                "invalidated_work_item_ids": [], "milestones": [], "policy_violation": "",
                "progress_kind": "none", "observed_errors": []}
    if not preparing:
        template["unit_assessment"] = {"work_item_id": work["id"], "status": "uncertain",
                                       "evidence_refs": [], "remaining": []}
    context = {"task": instruction,
               "response_template": template,
               "stage_acceptance": {"criterion": stage["completion_criteria"], "scope": "ended_stage_only"},
               "unit_acceptance": ({"work_item_id": work["id"], "scope": "not_assessed_during_prepare"}
                                   if preparing else {"work_item_id": work["id"],
                                       "criterion": work["completion_criteria"],
                                       "scope": "this_fixed_unit_only_not_all_original_requirements"}),
               "sibling_units_not_current_acceptance": [{"id": w["id"], "goal": w["goal"]}
                   for w in memory.work_items.values() if w["kind"] == "deliver" and w["id"] != work["id"]
                   and w["requirement_ids"] == work["requirement_ids"]],
               "stage": {k: v for k, v in stage.items() if k != "command"},
               "command_excerpt": _excerpt(stage.get("command", ""), 3000),
               "fixed_work_item": {**work, "facts_needed": targets},
               "requirements": [{k: r[k] for k in ("id", "requirement", "kind")}
                                for r in memory.requirements if r["id"] in work["requirement_ids"]
                                or r["kind"] == "constraint"],
               "known_facts": [f for f in memory.facts.values() if f["key"] in keys],
               "end_reason": end_reason, "actual_actions": actions, "evidence_ids": list(evidence),
               "historical_evidence_ids": sorted(memory.known_evidence)}
    text = json.dumps(context, ensure_ascii=False)
    if len(text) > budget:
        # Receipt deltas stay intact. Explicitly excerpt source text, never silently claim full coverage.
        for index, original in enumerate(context["known_facts"]):
            fact = {k: v for k, v in original.items() if k != "entries"}
            fact["value"] = _excerpt(fact["value"], max(1000, budget // (2 * max(1, len(keys)))))
            if fact["value"] != original["value"]:
                fact["value_excerpt_only"] = True
            context["known_facts"][index] = fact
        text = json.dumps(context, ensure_ascii=False)
    content = [{"type": "text", "text": text}]
    source_frames = [h["screenshot"] for h in history if h.get("source_frame")]
    paths = source_frames[-5:] if stage["purpose"] == "prepare" else (
        [history[0]["screenshot"]] if history else [])
    frames = [Path(p) for p in dict.fromkeys(paths + [str(screenshot)])]
    for path in frames:
        content.extend([{"type": "text", "text": "Observed screenshot: " + str(path)},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                         + base64.b64encode(path.read_bytes()).decode("ascii")}}])
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}]

    def validate(value):
        error = result_error(value)
        if not error:
            check = value.get("unit_assessment", {})
            required = ("progress_kind", "observed_errors") + (() if preparing else ("unit_assessment",))
            if not all(key in value for key in required):
                error = "include mandatory fields: " + ", ".join(required)
            elif preparing and (value["unit_met"] or value["delivery_state"] != "none"
                                or value.get("milestones")):
                error = "prepare cannot accept a delivery unit or claim delivery progress"
            elif not preparing and check.get("work_item_id") != work["id"]:
                error = "unit_assessment.work_item_id must be " + work["id"]
            elif not preparing and (check.get("status") == "met") != value["unit_met"]:
                error = "unit_met must agree with this unit_assessment, not whole-task completion"
            elif value["unit_met"] and value["delivery_state"] != "verified":
                error = "a met delivery unit requires verified evidence for its own fixed criterion"
            elif value["progress_kind"] in {"navigation", "source", "none"} and (
                    value["unit_met"] or value["delivery_state"] != "none" or value.get("milestones")):
                if value["unit_met"] or value.get("milestones"):
                    error = "navigation/source/no delivery cannot claim units or delivery milestones"
                else:
                    value["historical_delivery_claim"] = value["delivery_state"]
                    value.update(delivery_state="none", artifacts=[])
        if not error:
            allowed = set(evidence) | memory.known_evidence
            records = [value, *value["facts"], *value["materials"],
                       *value["artifacts"], *value["requirement_checks"], *value.get("milestones", []),
                       *value.get("observed_errors", [])]
            if not preparing:
                records.append(value["unit_assessment"])
            unknown = sorted({ref for r in records for ref in r["evidence_refs"] if ref not in allowed})
            if unknown:
                error = "evidence_refs contain unregistered IDs: " + json.dumps(unknown)
            elif value["stage_outcome"] == "met" and not set(value["evidence_refs"]) & set(evidence):
                error = "a met stage must cite at least one CURRENT evidence ID"
            elif any(not set(item["evidence_refs"]) or not set(item["evidence_refs"]) <= set(evidence)
                     for item in value["observed_errors"]):
                error = "observed_errors must cite CURRENT observed evidence"
            elif value["unit_met"] and not set(value["unit_assessment"]["evidence_refs"]) & set(evidence):
                error = "a met unit must cite CURRENT evidence for its fixed criterion"
        if error:
            messages.append({"role": "user", "content":
                "Invalid stage verification JSON: " + error + ". Return the COMPLETE corrected object "
                "with every mandatory key, without changing the observed facts or inventing evidence."})
        return not error

    try:
        result = client.json_request(messages, validate, role="verify_stage")
    except ModelResponseError as error:
        result = _failure_result(memory, history, "verification_output_invalid", evidence, str(error))
        return {**result, "failure_kind": "verification_output_invalid"}
    result = deepcopy(result)
    if preparing:
        result["unit_assessment"] = {"work_item_id": work["id"], "status": "not_assessed",
                                     "evidence_refs": [], "remaining": []}
    if result.get("progress_kind") in {"navigation", "source", "none"}:
        result.update(delivery_state="none", unit_met=False, milestones=[])
    if stage["route"] == "cli" and stage["purpose"] in {"produce", "save"}:
        if result["delivery_state"] == "verified":
            result.update(unit_met=False, delivery_state="saved",
                          acceptance_limited="cli_save_not_gui_acceptance")
    if result.get("policy_violation"):
        constraint = result.get("policy_constraint", "").strip()
        refs = set(result.get("policy_evidence_refs", []))
        current_actions = {h["evidence_ref"] for h in history if h.get("evidence_ref") in evidence}
        original = (len(constraint) >= 12 and constraint in instruction and any(
            token in constraint.casefold() for token in ("not ", "never ", "forbid", "禁止", "不得")))
        integrity = constraint in {"website_source_tampering", "evaluator_access", "credential_access"}
        fatal = bool(refs and refs <= current_actions and (original or integrity))
        result.update(stage_outcome="not_met", unit_met=False, delivery_state="none", milestones=[],
                      artifacts=[], requirement_checks=[], remaining=[result["policy_violation"]],
                      failure_kind="policy_violation" if fatal else "recoverable_stage_error")
        if fatal:
            result.update(facts=[], materials=[])
    result.update(check_mode="source" if stage["purpose"] == "prepare" else "delivery",
                  context_chars=len(text), image_count=len(frames))
    return result
