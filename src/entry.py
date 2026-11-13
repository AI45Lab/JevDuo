"""worker 侧主循环：读 JSON job spec → 逐题起 VM → 跑 agent → 官方评分 → 落盘。

跑在冻结的 OSWORLD_PYTHON 里，**不 import settings**（那需要 PyYAML）。
与 agent 子进程只通过 VM 的 (ip, port) 和 result.json 交互。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path

import evaluation_runtime
import recorder
import scoring
import worker_limits
from actor_run import ActorError, run_actor
from launch import classify
from phase_run import run_phases
from vm import Vm


def _load_spec(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _steps_from_disk(directory: Path) -> int:
    """Count complete persisted cycles when the subprocess cannot return them."""
    agent_root = directory / "agent"
    phases = sorted(agent_root.glob("phase-*"))
    if phases:
        return sum(
            recorder.count_steps(root, result_path=directory / "phases" / root.name / "agent_result.json")
            for root in phases
        )
    return recorder.count_steps(agent_root, result_path=directory / "agent_result.json")


def run_task(vm: Vm, spec: dict, task: dict, job_root: Path) -> dict:
    harness = spec["harness"]
    max_wall = float(harness["max_wall_seconds"])
    model = spec["model"]
    task_id = str(task["id"])
    directory = recorder.episode_dir(job_root, task_id)
    recorder.start_episode(directory, task)
    started = time.perf_counter()
    cancel_deadline = worker_limits.arm(max_wall)
    info: dict = {}
    phases: list = []
    try:
        evaluation_offset = evaluation_runtime.log_offset(job_root)
        vm.reset(task)
        phase_getter = getattr(task, "get_phases", None)
        phases = phase_getter() if callable(phase_getter) else []
        remaining = max_wall - (time.perf_counter() - started)
        if phases:
            info, raw = run_phases(
                vm, spec, task, directory, started, phases, evaluation_offset=evaluation_offset,
            )
            stop_reason = info.get("phase_stop_reason") or classify(info)[1]
        else:
            if remaining <= 0:
                info, stop_reason = {"timed_out": True}, "wall_time_limit"
            else:
                info, stop_reason = run_actor(
                    vm, model=model, harness=harness, query=task["instruction"],
                    session_id=re.sub(r"[^A-Za-z0-9._-]", "-", f"{spec['run_id']}-{task_id}"),
                    directory=directory, log_dir=directory / "agent", started=started,
                )
            raw = vm.score(task)
        evaluation_runtime.check_failures(job_root, since=evaluation_offset)
        score = scoring.official_score(raw) if raw is not None else None
        recorder.write_evaluation(directory, raw, score)
        return recorder.finalize(
            directory, task_id=task_id, stop_reason=stop_reason, score=score,
            status=scoring.episode_status(scored=score is not None, exception=None),
            steps=int(info.get("step_count") or _steps_from_disk(directory)), model=model["name"],
            seconds=time.perf_counter() - started,
            failure_reason=recorder.evaluation_failure_reason(raw),
        )
    except BaseException as error:
        if isinstance(error, ActorError) and not phases:
            info = error.info
        return recorder.finalize(
            directory, task_id=task_id, stop_reason="exception", score=None,
            status=scoring.episode_status(scored=False, exception=error),
            steps=int(info.get("step_count") or _steps_from_disk(directory)),
            model=model["name"],
            exception=error, seconds=time.perf_counter() - started,
        )
    finally:
        cancel_deadline()
        # 轨迹在 finally 里落盘：崩溃的题最需要轨迹，正是最容易丢掉的那次。
        # 这里自己吞异常，别让归一化失败盖掉上面已经算好的结果。
        try:
            recorder.write_trajectory(
                directory, directory / "agent",
                llm_calls=bool(spec["log"].get("llm_calls", True)),
                step_log_path=Path(info["step_log_path"]) if info.get("step_log_path") else None,
            )
        except Exception as error:
            print(json.dumps({"event": "trajectory_failed", "task_id": task_id,
                              "error": f"{type(error).__name__}: {error}"},
                             ensure_ascii=False), flush=True)


def run_job(spec: dict) -> dict:
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_MODEL", "TYPESAFE_BASE_URL"):
        if name in spec.get("subagent_env", {}):
            os.environ[name] = str(spec["subagent_env"][name])
    job_root = Path(spec["output_root"])
    job_root.mkdir(parents=True, exist_ok=True)
    benchmark = spec["benchmark"]
    vm = Vm(task_root=benchmark["task_root"], vm_path=benchmark["vm"],
            run_root=job_root, asset_root=benchmark.get("asset_root") or None,
            startup_wait_seconds=float(benchmark.get("startup_wait_seconds", 30)))
    evaluation_runtime.configure(spec, job_root)
    episodes: list[dict] = []
    try:
        for task_id in spec["tasks"]:
            write_heartbeat(job_root, done=len(episodes), total=len(spec["tasks"]), current=task_id)
            try:
                task = vm.fetch_task(task_id)
            except BaseException as error:
                directory = recorder.episode_dir(job_root, task_id)
                episodes.append(recorder.finalize(
                    directory, task_id=task_id, stop_reason="task_load", score=None,
                    status="failed_unscored", steps=0, model=spec["model"]["name"], exception=error))
            else:
                episodes.append(run_task(vm, spec, task, job_root))
            finally:
                recorder.write_job_summary(job_root, episodes)
                if not spec["log"].get("keep_obs_stage", False):
                    shutil.rmtree(job_root / "obs_stage" / task_id, ignore_errors=True)
    finally:
        vm.close()
    recorder.write_job_summary(job_root, episodes)
    write_heartbeat(job_root, done=len(episodes), total=len(spec["tasks"]), current="")
    return {"job_id": spec["job_id"], "done": len(episodes),
            "failed_unscored": sum(item["status"] == "failed_unscored" for item in episodes),
            "scored": sum(1 for item in episodes if item["status"] == "scored")}


def write_heartbeat(job_root: Path, *, done: int, total: int, current: str) -> None:
    (job_root / "heartbeat.json").write_text(json.dumps(
        {"done": done, "total": total, "current": current, "at": time.time()},
        ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="entry")
    parser.add_argument("--spec", required=True, help="job spec JSON 路径")
    args = parser.parse_args(argv)
    result = run_job(_load_spec(args.spec))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["failed_unscored"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
