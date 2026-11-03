"""One self-contained subgoal, not a speculative list of future UI actions."""

import base64
import json
import re

from ..memory.context import _fact_view, planning_context

REQUIREMENTS = """Extract the ORIGINAL task into JSON {requirements:[{id,requirement,output_path,
kind,units,unit_fact_keys,facts_needed}]}. kind is delivery or constraint. units is a list of STRINGS with
delivery units with their completion criteria; enumerate explicitly requested pages/items, otherwise
use one unit. Constraints have units:[]. facts_needed is [{key,description}] naming only essential
stable source information acquired from supplied materials; reuse the SAME key across requirements.
Never register current focus, open windows or selection as required source facts. Split large source
packets into the concrete fields or small sections needed by individual deliverables. unit_fact_keys
is an array parallel to units: each entry lists ONLY the facts_needed keys required by THAT unit.
Keys may reference definitions in ANY requirement; define each shared source key once. Use [] for a
unit needing no source reading. Constraints MUST have units:[] AND unit_fact_keys:[], not [[]].
Include a constraint's required source key on the delivery units that must satisfy that constraint.
A source-reading dependency must not gate unrelated work.
Do not list facts already stated in the instruction as facts_needed. Names, titles, URLs and target
systems explicitly provided by the user are inputs, not missing source facts.
Keep all original quantities, locations, prohibitions, interaction requirements and saving conditions.
Enumerate the individual pages/slides/form sections as small delivery units, including when their count
is explicit but their names are not. Do not collapse 'all 26 slides' into one giant unit. Each unit
lists only its own missing source content; whole-document consistency remains a final constraint.
output_path is null unless literally given as an absolute output path in the instruction.
Do not prescribe actions, guess source facts, inspect evaluators, or introduce hidden grading criteria.
Return a compact contract, no reasoning, no speculative extra work."""

REQUIREMENTS += '''
Every requirement MUST contain ALL seven keys, including a plain-string "requirement" describing the
actual obligation (also for constraints). units MUST be an array of STRINGS, never objects.
Requirement IDs must be nonempty and UNIQUE across the complete contract, including constraints.
Exact shape example: {"requirements":[{"id":"R1","requirement":"Create and save the requested document",
"output_path":null,"kind":"delivery","units":["Requested document is complete and saved"],
"unit_fact_keys":[[]],
"facts_needed":[]}]}. This example illustrates the format only; extract the real task's obligations.
'''

