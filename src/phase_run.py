"""Honor native task phases without resetting the VM or multiplying task budgets."""

from __future__ import annotations

import copy
import json
import math
import time
from pathlib import Path

import evaluation_runtime
import recorder
from actor_run import run_actor


def run_phases(
    vm, spec: dict, task: dict, directory: Path, started: float, phases: list,
    *, evaluation_offset: int | None = None,
) -> tuple[dict, dict]:
    harness, model = spec["harness"], spec["model"]
    job_root = directory.parent.parent
    if evaluation_offset is None:
        evaluation_offset = evaluation_runtime.log_offset(job_root)
    remaining_cycles = int(harness["max_steps"])
    usage = {"model_calls": 0, "tool_calls": 0, "cleanup_tool_calls": 0}
    results, info = [], {}
    stop_reason = None
    for index, phase in enumerate(phases, 1):
        if remaining_cycles <= 0:
            stop_reason = "phase_cycle_budget_exhausted"
            break
        remaining = float(harness["max_wall_seconds"]) - (time.perf_counter() - started)
        if remaining <= float(harness.get("evaluation_reserve_seconds", 0)):
            stop_reason = "wall_time_limit"
            if not info:
                info = {"timed_out": True}
            break
        # reset() has already performed the native task's first-phase setup,
        # including the prepared runtime's asset/certificate provisioning.
        if index > 1:
            vm.env._step_no = 0
            vm.env.action_history.clear()
            vm.env._traj_no += 1
            phase["setup"](
                vm.env.setup_controller,
                use_proxy=bool(task.get("proxy") and getattr(vm.env, "enable_proxy", False)),
            )
            vm.env.is_environment_used = True
            time.sleep(float(phase.get("pause_after_setup_seconds", 5)))
        vm.env.instruction = phase["instruction"]
        local = directory / "phases" / f"phase-{index:02d}"
        local.mkdir(parents=True, exist_ok=True)
        (local / "task.json").write_text(
            json.dumps({"id": task["id"], "phase_index": index, "instruction": phase["instruction"]})
        )
        budget = copy.deepcopy(harness)
        budget["max_steps"] = remaining_cycles
        budget["orchestration"]["max_cycles"] = remaining_cycles
        for kind in ("model_calls", "tool_calls"):
            budget["orchestration"][f"max_{kind}"] -= usage[kind]
        if budget["orchestration"]["max_model_calls"] <= 0:
            stop_reason = "global_model_calls_limit"
            break
        log_dir = directory / "agent" / f"phase-{index:02d}"
        info, actor_stop = run_actor(
            vm, model=model, harness=budget, query=phase["instruction"],
            session_id=f"{spec['run_id']}-{task['id']}-phase-{index}",
            directory=local, log_dir=log_dir, started=started,
        )
        step_log = recorder.find_log(
            log_dir, preferred=Path(info["step_log_path"]) if info.get("step_log_path") else None,
        )
        steps = int(info.get("step_count") or sum(1 for _ in recorder.read_jsonl(step_log)))
        if not 0 <= steps <= remaining_cycles:
            raise RuntimeError("Phase actor exceeded the shared cycle budget")
        remaining_cycles -= steps
        phase_usage = info.get("usage")
        if phase_usage is None:
            journal = recorder.find_log(step_log.parent if step_log else log_dir, "orchestration.jsonl")
            for record in recorder.read_jsonl(journal, required=True):
                phase_usage = record.get("state", {}).get("usage", phase_usage)
        if phase_usage is None or any(kind not in phase_usage for kind in usage):
            raise RuntimeError("Missing phase actor usage; cannot safely share the task budget")
        for kind in usage:
            usage[kind] += int(phase_usage.get(kind) or 0)
        score = float(phase["evaluate"](vm.env))
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise RuntimeError("Official phase evaluator returned an invalid score")
        evaluation_runtime.check_failures(job_root, since=evaluation_offset)
        result = {
            "phase_index": index,
            "name": phase.get("name", f"Phase {index}"),
            "score": score,
            "cycles": steps,
            "actor_stop_reason": actor_stop,
            "usage": phase_usage,
            "remaining_cycles": remaining_cycles,
        }
        (local / "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        results.append(result)
        (directory / "phase_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        if (phase.get("gate_min_score") is not None and score < float(phase["gate_min_score"])) or (
            phase.get("gate") and score <= 0
        ):
            stop_reason = "phase_gate"
            break
    if not results:
        raise RuntimeError("No official phase evaluation completed; task remains unscored")
    aggregate = dict(info)
    aggregate.update(
        step_count=int(harness["max_steps"]) - remaining_cycles, usage=usage, phases_completed=len(results)
    )
    aggregate["phase_stop_reason"] = stop_reason
    (directory / "agent_result.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return aggregate, {
        "score": round(max(0.0, min(1.0, sum(r["score"] for r in results))), 4),
        "phases": results,
        "phase_stop_reason": stop_reason,
    }
