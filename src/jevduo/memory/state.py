"""Host-owned task memory. Plans request work; stage verification commits progress."""

import json
import re
from copy import deepcopy
from pathlib import Path

LEVELS = {"none": 0, "implemented": 1, "saved": 2, "verified": 3}


class TaskMemory:
    def __init__(self, instruction, requirements):
        self.instruction = instruction
        self.requirements = deepcopy(requirements)
        ids = [r["id"].strip() for r in self.requirements]
        if not all(ids) or len(set(ids)) != len(ids):
            raise ValueError("Requirement IDs must be nonempty and unique")
        self.facts = {}
        self.materials = {}
        self.work_items = {}
        self.artifacts = {}
        self.failures = []
        self.execution_constraints = []
        self.active_stage = None
        self.stage_count = 0
        self.revision = 0
        self.known_evidence = set()
        self.last_rejections = []
        self.last_interaction = {}
        self.active_blocker = None
        sources = {f["key"]: f for r in self.requirements for f in r["facts_needed"]}
        self.source_keys = set(sources)
        self.stats = {"stages": 0, "verified_stages": 0, "gui_actions": 0,
                      "cli_actions": 0, "new_facts": 0, "delivery_advances": 0,
                      "source_parts": 0, "milestone_advances": 0, "stages_without_progress": 0}
        for req in self.requirements:
            req.update(status="unknown", evidence_refs=[])
            if req.get("kind") == "constraint":
                continue
            needed = [sources[key] for key in dict.fromkeys(
                key for keys in req["unit_fact_keys"] for key in keys)]
            preparation = f"{req['id']}:prepare"
            if needed:
                self._add(preparation, req, "prepare",
                          "Acquire missing source facts for a ready unit", [], needed)
            for index, unit in enumerate(req["units"], 1):
                keys = req["unit_fact_keys"][index - 1]
                self._add(f"{req['id']}:unit:{index}", req, "deliver", unit, [],
                          [sources[key] for key in keys])

    def _add(self, key, req, kind, goal, dependencies, facts):
        if key in self.work_items:
            raise ValueError("Duplicate work item: " + key)
        self.work_items[key] = {
            "id": key, "requirement_ids": [req["id"]], "kind": kind, "goal": goal,
            "completion_criteria": goal, "depends_on": dependencies,
            "facts_needed": facts, "status": "pending",
            "delivery_state": "none", "remaining": [], "evidence_refs": [],
            "milestones": {}, "failed_cli_stages": 0,
        }

    def _facts_ready(self, item):
        return all(self.facts.get(f["key"], {}).get("status") == "confirmed"
                   and self.facts[f["key"]]["scope"] == "task"
                   and self.facts[f["key"]].get("complete", True) for f in item["facts_needed"])

    def preparation_target(self, item):
        units = [item] if item["kind"] == "deliver" else [
                 w for w in self.work_items.values() if w["kind"] == "deliver"
                 and w["status"] != "done" and w["requirement_ids"] == item["requirement_ids"]]
        choices = [(unit, [f for f in unit["facts_needed"]
                           if not self._facts_ready({"facts_needed": [f]})]) for unit in units]
        choices = [(unit, missing) for unit, missing in choices if missing]
        return min(choices, key=lambda pair: len(pair[1])) if choices else (item, [])

    def ready_items(self):
        for item in self.work_items.values():
            if item["kind"] == "prepare" and item["status"] != "done" and self._facts_ready(item):
                item.update(status="done", evidence_refs=[ref for f in item["facts_needed"]
                            for ref in self.facts[f["key"]]["evidence_refs"]])
        return sorted([i for i in self.work_items.values() if i["status"] != "done"
                       and all(self.work_items[d]["status"] == "done" for d in i["depends_on"])],
                      key=lambda i: (i["kind"] != "deliver", i.get("stalled_stages", 0),
                                     len(self.preparation_target(i)[1])))

    def start(self, plan, step):
        item = self.work_items[plan["work_item_id"]]
        if item not in self.ready_items():
            raise ValueError("Stage work item is not ready or has already completed")
        self.stage_count += 1
        self.active_stage = {**deepcopy(plan), "id": f"S{self.stage_count}",
                             "started_step": step, "actions": 0, "status": "running"}
        if plan["purpose"] == "prepare":
            unit, missing = self.preparation_target(item)
            self.active_stage.update(prepares_unit=unit["id"], fact_targets=deepcopy(missing))
        item["status"] = "active"
        self.stats["stages"] += 1
        return self.active_stage

    def record_action(self, record):
        self.active_stage["actions"] += 1
        self.stats["cli_actions" if record["kind"] == "cli" else "gui_actions"] += 1
        if record["returncode"] != 0:
            error = (record.get("stderr") or record.get("stdout") or "Execution failed")[-700:]
            self._failure(error, [record["evidence_ref"]])

    def _failure(self, error, refs, **details):
        stage = self.active_stage
        matches = [f for f in self.failures if f["work_item_id"] == stage["work_item_id"]
                   and f["error"] == error]
        if matches:
            matches[-1]["count"] += 1
            matches[-1]["evidence_refs"] = refs
            matches[-1].update(details)
        else:
            self.failures.append({"stage_id": stage["id"], "work_item_id": stage["work_item_id"],
                                  "method": stage["method"], "error": error,
                                  "count": 1, "evidence_refs": refs, **details})

    def blocked_command(self, text, application=None):
        """Only an observed unsupported command is blocked; advice is never a capability fact."""
        command = re.match(r"\s*([A-Za-z]\w*)\s*\(", text)
        if not command:
            return ""
        for failure in self.failures:
            method = failure.get("observed_method", {})
            if method.get("command_name", "").casefold() != command[1].casefold():
                continue
            known = method.get("application", "").casefold()
            words = [w for w in re.findall(r"[a-z]+", (application or "").casefold())
                     if len(w) >= 3 and w not in {"classic", "presentation", "office", "desktop"}]
            if application and known and not any(w in known for w in words):
                continue
            return f"Observed unsupported command {command[1]!r}: {failure['error']}. Use another GUI method."
        return ""

    def block(self, kind, owner, subject, evidence_ref, *, attempt=None):
        """Execution owns blockers; renaming a plan or switching units cannot clear one."""
        previous = self.active_blocker
        if not previous or previous["owner"] != owner or previous["status"] == "resolved":
            self.active_blocker = {"kind": kind, "owner": owner, "subject": subject,
                "status": "blocked", "resume_stage_id": self.active_stage["id"],
                "work_item_id": self.active_stage["work_item_id"], "evidence_refs": [],
                "attempts": [], "resolution_evidence_refs": []}
        blocker = self.active_blocker
        if kind == "input_obstructed" or blocker["kind"] != "input_obstructed":
            blocker.update(kind=kind, subject=subject)
        if evidence_ref and evidence_ref not in blocker["evidence_refs"]:
            blocker["evidence_refs"].append(evidence_ref)
        blocker["evidence_refs"] = blocker["evidence_refs"][-4:]
        if attempt:
            blocker["attempts"].append({**attempt, "evidence_ref": evidence_ref})
            blocker["attempts"] = blocker["attempts"][-8:]
        return blocker

    def resolve_blocker(self, owner, evidence_ref):
        """Require a new observed resolution, never a planner's promise of recovery."""
        blocker = self.active_blocker
        if blocker and blocker["owner"] == owner and evidence_ref:
            blocker.update(status="resolved", resolution_evidence_refs=[evidence_ref])

    def apply(self, result, evidence):
        """Only the fixed active unit is writable; references must belong to real evidence."""
        stage = self.active_stage
        item = self.work_items[stage["work_item_id"]]
        current = set(evidence)
        explicit_outputs = {r.get("output_path") for r in self.requirements}
        unpublished = [a for a in result["artifacts"] if a["target_path"] != a["working_path"]
                       or Path(a["target_path"]).is_relative_to("/tmp")
                       and a["target_path"] not in explicit_outputs]
        pending_copy = any(a.get("delivery_pending") and a.get("work_item_id") == item["id"]
                           for a in self.artifacts.values())
        if (stage["purpose"] != "prepare" and result["delivery_state"] in {"saved", "verified"}
                and (unpublished and len(unpublished) == len(result["artifacts"])
                     or pending_copy and not result["artifacts"])):
            remaining = "Publish the working copy to the user's deliverable and reopen that final file."
            result.update(delivery_state="implemented", unit_met=False,
                          remaining=list(dict.fromkeys(result["remaining"] + [remaining])))
            if "unit_assessment" in result:
                result["unit_assessment"].update(status="not_met", remaining=result["remaining"])
            for artifact in unpublished:
                artifact["delivery_pending"] = True
        self.known_evidence.update(current)
        allowed = self.known_evidence
        self.last_rejections = []
        advanced = False
        for fact in result["facts"]:
            fact = deepcopy(fact)
            refs = fact["evidence_refs"]
            if not refs or not set(refs) <= allowed:
                self.last_rejections.append({"key": fact["key"], "reason": "unregistered_evidence"})
                continue
            if fact["key"] in self.source_keys and fact["scope"] != "task":
                self.last_rejections.append({"key": fact["key"], "reason": "source_key_requires_source_fact"})
                continue
            old = self.facts.get(fact["key"])
            if (fact["key"] in self.source_keys and stage["purpose"] != "prepare"
                    and (old is None or old["value"] != fact["value"])):
                self.last_rejections.append({"key": fact["key"],
                                             "reason": "source_update_requires_acquisition_stage"})
                continue
            entries = fact.get("entries", {})
            old_entries = old.get("entries", {}) if old else {}
            conflicts = any(key in old_entries and old_entries[key] != value
                            for key, value in entries.items())
            extending = bool(old_entries and entries and not conflicts)
            correction = bool(fact.get("correction_reason") and set(refs) & current)
            # Incomplete prose sources may arrive a section at a time without structured entries.
            # Append evidence, never replace the previously observed section or unlock a partial fact.
            if (old and old["status"] == fact["status"] == "confirmed"
                    and old["scope"] == fact["scope"] == "task" and not old.get("complete", True)
                    and not entries and not old_entries and set(refs) & current and not correction):
                sections = deepcopy(old.get("observations", [{k: old[k] for k in
                                    ("value", "source_ref", "evidence_refs")}]))
                if fact["value"] not in [section["value"] for section in sections]:
                    sections.append({k: fact[k] for k in ("value", "source_ref", "evidence_refs")})
                fact["observations"] = sections
                fact["value"] = "\n".join(section["value"] for section in sections)
                fact["evidence_refs"] = list(dict.fromkeys(old["evidence_refs"] + refs))
                extending = True
            if entries:
                fact["entries"] = {**old_entries, **entries}
                fact["value"] = json.dumps(fact["entries"], ensure_ascii=False)
                fact["evidence_refs"] = list(dict.fromkeys((old or {}).get("evidence_refs", []) + refs))
                fact["complete"] = bool(fact.get("complete", False) or (old or {}).get("complete", False))
            if (old and old["status"] == "confirmed" and old["scope"] == "task"
                    and (fact["status"] != "confirmed" or fact["scope"] != "task"
                         or old.get("complete", True) and not fact.get("complete", True)
                         or old["value"] != fact["value"] and not (correction or extending)
                         or conflicts and not correction)):
                self.last_rejections.append({"key": fact["key"], "reason": "confirmed_source_preserved"})
                continue
            if (fact["status"] == "confirmed" and fact["scope"] == "task"
                    and (old is None or old["value"] != fact["value"] or old["status"] != fact["status"]
                         or old.get("complete", True) != fact.get("complete", True))):
                advanced = advanced or fact["key"] in self.source_keys
                self.stats["new_facts"] += 1
            self.facts[fact["key"]] = {**fact, "requirement_ids": item["requirement_ids"],
                                      "observed_stage": stage["id"]}
        for material in result["materials"]:
            if material["evidence_refs"] and set(material["evidence_refs"]) <= allowed:
                old = self.materials.get(material["source_ref"], {})
                new_parts = set(material["covered_parts"]) - set(old.get("covered_parts", []))
                if new_parts:
                    self.stats["source_parts"] += len(new_parts)
                summary = old.get("summary", "")
                if material["summary"] not in summary:
                    summary = (summary + "\n" + material["summary"]).strip()
                self.materials[material["source_ref"]] = {
                    **material, "summary": summary,
                    "covered_parts": list(dict.fromkeys(old.get("covered_parts", [])
                                                         + material["covered_parts"])),
                    "evidence_refs": list(dict.fromkeys(old.get("evidence_refs", [])
                                                        + material["evidence_refs"]))}
        refs = result["evidence_refs"]
        grounded = bool(set(refs) & current) and set(refs) <= allowed
        level = result["delivery_state"]
        if result.get("progress_kind") in {"navigation", "source", "none"}:
            level = "none"
        delivering = item["kind"] == "deliver" and stage["purpose"] != "prepare"
        if grounded and delivering and level != "none" and result["stage_outcome"] == "met":
            for milestone in result.get("milestones", []):
                milestone_refs = milestone["evidence_refs"]
                old = item["milestones"].get(milestone["key"])
                if (set(milestone_refs) & current and set(milestone_refs) <= allowed
                        and (old is None or LEVELS[level] > LEVELS[old["delivery_state"]])):
                    item["milestones"][milestone["key"]] = {**milestone, "delivery_state": level}
                    advanced = True
                    self.stats["milestone_advances"] += 1
        # A verified section is not a verified whole document/application.
        if level == "verified" and not result["unit_met"]:
            saved = any(a["save_state"] == "saved" for a in result["artifacts"])
            level = "saved" if saved or stage["purpose"] == "save" else "implemented"
        if (grounded and delivering and LEVELS[level] > LEVELS[item["delivery_state"]]
                and (level != "verified" or result["stage_outcome"] == "met")):
            item["delivery_state"] = level
            advanced = True
            self.stats["delivery_advances"] += 1
        unit_check = result.get("unit_assessment", {})
        remaining = unit_check.get("remaining", result["remaining"]) if unit_check.get(
            "work_item_id") == item["id"] else result["remaining"]
        item.update(status="pending", remaining=remaining, evidence_refs=refs if grounded else [])
        preparation_met = item["kind"] == "prepare" and self._facts_ready(item)
        if grounded and result["stage_outcome"] == "met":
            self.stats["verified_stages"] += 1
            if preparation_met or (delivering and result["unit_met"]
                                   and level == "verified"):
                item["status"] = "done"
                advanced = True
        for check in result["requirement_checks"]:
            if check["evidence_refs"] and set(check["evidence_refs"]) <= allowed:
                req = next((r for r in self.requirements if r["id"] == check["id"]), None)
                if req is not None:
                    req.update(status=check["status"], evidence_refs=check["evidence_refs"])
        for artifact in result["artifacts"]:
            if not artifact["evidence_refs"] or not set(artifact["evidence_refs"]) <= allowed:
                continue
            old = self.artifacts.get(artifact["target_path"], {})
            if (old and artifact["working_path"] != old["working_path"]
                    and (not set(artifact["evidence_refs"]) & current
                         or artifact.get("replaces_working_path") != old["working_path"])):
                self.last_rejections.append({"key": artifact["target_path"],
                                             "reason": "working_copy_change_requires_explicit_migration"})
                continue
            self.artifacts[artifact["target_path"]] = {
                **artifact, "work_item_id": item["id"], "revision": old.get("revision", 0) + 1}
            if artifact["save_state"] == "saved" and artifact["target_path"] == artifact["working_path"]:
                for previous in self.artifacts.values():
                    if previous["working_path"] == artifact.get("replaces_working_path"):
                        previous.pop("delivery_pending", None)
        for key in result["invalidated_work_item_ids"]:
            if key in self.work_items and self.work_items[key]["kind"] == "deliver" and grounded:
                self.work_items[key].update(status="pending", delivery_state="none",
                                            milestones={},
                                            remaining=["Contradictory evidence requires reinspection"])
                for req in self.requirements:
                    if req["id"] in self.work_items[key]["requirement_ids"]:
                        req.update(status="uncertain", evidence_refs=list(current))
        for error in result.get("observed_errors", []):
            error_refs = set(error["evidence_refs"])
            if not error_refs or not error_refs <= current:
                continue
            details = {"suggested_actions": result["remaining"]}
            name = error.get("command_name", "")
            if (name and re.fullmatch(r"[A-Za-z]\w*", name)
                    and re.search(r"unknown command\s*:\s*" + re.escape(name) + r"\b",
                                  error["message"], re.I)):
                details["observed_method"] = {"application": error.get("application", ""),
                                               "command_name": name}
            self._failure(error["message"], error["evidence_refs"], **details)
        if result["stage_outcome"] != "met" and grounded:
            self._failure(result["summary"], refs, suggested_actions=result["remaining"])
        self.stats["stages_without_progress"] = 0 if advanced else self.stats["stages_without_progress"] + 1
        item["stalled_stages"] = 0 if advanced else item.get("stalled_stages", 0) + 1
        item["last_attempt"] = {k: stage[k] for k in ("route", "purpose", "method", "command", "inputs")}
        item["last_attempt"]["failure_kind"] = result.get("failure_kind", "")
        failed_cli = stage["route"] == "cli" and result.get("failure_kind") == "execution_error"
        item["failed_cli_stages"] = item["failed_cli_stages"] + 1 if failed_cli else 0
        self.revision += 1
        self.last_interaction = deepcopy(stage.get("interaction", {}))
        self.active_stage = None
        self.ready_items()
        return advanced

    def snapshot(self):
        return deepcopy({"schema_version": 2, "revision": self.revision, "instruction": self.instruction,
                         "requirements": self.requirements, "materials": list(self.materials.values()),
                         "facts": list(self.facts.values()), "work_items": list(self.work_items.values()),
                         "artifacts": list(self.artifacts.values()), "failures": self.failures,
                         "execution_constraints": self.execution_constraints,
                         "memory_update_rejections": self.last_rejections,
                         "last_interaction": self.last_interaction,
                         "active_blocker": self.active_blocker,
                         "active_stage": self.active_stage, "stats": self.stats,
                         "evidence_refs": sorted(self.known_evidence)})