PLAN = """Delegate ONE meaningful bounded subtask using the host's task memory. You are the planner;
the fast GUI choice model will autonomously observe and act, not follow an action list. Return JSON:
{done:false,work_item_id:"exact ready id",route:"gui|cli",purpose:"prepare|produce|save|verify",
goal:"specific result",completion_criteria:"stage exit condition",method:"concise approach",
input_fact_keys:["known key"],inputs:[{label:"field meaning",target_hint:"observed field label",
text:"exact supplied text",
input_mode:"replace|insert"}],guidance:"brief necessary navigation/context, not an ordered click script",
max_actions:24,command:"CLI only; otherwise empty string",
recovery_strategy:"continue|inspect_state|restore_context|alternate_interaction|alternate_route"}.
When ready_work_items[].recovery_required is true, choose an explicit recovery_strategy other than
continue. Changing the wording of a goal/method does not change the execution path. The host retains
failed navigation families for the observed execution scope across stages; unrelated source facts do
not release them. Restore the execution context, inspect a missing capability, use another interaction
or switch route only when the original task permits it. Never repeat an already-executed action merely
because a receipt or observation is missing.
Use a ready work item's FIXED goal and original requirements. Do not rename units or lower final criteria.
Missing source facts are acquisition targets, NOT a hard gate on starting a delivery unit. First make
and save the useful portion supported by known facts; leave unknown values unresolved, never guess them.
Choose a bounded incremental stage exit condition, not the whole unit's acceptance criteria. A partial
stage may succeed while the unit remains incomplete. Read a missing source only before the next action
actually needs it, then resume the preserved artifact. Respect explicit user-required reading order.
input_fact_keys may name ONLY confirmed task-scope keys present in memory.known_facts, NOT keys that
are merely requested in facts_needed. Use [] before any source facts have been observed.
For prepare, acquire only missing essential facts; do not reread facts already in memory. Source notes
are historical evidence, not guaranteed current UI. Reuse confirmed task facts. Temporary inventory,
selection and page state must be reobserved. Do not fabricate a URL or missing personal information.
Bound visual source reading to one small section at a time (normally 4-6 observations); the stage-end
verifier will record its contents. Do not delegate reading several documents and navigating a new site
as a single preparation goal. Later preparation continues from materials.covered_parts, not page one.
For GUI give a whole local goal (a form section, adding an attachment, a settings operation), normally
8-24 possible actions, not merely 'open menu'. Bound editing to the CURRENT page/form section or slide.
Stop before a new page requires new values or reasoning; plan it from its actual observation next.
Give only ONE current value per target_hint. Reading multiple URLs in the same address bar requires
separate stages, not competing replacement inputs. A typed navigation URL still requires Enter.
A file chooser may be completed with an already supplied exact path. Never delegate uploading while
the current form still needs unknown required fields. First fill the known current fields, then stop
to acquire only the missing values. Provide ALL exact text values from the instruction or known facts.
For every text input, target_hint must identify its intended observed field (copy the visible label
when available). For browser navigation use the observed address-bar label; for a document body or
desktop launcher identify that specific editor/search field. Text is never interchangeable among fields.
The choice model cannot generate free text. It can click, type provided inputs, use keyboard, scroll,
wait, request stage verification, or return control. Missing data/new reasoning requires another stage.
For CLI provide one bounded Bash command, current inputs and expected real results. Prefer actually
producing/saving deliverables over repeatedly reading tutorials, building audits or protecting unchanged
files. Inspect edited documents in the actual application before claiming them verified. Keep source
and final paths distinct. CLI is forbidden if allow_cli=false. Task constraints override routing choices.
Use installed tools; do not assume OCR binaries or passwordless sudo. Do not install packages as a
substitute for source reading. Keep commands valid Bash, including background-command separators.
memory.observed_missing_modules records actual import failures: use an available alternative, not the
same missing package again. Derive edit preconditions from observed document structure, not guessed
replacement counts. After an assertion fails, narrow the next edit using the real error and partial output.
Do not inspect evaluator code, ground truth, rewards, hidden state, unrelated secrets or scoring APIs.
Previous methods/errors are in memory; changing their description does not make them new. If stalled,
choose another method or another independent ready unit. Avoid optional research and cosmetic detours.
Only return {done:true,task_status:"completed|infeasible",current_goal:"evidence-based reason"} for an
actual whole-task terminal claim. Stage completion is NOT task completion. A blocked unit is not proof
the task is infeasible. Keep the response short: goal, inputs and one bounded command, no long analysis."""

