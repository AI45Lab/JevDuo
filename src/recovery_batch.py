"""Run an explicit repair cohort from immutable code; stop only its failed jobs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pool
import recorder
import rjob
import settings

ROOT = Path(__file__).resolve().parents[1]
TASKS = {"014", "031", "045", "057", "059", "061", "071", "074", "080", "092"}
TASKS.update({"005", "018", "024", "054", "055", "065", "069", "079", "081", "094", "101", "105", "107"})


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def agent_failure(agent: dict) -> str | None:
    decision = agent.get("terminal_decision") or {}
    if agent.get("error"):
        return str(agent["error"])
    if agent.get("terminal_status") == "error":
        return decision.get("final_answer") or "agent_error"
    # Older immutable attempts conflated the model's per-call TimeoutError
    # with the enclosing run deadline. Preserve and reject that known failure.
    if decision.get("reason") == "budget_exhausted" and re.fullmatch(
        r"LLM call timed out after [0-9.]+ seconds\.", decision.get("final_answer", ""),
    ):
        return decision["final_answer"]
    return None


def snapshot(destination: Path) -> None:
    destination.mkdir()
    for name in ("src", "infra"):
        shutil.copytree(ROOT / name, destination / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("AGENTS.md", "config.yaml", "pyproject.toml"):
        shutil.copy2(ROOT / name, destination / name)
    (destination / "venv").symlink_to(ROOT / "venv", target_is_directory=True)


def execute(config: dict, run_id: str, run_root: Path, job_id: int, task: str,
            *, repo_root: Path | None = None) -> dict:
    api_name, api = settings.select_model(config, None)
    job = run_root / "jobs" / f"job-{job_id:02d}"
    job.mkdir(parents=True)
    spec = settings.render_spec(config, run_id=run_id, job_id=job_id, tasks=[task],
                                api_name=api_name, api=api, run_root=run_root)
    spec_path = settings.write_spec(spec, run_root / "job_specs")
    client = rjob.Client()
    record = {"job_id": job_id, "task_id": task, "status": "submitting", "started_at": time.time()}
    name = None
    try:
        with (job / "submission-attempt.json").open("x") as handle:
            json.dump({"at": time.time(), "auto_resubmit": False}, handle)
        output = client.submit(pool.submit_argv(
            config, run_id=run_id, job_id=job_id, spec_path=spec_path, repo_root=repo_root,
        ))
        (job / "submit.log").write_text(output)
        match = re.search(r"created rjob_name:\s*(\S+)", output)
        if not match:
            raise RuntimeError("Submission name unknown; inspect submit.log; never replay submission")
        name = match.group(1)
        record.update(rjob=name, status="submitted", submitted_at=time.time())
        write(job / "job.json", record)
        print(json.dumps({"event": "submitted", **record}), flush=True)
        deadline = time.monotonic() + 10800 + 1800  # Task alarm separately includes setup and scoring.
        while True:
            if time.monotonic() >= deadline:
                client.stop(name)
                record.update(status="watch_timeout", stopped_on_failure=True)
                break
            episode = read(job / "episodes" / task / "episode.json")
            agent = read(job / "episodes" / task / "agent_result.json")
            failure = (
                episode.get("exception", {}).get("message", "failed_unscored")
                if episode.get("status") == "failed_unscored" else
                agent_failure(agent)
            )
            if failure:
                record.update(status="failed_unscored", failure=str(failure)[:2000])
                write(job / "controller_failure.json", {"task_id": task, "at": time.time(),
                                                        "reason": record["failure"]})
                try:
                    client.stop(name)
                    record["stopped_on_failure"] = True
                except rjob.RjobError:
                    record["stopped_on_failure"] = False
                break
            try:
                output = client.get(name)
            except rjob.RjobError as error:
                # Status reads have no effects. Keep checking local failure
                # artifacts and the watch deadline instead of killing a task
                # merely because the control plane briefly cannot be queried.
                with (job / "monitor-errors.jsonl").open("a") as handle:
                    handle.write(json.dumps({"at": time.time(), "operation": "get",
                                             "error_type": type(error).__name__,
                                             "error_tail": str(error)[-1500:]}) + "\n")
                time.sleep(15)
                continue
            (job / "status.txt").write_text(output)
            status = rjob.parse_status(output)
            if re.search(r"(?i)\b(?:rjob|replica)\s+\S+:\s*stopped\b", output):
                status = "stopped"
            if status != record["status"]:
                record.update(status=status, checked_at=time.time())
                write(job / "job.json", record)
                print(json.dumps({"event": "status", "task_id": task, "status": status}), flush=True)
            if status in rjob.TERMINAL_STATES or status == "stopped":
                episode = read(job / "episodes" / task / "episode.json")
                record["episode_status"] = episode.get("status", "missing_episode")
                record["score"] = episode.get("score")
                break
            time.sleep(15)
    except Exception as error:
        record.update(status="submission_unknown" if name is None else "controller_error",
                      error_type=type(error).__name__, error=str(error)[:1000])
        if name is not None:
            try:
                client.stop(name)
                record["stopped_on_failure"] = True
            except rjob.RjobError:
                pass
    finally:
        if name is not None:
            record.update(pool.collect(client, name, job))
        record["finished_at"] = time.time()
        write(job / "job.json", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--parallel", type=int, default=10)
    args = parser.parse_args()
    tasks = args.tasks.split(",")
    if len(tasks) != len(set(tasks)) or not set(tasks) <= TASKS:
        raise ValueError("Only explicitly authorized recovery tasks may be submitted")
    config = settings.load(ROOT / "config.yaml")
    orchestration = config["harness"]["orchestration"]
    assert config["harness"]["max_steps"] == orchestration["max_cycles"] == 20
    assert orchestration["subagent_steps_per_cycle"] == 50
    # Bounded JSON/provider retries plus terminal verification must never
    # consume a future child's fifty slots.
    slow_reserve = ((orchestration["max_cycles"] + 1) * 3
                    * (max(orchestration.get("empty_response_retries", 2),
                           orchestration.get("model_timeout_retries", 0)) + 1))
    assert orchestration["max_model_calls"] >= 20 * 50 + slow_reserve
    assert orchestration["max_tool_calls"] >= 20 * 50
    assert config["harness"]["max_wall_seconds"] == 10800
    run = ROOT / "runs" / args.run_id
    run.mkdir(exist_ok=True)
    with (run / "driver-started.json").open("x") as handle:
        json.dump({"pid": os.getpid(), "at": time.time(), "tasks": tasks}, handle)
    code = run / "source-snapshot"
    snapshot(code)
    write(run / "manifest.json", {"tasks": tasks, "source": str(code), "wall_seconds": 10800,
                                   "max_steps": 20, "subagent_steps_per_cycle": 50, "auto_resubmit": False,
                                   "evaluation_model": "k3.1-pro", "original_batch_untouched": True})
    records = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.parallel, len(tasks)))) as executor:
        futures = [executor.submit(execute, config, args.run_id, run, i, task, repo_root=code)
                   for i, task in enumerate(tasks)]
        for future in as_completed(futures):
            records.append(future.result())
            summary = recorder.merge_episodes(run, recorder.collect_episodes(run))
            write(run / "batch-status.json", {"finished_jobs": len(records), "total_jobs": len(tasks),
                                               "summary": summary, "jobs": records})
            print(json.dumps({"event": "job_finished", "job": records[-1]}, ensure_ascii=False), flush=True)
    summary = recorder.merge_episodes(run, recorder.collect_episodes(run))
    write(run / "completion.json", {"summary": summary, "jobs": records, "at": time.time()})
    return 0 if summary["scored"] == len(tasks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
