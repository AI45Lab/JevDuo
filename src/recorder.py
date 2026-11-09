"""结果目录契约：所有落盘都在 run 目录下，纯 dict，不 import 任何 harness 模块。

run 级：
  run.json / summary.json / summary.csv / job_specs/ / jobs/ / worker/
episode 级（jobs/job-NN/episodes/<task>/）：
  task.json / episode.json / evaluation.json / traj.jsonl / llm_calls.jsonl /
  screenshots/ / agent/ / agent_stdout.log / agent_result.json
"""

from __future__ import annotations

import json
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

_SECRET = re.compile(r"(sk-[A-Za-z0-9._-]{6,}|Bearer\s+)[^\s\"']*")


def _redact(text: str, limit: int = 500) -> str:
    return _SECRET.sub(r"\1[REDACTED]", text)[:limit]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_run(run_root: Path, spec: dict, *, git_describe: str, model_name: str, config_hash: str) -> None:
    _write_json(run_root / "run.json", {
        "run_id": spec["run_id"],
        "benchmark": spec["benchmark"]["name"],
        "model": model_name,
        "git_describe": git_describe,
        "config_hash": config_hash,
        "harness": spec["harness"],
        "created_at": time.time(),
        "job_ids": sorted({int(spec["job_id"])}) if "job_id" in spec else [],
    })


def episode_dir(job_root: Path, task_id: str) -> Path:
    return job_root / "episodes" / task_id


def start_episode(directory: Path, task: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "task.json", task)


def write_evaluation(directory: Path, raw: Any, score: float | None) -> None:
    _write_json(directory / "evaluation.json", {"raw": raw, "score": score})


def write_trajectory(directory: Path, agent_log_dir: Path, *, llm_calls: bool = True) -> int:
    """把 agent 的 steps.jsonl 归一化成 traj.jsonl，并搬 screenshots/。返回截图数。

    上游把日志写在 `<log_dir>/<session_id>/` 这一层，不保证就在 log_dir 根下；
    找最浅的那份 steps.jsonl。steps.jsonl 单题可达 100MB，只保留可读摘要，
    原始文件留在 agent/ 里不重复复制。
    """
    candidates = sorted(agent_log_dir.rglob("steps.jsonl"),
                        key=lambda path: (len(path.relative_to(agent_log_dir).parts), str(path)))
    steps = candidates[0] if candidates else None
    target = directory / "traj.jsonl"
    written = 0
    with target.open("w", encoding="utf-8") as out:
        if steps is not None:
            for line in steps.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.write(json.dumps(_compact_step(record), ensure_ascii=False) + "\n")
                written += 1

    shots = sorted(agent_log_dir.rglob("action_step*_tool_*.png"))
    if shots:
        folder = directory / "screenshots"
        folder.mkdir(exist_ok=True)
        for index, path in enumerate(shots):
            shutil.copyfile(path, folder / f"step_{index:02d}.png")

    if llm_calls and steps is not None:
        trace = steps.with_name("llm_trace.jsonl")
        if trace.is_file():
            # 单题 trace 可达上百 MB：用相对软链指向 agent/ 里的原件，不重复占空间。
            link = directory / "llm_calls.jsonl"
            link.unlink(missing_ok=True)
            try:
                link.symlink_to(trace.relative_to(directory))
            except OSError:
                shutil.copyfile(trace, link)
    return len(shots)


def _compact_step(record: dict, limit: int = 2000) -> dict:
    """步骤摘要：动作、回执、状态、截图引用；不含完整 prompt 与图片。"""
    calls = [
        {"name": call.get("name"), "arguments": call.get("arguments")}
        for call in record.get("assistant_tool_calls") or []
    ]
    results = [
        {"name": item.get("name"), "status": item.get("status"),
         "content": str(item.get("content") or "")[:limit]}
        for item in record.get("tool_results") or []
    ]
    return {
        "step": record.get("session_step"),
        "status": record.get("status"),
        "window": record.get("current_window"),
        "assistant_text": str(record.get("assistant_text") or "")[:limit],
        "tool_calls": calls,
        "tool_results": results,
        "screenshot": record.get("clean_screenshot_path"),
        "terminal_decision": record.get("terminal_decision"),
    }


