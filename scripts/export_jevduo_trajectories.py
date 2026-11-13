#!/usr/bin/env python3
"""Export a JevDuo run or episode archive to an offline HTML browser (Python 3.10+)."""

from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def read_json(path: Path, default=None):
    if not path.is_file():
        return {} if default is None else default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def records(path: Path):
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record must be an object")
            except ValueError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
            yield number, value


def archived_path(episode: Path, raw) -> Path | None:
    """Remap historical paths within this episode; never follow an old checkout."""
    if not isinstance(raw, str) or not raw:
        return None
    marker = f"/episodes/{episode.name}/"
    if marker in raw:
        candidate = episode / raw.rsplit(marker, 1)[1]
    elif not Path(raw).is_absolute():
        candidate = episode / raw
    else:
        candidate = Path(raw)
    candidate = candidate.resolve()
    return candidate if candidate.is_relative_to(episode.resolve()) else None


class Images:
    def __init__(self, output: Path, embed: bool):
        self.output = output
        self.embed = embed
        self.items = []
        self.index = {}

    def add(self, episode: Path, raw) -> int | None:
        if not isinstance(raw, str) or not raw:
            return None
        path = archived_path(episode, raw)
        key = str(path) if path else f"missing:{episode}:{raw}"
        if key in self.index:
            return self.index[key]
        src = ""
        if path and path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            if self.embed:
                mime = mimetypes.guess_type(path.name)[0] or "image/png"
                src = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
            else:
                src = quote(os.path.relpath(path, self.output.parent), safe="/")
        index = len(self.items)
        self.index[key] = index
        self.items.append(
            {
                "src": src,
                "name": str(path.relative_to(episode.resolve())) if path else raw,
                "missing": not bool(src),
            }
        )
        return index


def phase_of(path: Path, episode: Path):
    for part in path.relative_to(episode).parts:
        if part.startswith("phase-") and part[6:].isdigit():
            return int(part[6:])
    return None


def cycle_id(record: dict):
    return (record.get("orchestration") or {}).get("cycle_id")


def load_cycles(episode: Path, warnings: list) -> list[dict]:
    """traj.jsonl defines episode order and phase; full steps enrich by exact cycle ID."""
    full = []
    by_cycle = defaultdict(list)
    for path in sorted((episode / "agent").glob("**/steps.jsonl")):
        for line, record in records(path):
            item = dict(record)
            item["_source"] = f"{path.relative_to(episode)}:{line}"
            item["_phase"] = record.get("phase_index", phase_of(path, episode))
            full.append(item)
            if cycle_id(item):
                by_cycle[cycle_id(item)].append(item)
    trajectory = episode / "traj.jsonl"
    if not trajectory.is_file():
        if not full:
            warnings.append("缺少 traj.jsonl 和 agent/**/steps.jsonl；仅显示元数据。")
        else:
            warnings.append("缺少 traj.jsonl；按 agent 路径及文件记录顺序显示完整 steps。")
        return full
    cycles = []
    used = set()
    for line, record in records(trajectory):
        if isinstance(record.get("step"), dict):
            record = record["step"]
        matches = by_cycle.get(cycle_id(record), [])
        if len(matches) == 1:
            item = dict(matches[0])
            used.add(item["_source"])
            # Canonical fields from traj may contain phase/global numbering absent in full steps.
            item.update(record)
        else:
            item = dict(record)
            if len(matches) > 1:
                warnings.append(f"traj:{line} cycle_id 重复，未猜测完整 steps 的关联。")
        item["_source"] = f"traj.jsonl:{line}" + (f" + {matches[0]['_source']}" if len(matches) == 1 else "")
        item["_phase"] = record.get("phase_index", item.get("_phase"))
        cycles.append(item)
    extra = len(full) - len(used)
    if extra:
        warnings.append(f"有 {extra} 条完整 steps 未唯一匹配 traj；以 traj 的周期顺序为准。")
    if not cycles:
        warnings.append("traj.jsonl 为空。")
    return cycles


