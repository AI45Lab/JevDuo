"""开发机侧任务池：切片 → 并发提交 rjob → 轮询 → 收日志 → 合并 → 收尾。

并发只在 rjob 级：每 job 一个 rjob（默认 4C/16G），job 内逐题串行。
产物全部落 runs/<run_id>/，不做部署副本，也不 rsync 源码。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import recorder
import rjob
import settings

ROOT = Path(__file__).resolve().parents[1]


def submit_argv(config: dict, *, run_id: str, job_id: int, spec_path: Path) -> list[str]:
    cluster = config["cluster"]
    name = rjob.job_name(run_id, job_id)
    argv = [
        f"--name={name}",
        f"--charged-group={cluster.get('charged_group', 'evobox_proxy')}",
    ]
    if cluster.get("private_machine", True):
        argv.append("--private-machine=group")
    argv += [
        f"--cpu={int(cluster.get('cpu', 4))}",
        f"--memory={int(cluster.get('memory', 16384))}",
        f"--gpu={int(cluster.get('gpu', 0))}",
        f"--image={cluster['image']}",
        "-r", "never",
        "--auto-restart", "false",
        "--privileged", "true",
    ]
    for mount in cluster.get("mounts", []):
        argv.append(f"--mount={mount['source']}:{mount['target']}")
    argv += ["--", "bash", str(ROOT / "infra" / "worker.sh"), str(spec_path)]
    return argv


def collect(client: rjob.Client, name: str, job_dir: Path) -> dict:
    """把平台状态与日志补写进 job 目录；失败不致命。"""
    job_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    try:
        (job_dir / "status.txt").write_text(client.get(name), encoding="utf-8")
    except rjob.RjobError as error:
        result["status_error"] = str(error)[:500]
    try:
        (job_dir / "tail.log").write_text(client.logs(name), encoding="utf-8")
    except rjob.RjobError as error:
        result["logs_error"] = str(error)[:500]
    return result


def run_one(config: dict, *, run_id: str, job_id: int, tasks: list[str], api_name: str,
            api: dict, run_root: Path, keep: bool, client: rjob.Client) -> dict:
    spec = settings.render_spec(
        config, run_id=run_id, job_id=job_id, tasks=tasks,
        api_name=api_name, api=api, run_root=run_root)
    spec_path = settings.write_spec(spec, run_root / "job_specs")
    name = rjob.job_name(run_id, job_id)
    job_dir = run_root / "jobs" / f"job-{job_id:02d}"
    record = {"job_id": job_id, "rjob": name, "tasks": tasks,
              "cpu": config["cluster"].get("cpu"), "memory": config["cluster"].get("memory"),
              "started_at": time.time()}
    try:
        try:
            client.delete(name)
        except rjob.RjobError:
            pass
        client.submit(submit_argv(config, run_id=run_id, job_id=job_id, spec_path=spec_path))
        status = rjob.watch(client, name, poll_seconds=float(config["cluster"].get("poll_seconds", 60)),
                            timeout_seconds=float(config["harness"]["max_wall_seconds"])
                            * max(1, len(tasks)) + 1800)
        record["status"] = status
    except TimeoutError as error:
        record["status"], record["error"] = "timeout", str(error)
    except rjob.RjobError as error:
        record["status"], record["error"] = "error", str(error)[:2000]
    finally:
        record.update(collect(client, name, job_dir))
        record["finished_at"] = time.time()
        record["cleanup"] = rjob.cleanup(client, name, keep=keep or bool(config["cluster"].get("keep_job")))
        (job_dir / "job.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = run_root / "jobs" / f"job-{job_id:02d}" / "summary.json"
    if summary_path.is_file():
        record["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pool")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tasks", default="")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--shard", default=None)
    parser.add_argument("--jobs", type=int, default=None, help="覆盖 pool.max_parallel")
    parser.add_argument("--per-job", type=int, default=None, help="覆盖 pool.tasks_per_job")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=None, help="覆盖 harness.max_steps")
    parser.add_argument("--max-wall", type=float, default=None, help="单题墙钟上限（秒）")
    parser.add_argument("--keep", type=int, default=0)
    args = parser.parse_args(argv)

    config = settings.load(args.config)
    # 命令行覆盖只作用于本次 run 的内存副本，不改配置文件
    if args.max_steps:
        config["harness"]["max_steps"] = int(args.max_steps)
    if args.max_wall:
        config["harness"]["max_wall_seconds"] = float(args.max_wall)
    run_root = ROOT / config["project"]["run_root"] / args.run_id
    api_name, api = settings.select_model(config, args.model)
    tasks = settings.resolve_tasks(config, tasks=args.tasks, limit=args.limit, shard=args.shard)
    per_job = args.per_job or int(config["pool"]["tasks_per_job"])
    max_parallel = args.jobs or int(config["pool"]["max_parallel"])
    chunks = settings.chunk(tasks, per_job)

    recorder.write_run(run_root, {
        "run_id": args.run_id, "benchmark": config["benchmark"], "harness": config["harness"],
    }, git_describe=_git_describe(), model_name=api_name,
        config_hash=settings.describe(args.config))
    print(f"run={args.run_id} model={api_name} tasks={len(tasks)} jobs={len(chunks)} "
          f"parallel={max_parallel} per_job={per_job}", flush=True)

    client = rjob.Client()
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(chunks) or 1))) as pool:
        futures = {
            pool.submit(run_one, config, run_id=args.run_id, job_id=index, tasks=chunk,
                        api_name=api_name, api=api, run_root=run_root,
                        keep=bool(args.keep), client=client): index
            for index, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            job_id = futures[future]
            try:
                records.append(future.result())
            except BaseException as error:  # 单个 job 异常不终止整轮
                records.append({"job_id": job_id, "status": "error", "error": str(error)[:2000]})
            print(f"[job-{job_id:02d}] {records[-1].get('status')}", flush=True)

    summaries = [record["summary"] for record in records if record.get("summary")]
    merged = recorder.merge_summaries(run_root, summaries)
    failed_jobs = [record for record in records if record.get("status") != "succeeded"]
    print(json.dumps({"total": merged["total"], "scored": merged["scored"],
                      "failed_unscored": merged["failed_unscored"],
                      "score_mean": merged["score_mean"],
                      "failed_jobs": [record.get("job_id") for record in failed_jobs],
                      "run_root": str(run_root)}, ensure_ascii=False), flush=True)
    # job 没跑起来（镜像/环境失败）和题没跑成都要非零退出，避免误判为成功
    return 0 if merged["failed_unscored"] == 0 and not failed_jobs else 1


def _git_describe() -> str:
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