PLAN += """
Treat capability_missing and unresolved execution scopes as prerequisites, not wording problems.
memory.pending_publications are edited working copies not yet delivered. Publish a useful saved increment
to the observed user deliverable and reopen it before starting more optional research or another edit.
Saving /tmp files is not delivery. After a bounded edit, prefer publishing that increment rather than
spending the remaining budget on tiny edits in an unpublished copy. Do not overwrite it from a stale GUI.
Keep a valuable unsaved form in its own tab when opening a reference; explicitly preserve that context.
Use remaining_wall_seconds as a real deadline. Publish a useful current increment early; near the deadline,
prefer final-path saving/reopening over optional source exploration or another tiny unpublished edit.
Partial work is not task completion: retain unmet requirements and let official evaluation judge it.
Opening a file means it is loaded and usable in the requested application. Complete necessary native
open/import confirmations; handing off a path or leaving an import preview is not the finished handoff.
memory.active_blocker is the current observed execution obstruction, shared across work units.
Changing its name, unit or recovery_strategy does not resolve it. GUI recovery must expose an actually
available target/interaction; after grounding is exhausted use existing observed controls or another
allowed route. No new speculative coordinates or repeated requests for the exhausted capability.
Give goal as an action-oriented bounded objective, and completion_criteria as a SEPARATE desired
observable outcome. An exit condition is not an observed fact. Do not invent folders or navigation
capabilities: a page number printed in source content does not establish a viewer pagination control.
Resolve the active obstruction in ONE bounded prepare stage before proposing edits behind it. A business
dialog needed for the task must be completed, not dismissed. Do not reissue an unbound input unchanged:
first navigate to expose its target, acquire a supported interaction, or use another allowed route.
After CLI edits, the already-open GUI may be stale. Close/reopen the saved working artifact without
overwriting it from the old GUI buffer before visual acceptance. A title alone does not establish freshness.
Prefer a ready delivery unit over more preparation. Preparation is bounded acquisition for ONE missing
field or source section, not an all-document audit or a pixel-analysis research project. State the exact
observable subgoal, never 'Acquire the required source facts'. Reuse saved extractions and artifacts.
After two stages without new required facts or delivery progress, use a genuinely different method or
another ready unit. Do not add optional diagnostic outputs as new completion requirements. Make and
save a usable increment as soon as its inputs are known; preserve all original final requirements.
historical_ui_notes are prior observations, NOT the current interface. Current screenshot/controls win.
For prepare, follow next_acquisition: acquire the stated missing registered fields for that ONE unit.
Do not inventory every dropdown or inspect source code before filling fields whose values are known.
Use exact registered keys for acquired contents; fact_index lists already observed auxiliary sources,
not new dependencies. Do not reread auxiliary sources solely because their full text is omitted here.
Never type into an unrelated/Untitled document. Confirm the target application and document first.
For editing an existing document, include target_document:"exact filename" on GUI produce/save stages.
If it is not currently open in the foreground, first use a prepare stage to open/switch to it. A
navigation-only prepare stage may have no source fact targets. For websites or creating a NEW document,
target_document may be null. Available GUI operations include right-click, double-click, Escape,
Alt+Tab, Ctrl+O, keyboard navigation, supplied text, scrolling and screenshot-grounded targets.
known_facts includes partial and auxiliary source content; complete:false is usable for a partial
increment but does NOT mean the source or unit is complete. Never guess ownership/identity when joining
records from different sources. Do not keep preparing solely to make every facts_needed entry complete.
If an observed fact is indexed but its text is omitted/excerpted, return ONLY {read_memory:["key",...]}
for up to six keys, at most twice before planning. This reads stored evidence without reopening files.
Failure constraints, remaining work and rejected memory updates are actionable state, not suggestions
to repeat the same attempt with different wording. When the executor reports a blocked cycle, choose
another interaction, another source or an independent ready unit.
For a GUI stage that needs to open or switch applications, include target_application:"exact app name".
This explicit input will also be available for typing in the desktop launcher; do not leave it only in
prose guidance. Set it to null when not applicable. Prefer CLI to open an installed application or a
known document when allowed, then delegate actual UI editing to JEV. Do not alternate the application
drawer open/closed or repeat a covered target. Resolve the visible overlay or use another interaction.
completed milestones are delivered sections, not instructions to recreate them. Continue with an unmet
section/field group even when the whole unit is incomplete; do not re-audit completed increments.
For native input_type=date, provide ISO YYYY-MM-DD from the task/source. The executor edits the observed
Day/Month/Year segments separately; never concatenate a guessed locale's month/day/year digits.
When a date is still empty after typing, use its calendar controls or revise the input; don't retype it.
Two consecutive CLI execution failures require one bounded read-only structure inspection or GUI work
before another write. Use actual object identities; do not weaken assertions to obtain a pass.
memory.artifacts is the cumulative working-copy registry: resume its existing working_path for the
same target_path, not a fresh copy of the original on each stage. Save increments to that same artifact.
Do not replace editable slides, animations or native objects with full-page reference screenshots.
Treat website application code as read-only unless the user explicitly asks to develop that application.
Do not edit HTML/JS source, inject bootstraps, add options, remove fields or bypass validation to fill
a website. Use its real UI; ordinary CLI editing of the requested output document remains allowed.
For a supplied command entered into an application's command/input bar, add kind:"command",commit:"enter"
to that input. Ordinary text uses kind:"text",commit:null; a known selection uses kind:"choice".
These describe one observed control interaction, not a sequence of speculative future clicks.
unsupported_methods records ACTUAL application errors, not suggestions. Do not keep changing arguments
of an unrecognized command; use another supported interaction in the same required application.
Memory failures.error is observed evidence; suggested_actions is only a hypothesis. Check it against
the real application before treating it as valid syntax. Preserve already confirmed source content.
Window opening/maximization/menu navigation are prepare, not produce or saved-delivery milestones.
Keep the stage exit condition separate from the fixed unit criterion and whole-task requirements.
"""