def load_attempts(episode: Path, cycles: list[dict], warnings: list) -> list[dict]:
    """Stream traces once, dropping repeated state snapshots and retaining tool evidence."""
    paths = set((episode / "agent").glob("**/orchestration.jsonl"))
    for cycle in cycles:
        result = (cycle.get("orchestration") or {}).get("subagent_result") or {}
        path = archived_path(episode, result.get("trace_ref"))
        if path and path.is_file():
            paths.add(path)
    attempts = []

    def attempt_for(record, line, path, by_attempt, active_cycle):
        pending = (record.get("state") or {}).get("pending") or {}
        aid = record.get("attempt_id") or pending.get("attempt_id")
        # No ID means no cross-record association can be established.
        key = aid or f"unidentified-line-{line}"
        if key not in by_attempt:
            item = {
                "id": aid,
                "cycle_id": record.get("cycle_id") or active_cycle,
                "phase": phase_of(path, episode),
                "contract": pending.get("contract"),
                "observation": pending.get("observation"),
                "actions": [],
                "source": str(path.relative_to(episode)),
                "warnings": [],
            }
            by_attempt[key] = item
            attempts.append(item)
        return key, by_attempt[key]

    for path in sorted(paths):
        by_attempt = {}
        pending_tools = defaultdict(lambda: {"starts": [], "open": 0, "ambiguous": False})
        active_cycle = None

        for line, record in records(path):
            event = record.get("event")
            if event == "dispatch_started":
                active_cycle = record.get("cycle_id")
                attempt_for(record, line, path, by_attempt, active_cycle)
            elif event in {"dispatch_finished", "cycle_persisted"}:
                active_cycle = None
            elif event in {"tool_started", "tool_finished", "tool_failed"}:
                key, attempt = attempt_for(record, line, path, by_attempt, active_cycle)
                name = record.get("tool_name", "unknown")
                pending = pending_tools[(key, name)]
                if event == "tool_started":
                    action = {
                        "name": name,
                        "arguments": record.get("arguments"),
                        "result": None,
                        "status": "start_without_matched_result",
                        "source": f"{path.relative_to(episode)}:{line}",
                    }
                    attempt["actions"].append(action)
                    pending["starts"].append(action)
                    pending["open"] += 1
                    if pending["open"] > 1:
                        pending["ambiguous"] = True
                else:
                    matched = pending["open"] == 1 and not pending["ambiguous"]
                    if matched:
                        action = pending["starts"][0]
                    else:
                        action = {
                            "name": name,
                            "arguments": None,
                            "source": f"{path.relative_to(episode)}:{line}",
                        }
                        attempt["actions"].append(action)
                        if pending["open"]:
                            warning = "同名工具开始事件重叠，结束记录未猜测配对；保留独立事件。"
                            if warning not in attempt["warnings"]:
                                attempt["warnings"].append(warning)
                    action["result"] = (
                        record.get("result")
                        if event == "tool_finished"
                        else {"trace_event": "tool_failed", "error_type": record.get("error_type")}
                    )
                    action["failure"] = event == "tool_failed"
                    action["elapsed_seconds"] = record.get("elapsed_seconds")
                    action["status"] = "finished" if matched else "result_without_matched_start"
                    if matched and event == "tool_failed":
                        action["status"] = "failed"
                    action["result_source"] = f"{path.relative_to(episode)}:{line}"
                    if pending["open"]:
                        pending["open"] -= 1
                    # A completed ambiguous group must not prevent later serial calls
                    # from pairing. This clears bookkeeping, never assigns its results.
                    if not pending["open"]:
                        pending["starts"].clear()
                        pending["ambiguous"] = False
    if not paths and any((c.get("orchestration") or {}).get("subagent_result") for c in cycles):
        warnings.append("缺少 orchestration.jsonl；子代理结果仍显示，工具明细不可用。")
    return attempts


