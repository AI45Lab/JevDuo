"""Compact role-specific views. Raw receipts/screenshots remain in the episode record."""

import re


def _fact_view(fact, limit=2400):
    view = {k: v for k, v in fact.items() if k not in {"observations", "entries"}}
    if len(view["value"]) > limit:
        view["value"] = (view["value"][:limit // 2]
                         + "\n[excerpt; request read_memory for the stored content]\n"
                         + view["value"][-limit // 2:])
        view["value_excerpt_only"] = True
    return view


def planning_context(memory):
    ready = memory.ready_items()[:4]
    keys = {f["key"] for item in ready for f in (
        memory.preparation_target(item)[0]["facts_needed"] if item["kind"] == "prepare"
        else item["facts_needed"])}
    facts = [f for f in memory.facts.values() if f["scope"] == "task"]
    auxiliary = {f["key"] for f in facts[-8:]}
    visible = [_fact_view(f) for f in facts if f["key"] in keys | auxiliary]
    return {"requirements": [{k: r[k] for k in ("id", "requirement", "kind", "status")}
                             for r in memory.requirements],
            "ready_work_items": [{**item, "can_start_partial": item["kind"] == "deliver",
                "next_acquisition": {
                "unit": memory.preparation_target(item)[0]["goal"],
                "missing_facts": memory.preparation_target(item)[1]}}
                for item in ready],
            "completed_work": [i["id"] for i in memory.work_items.values() if i["status"] == "done"],
            "known_facts": visible,
            "fact_index": [{**{k: f[k] for k in ("key", "status", "source_ref")},
                            "complete": f.get("complete", True)} for f in facts],
            "historical_ui_notes": [f for f in memory.facts.values() if f["scope"] == "stage"][-6:],
            "materials": [{"source_ref": m["source_ref"], "covered_parts": m["covered_parts"],
                           "summary_excerpt": m["summary"][:1800],
                           "summary_excerpt_only": len(m["summary"]) > 1800}
                          for m in list(memory.materials.values())[-8:]],
            "artifacts": list(memory.artifacts.values()), "failures": memory.failures[-12:],
            "pending_publications": [a for a in memory.artifacts.values() if a.get("delivery_pending")],
            "unsupported_methods": [{**f["observed_method"], "observed_error": f["error"],
                                     "evidence_refs": f["evidence_refs"]}
                                    for f in memory.failures if f.get("observed_method")],
            "observed_missing_modules": sorted({name for f in memory.failures for name in
                re.findall(r"No module named ['\"]([^'\"]+)['\"]", f["error"])}),
            "execution_constraints": memory.execution_constraints[-12:],
            "active_blocker": memory.active_blocker,
            "memory_update_rejections": memory.last_rejections,
            "last_interaction_historical": memory.last_interaction,
            "stages_without_progress": memory.stats["stages_without_progress"]}


def execution_context(memory):
    stage = memory.active_stage
    item = memory.work_items[stage["work_item_id"]]
    return {"goal": stage["goal"], "completion_criteria": stage["completion_criteria"],
            "method": stage["method"],
            "constraints": [r["requirement"] for r in memory.requirements if r.get("kind") == "constraint"],
            "inputs": stage["inputs"], "guidance": stage["guidance"],
            "target_application": stage.get("target_application"),
            "facts": [_fact_view(memory.facts[k]) for k in stage["input_fact_keys"] if k in memory.facts],
            "delivery_state": item["delivery_state"], "remaining": item["remaining"],
            "completed_milestones": item["milestones"],
            "failures": [f for f in memory.failures if f["work_item_id"] == item["id"]][-4:],
            "execution_constraints": [c for c in memory.execution_constraints
                                      if c["work_item_id"] == item["id"] or c.get("global_scope")][-6:],
            "active_blocker": memory.active_blocker,
            "actions_remaining": stage["max_actions"] - stage["actions"],
            "control": "Choose each next action from the NEW observation. DONE requests stage verification, "
                       "not whole-task completion. Do not repeat satisfied input or toggle states."}


def verification_context(memory):
    """Independent stage evidence remains available at whole-task verification."""
    return {"requirements": memory.requirements,
            "work_items": [{k: item[k] for k in ("id", "goal", "status", "delivery_state", "remaining",
                                                "evidence_refs", "milestones")}
                           for item in memory.work_items.values() if item["kind"] == "deliver"],
            "source_facts": [_fact_view(f) for f in memory.facts.values()
                             if f["scope"] == "task" and f["status"] == "confirmed"],
            "artifacts": list(memory.artifacts.values()),
            "active_blocker": memory.active_blocker}