def _usage_counts(usage: dict) -> dict:
    # NexAU's input_tokens excludes cache reads/writes; input_tokens_uncached is its alias, not extra input.
    cached = usage.get("cache_read_tokens", 0)
    created = usage.get("cache_creation_tokens", 0)
    return {"prompt_tokens": usage.get("input_tokens", 0) + cached + created,
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": cached, "reasoning_tokens": usage.get("reasoning_tokens", 0)}


def write_execution_metrics(directory: Path, agent_log_dir: Path, info: dict) -> None:
    """Summarize existing model traces and the plugin ledger without inventing missing usage."""
    model = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
             "reasoning_tokens": 0, "calls_missing_usage": 0,
             "invocations": 0, "seconds": 0.0, "failed_invocations": 0}
    trace = directory / "llm_calls.jsonl"
    if trace.is_file():
        with trace.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                usage = record["llm_trace"].get("usage") or {}
                model["calls"] += 1
                if usage:
                    for key, value in _usage_counts(usage).items():
                        model[key] += value
                else:
                    model["calls_missing_usage"] += 1
    actions = {"gui_actions": 0, "cli_actions": 0, "jev_calls": 0, "jev_seconds": 0.0}
    phases: dict[str, float] = {}
    invocation_usage = []
    ledger = agent_log_dir / "jev_actions.jsonl"
    if ledger.is_file():
        with ledger.open(encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                route = event.get("route")
                if route == "action":
                    actions["cli_actions" if event["operation"] == "terminal" else "gui_actions"] += 1
                elif route == "gui":
                    actions["jev_calls"] += 1
                    actions["jev_seconds"] += event["ranking"]["latency_ms"] / 1000
                elif route == "slow_model_call":
                    model["invocations"] += 1
                    model["seconds"] += event["seconds"]
                    model["failed_invocations"] += int(event["failed"])
                    invocation_usage.append(event.get("usage"))
                elif route in {"receipt", "local_observation"}:
                    phase = "cli" if event.get("operation") == "terminal" else route
                    phases[phase] = phases.get(phase, 0) + event.get("seconds", 0)
    if invocation_usage:
        # Includes successful HTTP replies rejected as empty by the model runtime before trace persistence.
        model["calls"] = len(invocation_usage)
        model["calls_missing_usage"] = sum(not usage for usage in invocation_usage)
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens"):
            model[key] = sum(_usage_counts(usage)[key] for usage in invocation_usage if usage)
    _write_json(directory / "timing.json", {
        "slow_model": model, "local_execution": actions, "phases_seconds": phases,
        "actor_seconds": info.get("seconds"), "logical_steps": info.get("logical_steps"),
        "physical_actions": actions["gui_actions"] + actions["cli_actions"] if ledger.is_file() else None,
        "counting": "JEV mode: every GUI primitive and independent VM CLI command consumes one action",
    })


def evaluation_failure_reason(raw: Any) -> str:
    """评分器自己给出的失败原因（如 replay_failed / melt_not_found）。

    任务 055 的回放校验依赖 VM 里的 melt（MLT 渲染器），缺失时评分器直接判 0，
    这不是 agent 没做对。把它记进结果，避免与真实失败混为一谈。
    """
    if isinstance(raw, dict):
        reason = raw.get("failure_reason")
        if reason:
            return str(reason)
        replay = raw.get("replay_validation")
        if isinstance(replay, dict) and replay.get("reason"):
            return f"replay:{replay['reason']}"
    return ""


def finalize(directory: Path, *, task_id: str, stop_reason: str, score: float | None,
             status: str, steps: int, exception: BaseException | None = None,
             model: str = "", seconds: float = 0.0, failure_reason: str = "") -> dict:
    record = {
        "task_id": task_id,
        "stop_reason": stop_reason,
        "score": score,
        "status": status,
        "steps": steps,
        "model": model,
        "seconds": round(seconds, 3),
        "finished_at": time.time(),
    }
    if failure_reason:
        record["evaluation_failure_reason"] = failure_reason
    if exception is not None:
        record["exception"] = {
            "type": type(exception).__name__,
            "message": _redact(str(exception)),
            "traceback": [_redact(line, 300) for line in traceback.format_exception(
                type(exception), exception, exception.__traceback__)[-8:]],
        }
    _write_json(directory / "episode.json", record)
    return record


def write_job_summary(job_root: Path, episodes: list[dict]) -> None:
    scored = [item for item in episodes if item["status"] == "scored"]
    summary = {
        "job_id": job_root.name,
        "total": len(episodes),
        "scored": len(scored),
        "failed_unscored": len(episodes) - len(scored),
        "score_mean": (round(sum(item["score"] or 0.0 for item in scored) / len(scored), 4)
                       if scored else None),
        "episodes": episodes,
    }
    _write_json(job_root / "summary.json", summary)


def collect_episodes(run_root: Path) -> list[dict]:
    """从磁盘扫描已落盘的题（不依赖 entry 写的 job summary）。

    续跑与汇总都以 episode.json 为准：进程被杀、job 被删，只要题跑完过就有记录。
    同一题出现多次时取 finished_at 最新的那次。
    """
    latest: dict[str, dict] = {}
    for path in sorted((run_root / "jobs").glob("*/episodes/*/episode.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_id = str(record.get("task_id") or path.parent.name)
        record.setdefault("task_id", task_id)
        previous = latest.get(task_id)
        if previous is None or (record.get("finished_at") or 0) >= (previous.get("finished_at") or 0):
            latest[task_id] = record
    return sorted(latest.values(), key=lambda item: item["task_id"])


def merge_episodes(run_root: Path, episodes: list[dict]) -> dict:
    """把 episode 记录合并成 run 级 summary.json / summary.csv。"""
    episodes = sorted(episodes, key=lambda item: item["task_id"])
    scored = [item for item in episodes if item["status"] == "scored"]
    merged = {
        "total": len(episodes),
        "scored": len(scored),
        "failed_unscored": len(episodes) - len(scored),
        "score_mean": (round(sum(item["score"] or 0.0 for item in scored) / len(scored), 4)
                       if scored else None),
        "episodes": episodes,
    }
    _write_json(run_root / "summary.json", merged)
    lines = ["task_id,status,score,steps,seconds,stop_reason,model,failure_reason"]
    for item in episodes:
        score = item.get("score")
        lines.append(",".join(str(value) for value in (
            # 0.0 是合法分数，不能当成空值
            item["task_id"], item["status"], "" if score is None else score,
            item.get("steps", ""), item.get("seconds", ""),
            item.get("stop_reason", ""), item.get("model", ""),
            item.get("evaluation_failure_reason", ""))))
    (run_root / "summary.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return merged


def merge_summaries(run_root: Path, job_summaries: list[dict]) -> dict:
    episodes: list[dict] = []
    for summary in job_summaries:
        episodes.extend(summary.get("episodes", []))
    return merge_episodes(run_root, episodes)