def latest_observation(messages):
    """Read explicit observation metadata, never infer an image from time or filename."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        texts = (
            [content]
            if isinstance(content, str)
            else [
                block.get("text")
                for block in content or []
                if isinstance(block, dict) and block.get("type") == "text"
            ]
        )
        for text in texts:
            if not isinstance(text, str):
                continue
            value = decoded(text)
            observation = value.get("observation") if isinstance(value, dict) else None
            if isinstance(observation, dict) and isinstance(observation.get("screenshot_path"), str):
                return observation
    return None


def load_model_observations(episode: Path, cycles: list[dict], attempts: list[dict], warnings: list):
    """Extract only the latest observation per General inference, not repeated image history."""
    by_attempt = defaultdict(list)
    for attempt in attempts:
        if attempt["id"]:
            by_attempt[attempt["id"]].append(attempt)
        attempt["observations"] = []
    by_result = defaultdict(list)
    for cycle in cycles:
        result = (cycle.get("orchestration") or {}).get("subagent_result") or {}
        if result.get("attempt_id"):
            by_result[result["attempt_id"]].append(cycle)
    paths = sorted((episode / "agent").glob("**/llm_trace.jsonl"))
    if not paths and (episode / "llm_calls.jsonl").is_file():
        paths = [episode / "llm_calls.jsonl"]
    if not paths and any((a.get("contract") or {}).get("agent_id") == "general" for a in attempts):
        warnings.append("缺少 llm_trace.jsonl；未提取 General 周期内的模型观察截图。")
    missing_ids = set()
    for path in paths:
        print(
            f"    reading model observations: {path.relative_to(episode)} "
            f"({path.stat().st_size / 1e6:.1f} MB)",
            flush=True,
        )
        for line, record in records(path):
            parts = str(record.get("agent_path", "")).split("/")
            if len(parts) != 3 or parts[:2] != ["subagents", "general"] or not parts[2]:
                continue
            aid = parts[2]
            trace = record.get("llm_trace") or {}
            observation = latest_observation((trace.get("request") or {}).get("messages") or [])
            if observation is None:
                continue
            matches = by_attempt.get(aid, [])
            if not matches and len(by_result.get(aid, [])) == 1:
                # An explicit attempt ID in both sources still works without a journal.
                cycle = by_result[aid][0]
                attempt = {
                    "id": aid,
                    "cycle_id": cycle_id(cycle),
                    "phase": cycle.get("_phase"),
                    "contract": ((cycle.get("orchestration") or {}).get("decision") or {}).get("contract"),
                    "observation": None,
                    "observations": [],
                    "actions": [],
                    "source": str(path.relative_to(episode)),
                    "warnings": ["缺少该尝试的工具事件；以下为模型观察和提议，不能证明工具已执行。"],
                }
                attempts.append(attempt)
                by_attempt[aid].append(attempt)
                matches = [attempt]
            if len(matches) != 1:
                missing_ids.add(aid)
                continue
            attempt = matches[0]
            attempt["observations"].append(
                {
                    "observation": observation,
                    "model_round": len(attempt["observations"]) + 1,
                    "assistant_text": trace.get("assistant_text"),
                    "tool_calls": trace.get("assistant_tool_calls") or [],
                    "source": f"{path.relative_to(episode)}:{line}",
                }
            )
    if missing_ids:
        warnings.append(f"有 {len(missing_ids)} 个模型尝试 ID 未唯一关联周期/trace；未按文件名或时间猜测。")


def jev_evidence(summary, agent):
    if agent != "gui_jev":
        return None
    value = summary if isinstance(summary, dict) else {}
    notes = ["操作来自子代理返回摘要，不与脱敏的外层 CLI 事件按序强行配对；未记录逐动作 PNG 截图。"]
    if value.get("turns_truncated"):
        notes.append("turns_truncated=true：原记录只保留末尾最多 40 个动作。")
    if value.get("context_truncated"):
        notes.append("context_truncated=true：摘要经过压缩，动作列表可能只保留末尾 8 条。")
    if value.get("evidence_truncated"):
        notes.append("evidence_truncated=true：摘要进一步截断，动作列表已被省略。")
    turns = value.get("turns") if isinstance(value.get("turns"), list) else []
    if not turns:
        notes.append("此周期返回摘要未保留动作明细；仍可查看最后操作、诊断和返回结果。")
    return {"turns": turns, "notes": notes, "source": "subagent_result.summary.turns"}


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def add_frames(episode: Path, record: dict, images: Images, attempts: list) -> list:
    frames = []
    seen = set()

    def add(label, raw, context=None):
        index = images.add(episode, raw)
        if index is not None and (index not in seen or context is not None):
            seen.add(index)
            frame = {"label": label, "image": index}
            if context is not None:
                frame["context"] = context
            frames.append(frame)

    orchestration = record.get("orchestration") or {}
    result = orchestration.get("subagent_result") or {}
    add("Slow 当前观察 / 派发前", record.get("clean_screenshot_path") or record.get("screenshot"))
    for artifact in record.get("observation_artifacts") or []:
        if artifact.get("role") == "current_clean":
            add("Slow 当前观察 / 派发前", artifact.get("path"))
    for attempt in attempts:
        observation = attempt.get("observation") or {}
        add("子代理输入观察", observation.get("screenshot_path"))
        for item in attempt.get("observations", []):
            add(
                f"General 中间观察 {item['model_round']} / 模型决策前",
                item["observation"]["screenshot_path"],
                {
                    "attempt_id": attempt["id"],
                    "observation_id": item["observation"].get("observation_id"),
                    "source": item["source"],
                    "model_text": item["assistant_text"],
                    "model_tool_proposals": item["tool_calls"],
                    "note": "截图是该次 General 模型调用的输入；下列调用是模型提议，执行结果请看工具 trace。",
                },
            )
    add("子代理返回观察（不代表父代理已验证）", (result.get("last_observation") or {}).get("screenshot_path"))
    for label, owner in [("Slow 证据引用", orchestration.get("decision") or {}), ("子代理证据引用", result)]:
        for ref in owner.get("evidence_refs") or []:
            if isinstance(ref, str) and Path(ref).suffix.lower() in IMAGE_SUFFIXES:
                add(label, ref)
    return frames


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def collect(root: Path, output: Path, embed=False, task_ids=(), model_observations=True) -> dict:
    root = root.resolve()
    images = Images(output.resolve(), embed)
    tasks = []
    episodes = sorted([*(root / "episodes").glob("*"), *(root / "jobs").glob("job-*/episodes/*")])
    for episode in episodes:
        if not episode.is_dir():
            continue
        info = read_json(episode / "episode.json")
        task_id = str(info.get("task_id", episode.name))
        if task_ids and task_id not in task_ids:
            continue
        warnings = []
        if not info:
            warnings.append("缺少 episode.json；停止原因、得分或耗时可能不完整。")
        cycles = load_cycles(episode, warnings)
        attempts = load_attempts(episode, cycles, warnings)
        if model_observations:
            load_model_observations(episode, cycles, attempts, warnings)
        else:
            warnings.append("已跳过 llm_trace：未提取 General 周期内的模型观察截图。")
        cycle_counts = Counter(cycle_id(c) for c in cycles if cycle_id(c))
        duplicate_ids = {cid for cid, count in cycle_counts.items() if count > 1}
        if duplicate_ids:
            warnings.append("轨迹存在重复 cycle_id；对应工具 trace 单独保留，不猜测其周期归属。")
        used = set()
        entries = []
        for position, record in enumerate(cycles, 1):
            orchestration = record.get("orchestration") or {}
            result = orchestration.get("subagent_result") or {}
            cid = cycle_id(record)
            matches = []
            for i, attempt in enumerate(attempts):
                # Cycle IDs and attempt IDs are authoritative; don't join by subgoal text.
                same_cycle = cid and attempt["cycle_id"] == cid
                same_attempt = result.get("attempt_id") and attempt["id"] == result["attempt_id"]
                conflicting_cycle = same_attempt and cid and attempt["cycle_id"] and not same_cycle
                if conflicting_cycle:
                    warning = (
                        f"attempt={attempt['id']} 的 trace cycle_id 与周期记录冲突；"
                        "未按 attempt ID 强行关联。"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                ambiguous_cycle = cid in duplicate_ids or attempt["cycle_id"] in duplicate_ids
                if (
                    i not in used
                    and not conflicting_cycle
                    and not ambiguous_cycle
                    and (same_cycle or same_attempt)
                ):
                    matches.append(attempt)
                    used.add(i)
            decision = orchestration.get("decision") or {}
            summary = decoded(result.get("summary"))
            entries.append(
                {
                    "number": record.get("session_step", record.get("step", position)),
                    "phase": record.get("_phase"),
                    "kind": "cycle",
                    "cycle_id": cid,
                    "status": record.get("status"),
                    "decision": decision,
                    "text": record.get("assistant_text") or decision.get("summary") or record.get("result"),
                    "reasoning": "\n\n".join(
                        str(record[k]) for k in ("llm_reasoning", "llm_thinking") if record.get(k)
                    ),
                    "terminal": record.get("terminal_decision"),
                    "result": result,
                    "summary": summary,
                    "usage": orchestration.get("usage"),
                    "jev": jev_evidence(summary, (decision.get("contract") or {}).get("agent_id")),
                    "exit_reason": orchestration.get("exit_reason"),
                    "attempts": matches,
                    "frames": add_frames(episode, record, images, matches),
                    "source": record["_source"],
                    "warnings": [],
                }
            )
            if result and not matches:
                entries[-1]["warnings"].append("未找到该子代理尝试的工具 trace；工具明细未知。")
            # Only a next cycle in the same phase can supply the next parent verification.
            next_record = cycles[position] if position < len(cycles) else None
            if next_record and next_record.get("_phase") == record.get("_phase"):
                entries[-1]["next_verification"] = (
                    (next_record.get("orchestration") or {}).get("decision") or {}
                ).get("verification")
        for i, attempt in enumerate(attempts):
            if i in used:
                continue
            warnings.append(f"尝试 {attempt['id'] or '无 ID'} 有 trace，但没有对应的已写入周期；单独保留。")
            record = {"orchestration": {}}
            entries.append(
                {
                    "kind": "trace_only",
                    "number": None,
                    "phase": attempt["phase"],
                    "cycle_id": attempt["cycle_id"],
                    "decision": {"contract": attempt["contract"]},
                    "attempts": [attempt],
                    "frames": add_frames(episode, record, images, [attempt]),
                    "source": attempt["source"],
                    "warnings": ["仅有执行 trace，未推断完成状态或父代理验证。"],
                }
            )
        task = read_json(episode / "task.json")
        evaluation = read_json(episode / "evaluation.json")
        score, score_source = None, None
        for value, source in [
            (evaluation.get("score"), "evaluation.json:score"),
            (evaluation.get("raw"), "evaluation.json:raw"),
            (info.get("score"), "episode.json:score"),
        ]:
            if finite_number(value):
                score, score_source = value, source
                break
        if score_source == "episode.json:score":
            warnings.append("evaluation.json 无可用数值得分，使用 episode.json 的记录得分。")
        if info.get("steps") is not None and info["steps"] != len(cycles):
            warnings.append(f"episode.steps={info['steps']}，轨迹周期记录={len(cycles)}。")
        phases = []
        for path in sorted((episode / "phases").glob("*/task.json")):
            phase = read_json(path)
            phases.append(
                {
                    "index": phase.get("phase_index", phase_of(path, episode)),
                    "instruction": phase.get("instruction", ""),
                }
            )
        actions = [a for attempt in attempts for a in attempt["actions"]]
        tasks.append(
            {
                "id": task_id,
                "source": str(episode.relative_to(root)),
                "instruction": task.get("instruction", ""),
                "score": score,
                "score_source": score_source,
                "evaluation": evaluation,
                "status": info.get("status", "unknown"),
                "stop": info.get("stop_reason", "unknown"),
                "seconds": info.get("seconds"),
                "model": info.get("model"),
                "reported_steps": info.get("steps"),
                "cycle_count": len(cycles),
                "attempt_count": len(attempts),
                "tool_count": sum(a["status"] != "result_without_matched_start" for a in actions),
                "unmatched_result_count": sum(a["status"] == "result_without_matched_start" for a in actions),
                "unmatched_start_count": sum(a["status"] == "start_without_matched_result" for a in actions),
                "failed_tool_count": sum(a.get("failure", False) for a in actions),
                "model_observation_count": sum(len(a.get("observations", [])) for a in attempts),
                "jev_turn_count": sum(len((s.get("jev") or {}).get("turns", [])) for s in entries),
                "redacted_count": sum(
                    isinstance(a.get("arguments"), dict) and a["arguments"].get("redacted") is True
                    for a in actions
                ),
                "phases": phases,
                "phase_results": read_json(episode / "phase_results.json", []),
                "warnings": warnings,
                "entries": entries,
            }
        )
        # Frame contexts already retain the compact model evidence. Avoid duplicating
        # every model proposal and historical screenshot path in the HTML payload.
        for attempt in attempts:
            attempt.pop("observations", None)
        print(
            f"  {task_id}: score={score}, cycles={len(cycles)}, attempts={len(attempts)}, "
            f"tool_starts={tasks[-1]['tool_count']}, "
            f"model_observations={tasks[-1]['model_observation_count']}, "
            f"jev_turns={tasks[-1]['jev_turn_count']}",
            flush=True,
        )
    if not tasks:
        raise ValueError(f"No matching episodes found under {root} (episodes/* or jobs/job-*/episodes/*)")
    missing = set(task_ids) - {t["id"] for t in tasks}
    if missing:
        raise ValueError(f"Unknown task IDs: {', '.join(sorted(missing))}")
    counts = Counter(task["id"] for task in tasks)
    for task in tasks:
        if counts[task["id"]] > 1:
            task["warnings"].append("同题存在多份执行记录，分别展示；汇总统计包含每份执行，未挑选最新结果。")
    return {
        "title": root.name,
        "tasks": tasks,
        "images": images.items,
        "count_note": (
            "周期来自 traj / steps；工具调用数为 trace 中 tool_started 事件数（含失败、观察和脱敏调用），"
            "不是 GUI 原子动作数或报告中的估算步数。"
        ),
    }


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        Path(__file__)
        .with_name("jevduo_trajectory_viewer.html")
        .read_text(encoding="utf-8")
        .replace("__TRAJECTORY_DATA__", payload)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        type=Path,
        help="Run root with jobs/job-*/episodes/<task>, or archive/job root with episodes/<task>",
    )
    parser.add_argument("-o", "--output", type=Path, help="Default: ROOT/trajectories.html")
    parser.add_argument("--task", action="append", default=[], help="Task ID; repeat to select tasks")
    parser.add_argument("--embed-images", action="store_true", help="Embed screenshots in a standalone HTML")
    parser.add_argument(
        "--skip-model-observations",
        action="store_true",
        help="Skip potentially large llm_trace files; omit General intermediate observations",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing generated HTML")
    args = parser.parse_args()
    output = (args.output or args.root / "trajectories.html").resolve()
    if output.suffix.lower() != ".html":
        parser.error("Output must have an .html extension")
    if output.exists() and not args.force:
        parser.error(f"Output exists; choose another path or use --force: {output}")
    try:
        data = collect(
            args.root,
            output,
            args.embed_images,
            args.task,
            model_observations=not args.skip_model_observations,
        )
        document = render(data)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.force else "x", encoding="utf-8") as stream:
            stream.write(document)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Export failed: {error}\n")
    print(
        f"Exported {len(data['tasks'])} tasks, {len(data['images'])} unique images "
        f"({sum(i['missing'] for i in data['images'])} missing) → {output}"
    )
    if not args.embed_images:
        print("Images use relative paths; keep the HTML with the archive or use --embed-images.")


if __name__ == "__main__":
    main()