def goal_requirements(client, instruction):
    messages = [{"role": "system", "content": REQUIREMENTS}, {"role": "user", "content": instruction}]

    def valid(v):
        okay = (isinstance(v.get("requirements"), list) and bool(v["requirements"])
                and all(isinstance(r, dict) and isinstance(r.get("id"), str)
                        and isinstance(r.get("requirement"), str)
                        and r.get("kind") in {"delivery", "constraint"}
                        and isinstance(r.get("units"), list)
                        and (r.get("kind") == "constraint" or bool(r["units"]))
                        and all(isinstance(u, str) for u in r["units"])
                        and isinstance(r.get("unit_fact_keys"), list)
                        and len(r["unit_fact_keys"]) == len(r["units"])
                        and all(isinstance(keys, list) and all(isinstance(k, str) for k in keys)
                                for keys in r["unit_fact_keys"])
                        and isinstance(r.get("facts_needed"), list)
                        and all(isinstance(f, dict) and isinstance(f.get("key"), str)
                                and isinstance(f.get("description"), str) for f in r["facts_needed"])
                        for r in v["requirements"]))
        if okay:
            ids = [r["id"].strip() for r in v["requirements"]]
            if not all(ids) or len(set(ids)) != len(ids):
                messages.append({"role": "user", "content":
                    "Duplicate or empty requirement id. Give EVERY requirement a unique nonempty id; "
                    "preserve ALL delivery obligations and return the COMPLETE corrected contract."})
                return False
            registered = {f["key"] for r in v["requirements"] for f in r["facts_needed"]}
            okay = all(set(keys) <= registered for r in v["requirements"] for keys in r["unit_fact_keys"])
        if not okay:
            messages.append({"role": "user", "content":
                "Invalid requirements JSON. Every entry needs id, requirement (plain text, mandatory), "
                "output_path, kind, units (array of STRINGS, not objects), unit_fact_keys "
                "(one array of source key strings per unit) and facts_needed "
                "(array of {key,description}). Include constraint text in requirement; units:[] is not "
                "a substitute. Constraints need units:[] AND unit_fact_keys:[], not [[]]. "
                "Shared source keys may be defined in any requirement. Return the COMPLETE object "
                "{requirements:[...]}, not an isolated fact or requirement."})
        return okay

    value = client.json_request(messages, valid, role="task_requirements")
    for r in value["requirements"]:
        path = r.get("output_path")
        if not isinstance(path, str) or not path.startswith("/") or path not in instruction:
            r["output_path"] = None
    return value["requirements"]


