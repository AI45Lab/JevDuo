"""批量基线汇总器：把 rjob_logs/ 下多个 run 目录汇成一份 Markdown 基线报告。

纯标准库（无新增依赖），只读结果目录、不碰 VM、不调模型。
聚合内容：得分分布 / 停止原因 / 每题耗时 / 慢模型 token 成本 / 需补跑的环境失败清单。

用法：
    python3 tools/batch_summary.py --runs-root rjob_logs --prefix osw21-500-kev
    python3 tools/batch_summary.py --runs-root rjob_logs --out docs/baseline.md
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

#: 判定为「环境失败、应补跑」的错误特征（其余失败算模型侧或题目侧，计入基线）
ENV_ERROR_MARKERS = (
    "VM failed to become ready",
    "GITLAB_URL and GITLAB_PRIVATE_TOKEN",
    "LLM whole-table judgment",
    # 判分/评估侧的基础设施故障：缺 judge 凭据、视觉模型调用失败、动作回执缺失
    "No API key found for provider",
    "visual model call failed",
    "no execution acknowledgement",
)

#: 评估侧基础设施失败：重跑无望（当前 judge 的媒体上限/能力限制），要换 judge 或绕过才可能通过
JUDGE_INFRA_MARKERS = (
    "media exceeds size limit",
)


def is_judge_infra_failure(row: dict[str, Any]) -> bool:
    """判分侧的基础设施失败（如 judge 媒体上限），与"补跑可解"的环境失败区分开。"""
    text = f"{row.get('error') or ''} {row.get('exception') or ''}"
    return any(marker in text for marker in JUDGE_INFRA_MARKERS)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


#: 任务域 → 建议的分析方向（画布类缺语义目标，web 类是步内重规划效率问题）
DOMAIN_HINTS = {
    "gimp_image": "画布类：无语义目标，需补视觉/结构化线索",
    "video_editor": "视频编辑类：时间轴元素无 a11y 语义",
    "libreoffice_impress": "画布类：幻灯片元素无 a11y 语义",
    "browser_web": "web 类：重点查步内重规划与操作被拒",
    "libreoffice_calc": "表格类：a11y 目标清晰，查能否推满分",
    "libreoffice_writer": "文档类：查输入/保存路径",
    "vscode_ide": "IDE 类：当前最好，验证回退候选改动",
    "media_player": "播放器类",
    "system_settings": "系统设置类",
    "email_client": "邮件客户端类",
    "terminal_cli": "终端类",
    "other": "其它",
}

#: 域判定按**题面点名的应用**来分（顺序即优先级），避免泛化关键词误分类
_DOMAIN_PATTERNS = (
    ("gimp_image", r"\bgimp\b"),
    ("video_editor", r"shotcut|video editor|video post-production|footage"),
    ("libreoffice_impress", r"\.pptx|slides?\b|presentation\b|deck\b"),
    ("libreoffice_calc", r"\.xlsx|\.csv|spreadsheet|\bcalc\b|formula|worksheet"),
    ("libreoffice_writer", r"\.docx|\bwriter\b|paragraph|letter|essay|thesis"),
    ("vscode_ide", r"vscode|vs code|code editor|repository|\brepo\b|\bgit\b|\bcommit\b"),
    ("browser_web", r"http://|https://|\bwebsite\b|browser|chrome|web page|webform|upload|download"),
    ("media_player", r"\bvlc\b|media player|playlist|\bmp3\b|\bmp4\b"),
    ("email_client", r"thunderbird|\bemail\b|inbox|mail client"),
    ("system_settings", r"system settings|bluetooth|\bvolume\b|network settings|display settings"),
    ("terminal_cli", r"\bterminal\b|bash script|shell command"),
)



def domain_of(instruction: str) -> str:
    """按题面文本粗判应用域（用于分诊建议，不参与评分）。"""
    text = (instruction or "").lower()
    for name, pattern in _DOMAIN_PATTERNS:
        if re.search(pattern, text):
            return name
    return "other"


def _noop_rate(episode_dir: Path) -> float | None:
    """该题的"空转"占比：排序后**没有可执行动作**的决策比例。

    口径说明：判空转看的是"动作有没有编译出来"（gui 事件里 `action` 为 null），
    而不是"winner 是不是回退项"。真机路径下 grounder 是关的
    （`jevduo_eval/entry.py` 显式传 `grounder=None`），因此 `visual_fallback`
    与 `return_to_planner` **同样编译不出动作**——两者都是真空转。
    （2026-10-25 修正：旧口径只数 `return_to_planner`，把 44% 的真实空转
    报成了 22%，低估了一倍。）
    """
    events = episode_dir / "events.jsonl"
    if not events.is_file():
        return None
    total = noop = 0
    with events.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("route") != "gui" or not record.get("winner"):
                continue
            total += 1
            if record.get("action") is None:
                noop += 1
    return (noop / total) if total else None


def _steps_from_events(episode_dir: Path) -> int:
    """从事件流回补真实步数（异常路径下 episode.json 的 steps 可能是 0）。"""
    events = episode_dir / "events.jsonl"
    if not events.is_file():
        return 0
    last = 0
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") or record
        step = payload.get("step")
        if isinstance(step, int) and step > last:
            last = step
    return last


def _episode_tokens(episode_dir: Path) -> tuple[int, int, int, int]:
    """返回 (调用次数, prompt tokens, completion tokens, cached tokens)；只统计慢模型调用。"""
    calls = prompt = completion = cached = 0
    llm_calls = episode_dir / "llm_calls.jsonl"
    if not llm_calls.is_file():
        return 0, 0, 0, 0
    for line in llm_calls.read_text(encoding="utf-8").splitlines():
        record = _read_json_line(line)
        if not record:
            continue
        usage = (record.get("response") or {}).get("usage") or record.get("usage") or {}
        calls += 1
        prompt += usage.get("prompt_tokens") or 0
        completion += usage.get("completion_tokens") or 0
        details = usage.get("prompt_tokens_details") or {}
        cached += details.get("cached_tokens") or 0
    return calls, prompt, completion, cached


def _read_json_line(line: str) -> dict[str, Any]:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def collect_rows(runs_root: Path, prefix: str = "") -> list[dict[str, Any]]:
    """扫描 runs_root 下匹配的 run 目录，收集每题的汇总行。

    `prefix` 可为逗号分隔的多个前缀（补跑片前缀常与主批次不同）。
    同一题出现在多个 run 时取「已出分优先、其次批次时间戳晚」的那条。
    """
    prefixes = [p.strip() for p in prefix.split(",") if p.strip()]
    rows: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return rows
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        if prefixes and not any(run_dir.name.startswith(p) for p in prefixes):
            continue
        episodes = run_dir / "episodes"
        if not episodes.is_dir():
            continue
        for episode_dir in sorted(p for p in episodes.iterdir() if p.is_dir()):
            episode = _read_json(episode_dir / "episode.json")
            if not episode:
                continue
            timing = _read_json(episode_dir / "timing.json")
            calls, prompt, completion, cached = _episode_tokens(episode_dir)
            steps = episode.get("steps")
            if not isinstance(steps, int) or steps <= 0:
                # 异常路径下 episode.json 的 steps 记成 0，用事件流回补
                steps = _steps_from_events(episode_dir)
            task_meta = _read_json(episode_dir / "task.json")
            domain = domain_of(str(task_meta.get("instruction") or ""))
            rows.append({
                "run": run_dir.name,
                "mtime": (episode_dir / "episode.json").stat().st_mtime,
                "task": episode_dir.name,
                "score": episode.get("score"),
                "status": episode.get("status"),
                "stop": episode.get("stop_reason"),
                "steps": steps,
                "exception": episode.get("exception_type"),
                "domain": domain,
                "noop_rate": _noop_rate(episode_dir),
                "error": (episode.get("error") or "")[:200],
                "wall_seconds": timing.get("wall_seconds"),
                "calls": calls,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
            })
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = best.get(row["task"])
        if current is None or _row_rank(row) > _row_rank(current):
            best[row["task"]] = row
    return sorted(best.values(), key=lambda row: row["task"])


def run_timestamp(run_name: str) -> str:
    """从 run-id 里取批次时间戳（形如 20261024-235014），用于判断新旧批次。

    不能用文件 mtime：GPFS 上 mtime 精度是秒级，同秒写入的两批排不出先后。
    """
    # 批次时间戳有两种格式：20261024-235014（6 位）与 20261025-0450（4 位），
    # 只认 6 位会让新批次之间排不出新旧（c10/c20 合并时全落空 → 挑错批次）
    match = re.search(r"(\d{8}-\d{4,6})", run_name)
    return match.group(1) if match else ""


def _row_rank(row: dict[str, Any]) -> tuple[int, str]:
    """去重优先级：已出分 > 批次时间戳晚。

    补跑的目的是用修复后的配置重测，所以失败题取**最新批次**那次的结果——
    旧批次里的错误（缺凭据等）已被后续补跑取代，不该盖住新结果。
    （run-id 前缀不同，不能按目录名排；文件 mtime 在 GPFS 上只有秒级精度，也排不出先后。）
    """
    return (1 if row["status"] == "scored" else 0, run_timestamp(row["run"]))


def is_env_failure(row: dict[str, Any]) -> bool:
    """环境/配置类失败（应补跑），区别于模型侧失败（计入基线）。"""
    text = f"{row.get('error') or ''} {row.get('exception') or ''}"
    return any(marker in text for marker in ENV_ERROR_MARKERS)


#: 失败模式标签 → 说明（供报告与后续按任务定位 harness 模块用）
TASK_TAGS = {
    "env_failure": "环境/配置失败（需补跑，非模型能力）",
    "model_schema": "模型侧异常（schema 不合规、升档重试耗尽）",
    "judge_infra": "评估侧基础设施失败（judge 媒体上限等，重跑无望）",
    "infeasible": "模型判定任务不可行/放弃（长自然语言 stop_reason）",
    "no_action": "空转过多触发 no_action_limit",
    "wall_limit": "墙钟预算耗尽",
    "max_steps": "步数预算耗尽",
    "done_full": "完成且满分",
    "done_partial": "完成但部分分",
    "done_zero": "完成但零分",
}


def classify_task(row: dict[str, Any]) -> str:
    """给单题打失败模式标签：便于后续按题定位 harness 模块。

    先看停止原因，再看状态：max_steps / wall_time_limit 的题同样会走评分
    （status=scored、score 可能为 0），按状态优先会把它们误归成「完成但零分」。
    """
    stop = str(row.get("stop") or "")
    if row["status"] != "scored":
        if is_judge_infra_failure(row):
            return "judge_infra"
        if is_env_failure(row):
            return "env_failure"
        if row.get("exception") == "SlowModelSchemaError" or "missing required fields" in stop:
            return "model_schema"
        if len(stop) > 40:      # 模型的自然语言理由 = 判定不可行/放弃
            return "infeasible"
        if stop == "wall_time_limit":
            return "wall_limit"
        if stop == "max_steps":
            return "max_steps"
        if stop == "no_action_limit":
            return "no_action"
        return "model_schema"
    if stop in {"no_action_limit", "wall_time_limit", "max_steps"}:
        return {"no_action_limit": "no_action", "wall_time_limit": "wall_limit",
                "max_steps": "max_steps"}[stop]
    score = row["score"]
    if isinstance(score, (int, float)) and score >= 1.0:
        return "done_full"
    if isinstance(score, (int, float)) and score > 0:
        return "done_partial"
    return "done_zero"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """给出行级数据算出的批次级指标。"""
    scored = [row for row in rows if row["status"] == "scored"]
    scores = [row["score"] for row in scored if isinstance(row["score"], (int, float))]
    env_failed = [row for row in rows if row["status"] != "scored" and is_env_failure(row)]
    judge_infra = [row for row in rows if row["status"] != "scored" and is_judge_infra_failure(row)]
    model_failed = [row for row in rows
                    if row["status"] != "scored"
                    and not is_env_failure(row) and not is_judge_infra_failure(row)]
    return {
        "total": len(rows),
        "scored": len(scored),
        "env_failed": env_failed,
        "judge_infra": judge_infra,
        "model_failed": model_failed,
        "scores": scores,
        "full_hits": sum(1 for score in scores if score >= 1.0),
        "partial_hits": sum(1 for score in scores if 0 < score < 1.0),
        "zeros": sum(1 for score in scores if score == 0),
        "average_scored": (sum(scores) / len(scores)) if scores else None,
        "average_all": (sum(scores) / len(rows)) if rows else None,
        "stop_reasons": Counter(_brief(row["stop"]) for row in rows),
        "wall_seconds": sum(row["wall_seconds"] or 0 for row in rows),
        "calls": sum(row["calls"] for row in rows),
        "prompt_tokens": sum(row["prompt_tokens"] for row in rows),
        "completion_tokens": sum(row["completion_tokens"] for row in rows),
    }


def _brief(value: Any, limit: int = 28) -> str:
    text = str(value or "-")
    return text if len(text) <= limit else text[:limit] + "…"


def render_markdown(rows: list[dict[str, Any]], stats: dict[str, Any], title: str) -> str:
    """把行与统计渲染成 Markdown 报告。"""
    lines: list[str] = [f"# {title}", ""]
    lines.append(
        f"- 已完成 **{stats['total']}** 题（scored {stats['scored']}，"
        f"环境失败 {len(stats['env_failed'])}，评估基础设施 {len(stats['judge_infra'])}，"
        f"模型侧失败 {len(stats['model_failed'])}）"
    )
    if stats["scores"]:
        lines.append(
            f"- 得分：满分 {stats['full_hits']} / 部分分 {stats['partial_hits']} / 零分 {stats['zeros']}"
        )
        lines.append(
            f"- 平均分：已出分题 {stats['average_scored']:.4f}；全题面 {stats['average_all']:.4f}"
        )
    lines.append(f"- 停止原因：{dict(stats['stop_reasons'])}")
    lines.append(f"- 各题耗时合计 {stats['wall_seconds'] / 3600:.1f} 小时")
    total_tokens = stats["prompt_tokens"] + stats["completion_tokens"]
    lines.append(
        f"- 慢模型调用 {stats['calls']} 次，prompt {stats['prompt_tokens']:,} / "
        f"completion {stats['completion_tokens']:,} / 合计 {total_tokens:,} tokens"
    )
    lines.append("")
    lines.append("## 逐题明细")
    lines.append("")
    lines.append("| 题 | 得分 | 状态 | 停止原因 | 步数 | 墙钟(分) | 空转 | 域 | 调用 | tokens |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        score = row["score"]
        if score is None:
            score_text = "—"
        elif isinstance(score, (int, float)):
            score_text = f"{score:.4f}"
        else:
            score_text = str(score)
        wall = row["wall_seconds"]
        wall_text = f"{wall / 60:.1f}" if isinstance(wall, (int, float)) else "—"
        tokens = row["prompt_tokens"] + row["completion_tokens"]
        noop = row.get("noop_rate")
        noop_text = f"{noop:.0%}" if isinstance(noop, (int, float)) else "—"
        lines.append(
            f"| {row['task']} | {score_text} | {row['status']} | "
            f"{_brief(row['stop'] or row['exception'])} | {row['steps']} | {wall_text} | "
            f"{noop_text} | {row.get('domain', 'other')} | "
            f"{row['calls']} | {tokens:,} |"
        )
    lines.append("")
    lines.append("## 失败模式分布（按题分组，供后续定位 harness 模块）")
    lines.append("")
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(classify_task(row), []).append(row["task"])
    for tag, tasks in sorted(grouped.items(), key=lambda item: -len(item[1])):
        lines.append(f"- **{tag}**（{TASK_TAGS.get(tag, tag)}）：{len(tasks)} 题 — {', '.join(tasks)}")
    lines.append("")
    lines.append("## 分诊建议（按题）")
    lines.append("")
    lines.append("| 域 | 题数 | 平均空转 | 建议动作 |")
    lines.append("|---|---|---|---|")
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(row.get("domain", "other"), []).append(row)
    for domain, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        noops = [item["noop_rate"] for item in items
                 if isinstance(item.get("noop_rate"), (int, float))]
        avg = sum(noops) / len(noops) if noops else 0.0
        lines.append(
            f"| {domain} | {len(items)} | {avg:.0%} | "
            f"{DOMAIN_HINTS.get(domain, '查逐题轨迹')} |"
        )
    lines.append("")
    if stats["env_failed"]:
        lines.append("## 未出分的执行侧失败（已定位并修复，修复后重跑见「优化后对照」）")
        lines.append("")
        lines.append("| 题 | 异常 | 错误 |")
        lines.append("|---|---|---|")
        for row in stats["env_failed"]:
            lines.append(f"| {row['task']} | {row['exception']} | {row['error'][:90]} |")
        lines.append("")
        tasks = ",".join(row["task"] for row in stats["env_failed"])
        lines.append(
            "这类失败已修（CLI 无回执不再判死整题），修复后用同预算重跑，"
            "结果记为「优化后对照」，**不并入本基线均分**。复现命令："
        )
        lines.append("")
        lines.append("```bash")
        lines.append(f"TASKS={tasks} MAX_STEPS=500 MAX_WALL=10800 \\")
        lines.append("  SLOW_MODEL=minimax-m3.1-flash FAST_MODEL=kev PREFIX=osw21-retry \\")
        lines.append("  bash scripts/run_osw21.sh")
        lines.append("```")
        lines.append("")
    if stats["judge_infra"]:
        lines.append("## 评估侧基础设施失败（已知限制：judge 媒体上限，需改共享 runtime）")
        lines.append("")
        lines.append("| 题 | 异常 | 错误 |")
        lines.append("|---|---|---|")
        for row in stats["judge_infra"]:
            lines.append(f"| {row['task']} | {row['exception']} | {row['error'][:90]} |")
        lines.append("")
    if stats["model_failed"]:
        lines.append("## 模型侧失败（计入基线，不补跑）")
        lines.append("")
        for row in stats["model_failed"]:
            lines.append(f"- {row['task']}：`{_brief(row['error'] or row['exception'], 120)}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 rjob_logs 下的批次基线")
    parser.add_argument("--runs-root", type=Path, default=Path("rjob_logs"))
    parser.add_argument("--prefix", default="", help="只汇总 run 目录名以该前缀开头的批次")
    parser.add_argument("--out", type=Path, default=None, help="把 Markdown 报告写到该文件")
    parser.add_argument("--title", default="OSW2.1 基线汇总")
    args = parser.parse_args(argv)

    rows = collect_rows(args.runs_root, args.prefix)
    if not rows:
        print(f"没有找到匹配的 run 目录：{args.runs_root}（prefix={args.prefix or '*'}）")
        return 1
    stats = summarize(rows)
    report = render_markdown(rows, stats, args.title)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"\n[已写入] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
