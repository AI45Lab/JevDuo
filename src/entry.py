"""worker 侧主循环：读 JSON job spec → 逐题起 VM → 跑 agent → 官方评分 → 落盘。

跑在冻结的 OSWORLD_PYTHON 里，**不 import settings**（那需要 PyYAML）。
与 agent 子进程只通过 VM 的 (ip, port) 和 result.json 交互。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

import recorder
import scoring
from appcfg import write_configs
from launch import classify, run_agent
from vm import Vm


def _load_spec(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _steps_from_disk(directory: Path) -> int:
    """异常退出时从 steps.jsonl 数步数：agent 子进程没返回，但轨迹已经写了。"""
    candidates = sorted(directory.glob("agent/**/steps.jsonl"), key=lambda path: len(path.parts))
    if not candidates:
        return 0
    try:
        with candidates[0].open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def run_task(vm: Vm, spec: dict, task: dict, job_root: Path) -> dict:
    harness = spec["harness"]
    max_steps = int(harness["max_steps"])
    max_wall = float(harness["max_wall_seconds"])
    model = spec["model"]
    task_id = str(task["id"])
    directory = recorder.episode_dir(job_root, task_id)
    recorder.start_episode(directory, task)
    started = time.perf_counter()
    info: dict = {}
    try:
        vm.reset(task)
        remaining = max_wall - (time.perf_counter() - started)
        if remaining <= 0:
            info = {"timed_out": True}
        else:
            session_id = re.sub(r"[^A-Za-z0-9._-]", "-", f"{spec['run_id']}-{task_id}")
            app_path = write_configs(
                directory / "config", model=model, harness=harness,
                vm_ip=vm.vm_ip, server_port=vm.server_port, max_steps=max_steps,
                artifact_root=directory / "agent",
            )
            info = run_agent(
                query=task["instruction"], session_id=session_id, app_config=app_path,
                log_dir=directory / "agent", result_path=directory / "agent_result.json",
                stdout_path=directory / "agent_stdout.log", max_steps=max_steps,
                timeout_seconds=remaining, api_key=model["api_key"],
                direct_api=bool(model.get("direct_api", False)),
            )
        action, stop_reason = classify(info)
        if action == "error":
            raise RuntimeError(f"agent 运行失败：{stop_reason}（详见 agent_stdout.log）")
        if action:  # DONE/FAIL 必须送达 VM：infeasible 题依赖最后一个动作是 FAIL
            vm.execute(action, pause=1)
        recorder.write_trajectory(
            directory, directory / "agent",
            llm_calls=bool(spec["log"].get("llm_calls", True)),
        )
        raw = vm.score(task)
        score = scoring.official_score(raw) if raw is not None else None
        recorder.write_evaluation(directory, raw, score)
        return recorder.finalize(
            directory, task_id=task_id, stop_reason=stop_reason, score=score,
            status=scoring.episode_status(scored=score is not None, exception=None),
            steps=int(info.get("step_count") or 0), model=model["name"],
            seconds=time.perf_counter() - started,
            failure_reason=recorder.evaluation_failure_reason(raw),
        )
    except BaseException as error:
        return recorder.finalize(
            directory, task_id=task_id, stop_reason="exception", score=None,
            status=scoring.episode_status(scored=False, exception=error),
            steps=int(info.get("step_count") or _steps_from_disk(directory)),
            model=model["name"],
            exception=error, seconds=time.perf_counter() - started,
        )


def run_job(spec: dict) -> dict:
    job_root = Path(spec["output_root"])
    job_root.mkdir(parents=True, exist_ok=True)
    benchmark = spec["benchmark"]
    vm = Vm(task_root=benchmark["task_root"], vm_path=benchmark["vm"],
            run_root=job_root, asset_root=benchmark.get("asset_root") or None,
            startup_wait_seconds=float(benchmark.get("startup_wait_seconds", 30)))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