def goal_error(value, memory, allow_cli):
    """Name the rejected field/constraint so JSON repair can actually change the request."""
    if type(value.get("done")) is not bool:
        return "done must be a boolean"
    if value["done"]:
        return ("" if value.get("task_status") in ("completed", "infeasible")
                and isinstance(value.get("current_goal"), str) and value["current_goal"].strip()
                else "terminal claims require task_status and a nonempty current_goal")
    ready = {i["id"]: i for i in memory.ready_items()}
    if not isinstance(value.get("work_item_id"), str):
        return "work_item_id must be a string naming an exact ready unit"
    item = ready.get(value.get("work_item_id"))
    if item is None:
        return "work_item_id must be one of " + json.dumps(list(ready))
    strategy = value.get("recovery_strategy", "continue")
    if strategy not in {"continue", "inspect_state", "restore_context", "alternate_interaction",
                        "alternate_route"}:
        return "recovery_strategy must name a supported execution recovery"
    if item.get("recovery_required") and strategy == "continue":
        return "execution recovery is required; declare how the blocked path will change"
    if item.get("stalled_stages", 0) >= 2:
        previous = item.get("last_attempt", {})
        if (value.get("route") == previous.get("route") == "cli"
                and isinstance(value.get("command"), str)
                and value["command"].strip() == previous.get("command", "").strip()):
            return "command repeats the same CLI attempt after two stalled stages; change method or work unit"
    if value.get("route") not in (("gui", "cli") if allow_cli else ("gui",)):
        return "route must be gui" + (" or cli" if allow_cli else "; CLI is disabled")
    purposes = ("prepare",) if item["kind"] == "prepare" else ("prepare", "produce", "save", "verify")
    if value.get("purpose") not in purposes:
        return "purpose must be one of " + json.dumps(sorted(purposes))
    if (item.get("failed_cli_stages", 0) >= 2 and value.get("route") == "cli"
            and value.get("purpose") in {"produce", "save"}):
        return ("two CLI executions failed; inspect structure read-only in prepare "
                "or use GUI before writing")
    for key in ("goal", "completion_criteria", "method"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            return key + " must be a nonempty string"
    if not isinstance(value.get("guidance"), str):
        return "guidance must be a string (empty allowed)"
    for key in ("target_document", "target_application"):
        if value.get(key) is not None and (not isinstance(value[key], str) or not value[key].strip()):
            return key + " must be a nonempty string or null"
    app_words = re.findall(r"\w{3,}", (value.get("target_application") or "").casefold())
    blockers = [c for c in memory.execution_constraints if c.get("kind") == "execution_checkpoint"
                and c.get("global_scope") and c.get("recovery_required")
                and (not app_words or any(w in json.loads(c["scope_key"])[0].casefold() for w in app_words))]
    if blockers and value.get("route") == "gui" and value.get("purpose") in {"produce", "save"}:
        return "resolve the active GUI input obstruction in a bounded prepare stage before editing"
    if type(value.get("max_actions")) is not int or not 1 <= value["max_actions"] <= 24:
        return "max_actions must be an integer in 1..24"
    allowed = [k for k, f in memory.facts.items() if f["status"] == "confirmed" and f["scope"] == "task"]
    if not isinstance(value.get("input_fact_keys"), list) or any(
            not isinstance(k, str) or k not in allowed for k in value["input_fact_keys"]):
        return ("input_fact_keys must contain only acquired confirmed task keys: " + json.dumps(allowed)
                + "; copy keys EXACTLY, including dots and punctuation; do not normalize filenames. "
                "Use [] if none are needed. Rejected keys: " + json.dumps(value.get("input_fact_keys")))
    if not isinstance(value.get("inputs"), list):
        return "inputs must be an array (empty allowed)"
    input_values = {}
    for index, entry in enumerate(value["inputs"]):
        if (not isinstance(entry, dict) or not isinstance(entry.get("label"), str)
                or not isinstance(entry.get("text"), str)
                or entry.get("input_mode") not in ("replace", "insert")):
            return f"inputs[{index}] requires label:string, text:string, input_mode:replace|insert"
        if "target_hint" in entry and (not isinstance(entry["target_hint"], str)
                                       or not entry["target_hint"].strip()):
            return f"inputs[{index}].target_hint must identify the intended field"
        if entry.get("kind", "text") not in {"text", "command", "choice"}:
            return f"inputs[{index}].kind must be text, command or choice"
        if entry.get("commit") not in (None, "enter"):
            return f"inputs[{index}].commit must be enter or null"
        blocked = memory.blocked_command(entry["text"], value.get("target_application"))
        if blocked and value["route"] == "gui":
            return blocked
        hint = entry.get("target_hint", entry["label"]).strip().casefold()
        if value["route"] == "gui" and hint in input_values and input_values[hint] != entry["text"]:
            return f"inputs assign conflicting texts to {hint!r}; delegate one current value/URL per stage"
        input_values[hint] = entry["text"]
    previous = item.get("last_attempt", {})
    if (item.get("stalled_stages", 0) >= 2
            and previous.get("failure_kind", "").startswith("capability_missing:")
            and value["route"] == "gui" and value["inputs"]
            and [i["text"] for i in value["inputs"] if i.get("scope") != "desktop_launcher"]
            == [i["text"] for i in previous.get("inputs", []) if i.get("scope") != "desktop_launcher"]):
        return "the same input is unsupported; expose its target in a prepare stage or change route"
    if not isinstance(value.get("command"), str):
        return "command must be a string; use an empty string for GUI"
    if value["route"] == "cli" and not value["command"].strip():
        return "command must be a nonempty Bash command for CLI"
    return ""


def valid_goal(value, memory, allow_cli):
    return not goal_error(value, memory, allow_cli)


def plan_goal(client, instruction, screenshot, targets, recent, tree, verified_focus=None,
              requirements=None, remaining_steps=None, *, memory, allow_cli=True, route_policy="auto",
              remaining_seconds=None):
    allow_cli = allow_cli and not re.search(
        r"do not (?:open or use |use |open )?(?:terminal|the terminal)|\bGUI[- ]only\b|\bCLI is forbidden\b",
        instruction, re.I)
    context = {"task": instruction, "memory": planning_context(memory), "allow_cli": bool(allow_cli),
               "route_policy": route_policy, "remaining_actions": remaining_steps,
               "remaining_wall_seconds": remaining_seconds,
               "visible_controls": [t.compact() for t in targets], "recent": recent[-4:]}
    messages = [{"role": "system", "content": PLAN}, {"role": "user", "content": [
        {"type": "text", "text": json.dumps(context, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
         + base64.b64encode(screenshot.read_bytes()).decode("ascii")}},
    ]}]
    reads = 0

    def validate(v):
        if set(v) == {"read_memory"}:
            okay = (reads < 2 and isinstance(v["read_memory"], list) and 1 <= len(v["read_memory"]) <= 6
                    and all(isinstance(k, str) and k in memory.facts and memory.facts[k]["scope"] == "task"
                            for k in v["read_memory"]))
            error = ("read_memory needs 1..6 existing task-scope keys and at most two reads"
                     if not okay else "")
        else:
            error = goal_error(v, memory, allow_cli)
        if error:
            messages.append({"role": "user", "content":
                "Invalid goal-stage JSON: " + error + ". Return the COMPLETE corrected compact stage "
                "object, preserving evidence and original requirements. Do not replay an executed action."})
        return not error

    while True:
        value = client.json_request(messages, validate, role="plan_stage")
        if set(value) != {"read_memory"}:
            application = value.get("target_application")
            if not value["done"] and value["route"] == "gui" and application and not any(
                    i["text"] == application for i in value["inputs"]):
                value["inputs"].append({"label": "Target application name for desktop search",
                                        "target_hint": "Desktop search", "text": application,
                                        "input_mode": "replace", "scope": "desktop_launcher"})
            return value
        reads += 1
        messages.append({"role": "assistant", "content": json.dumps(value)})
        messages.append({"role": "user", "content": json.dumps({
            "stored_source_facts": [_fact_view(memory.facts[k], 16000) for k in value["read_memory"]],
            "instruction": "Use the stored evidence; now return a bounded stage plan. "
                           "Do not reopen these sources just to recover the same content.",
            "memory_reads_remaining": 2 - reads}, ensure_ascii=False)})
