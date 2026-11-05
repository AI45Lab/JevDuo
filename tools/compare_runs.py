#!/usr/bin/env python3
"""两个批次的逐题对照（默认 kev vs jev）：把「小脑的锅」和「harness 的锅」分开。

用法:
    python3 tools/compare_runs.py --a osw21-500-kev --b osw21-500-jev-ab

输出：逐题对照表 + 批次级对比（得分 / 步数 / 空转率 / token 成本）。
纯标准库、只读结果目录。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

_TOOL = Path(__file__).resolve().parent / "batch_summary.py"
_spec = importlib.util.spec_from_file_location("batch_summary", _TOOL)
batch_summary = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(batch_summary)

#: 不产生 UI 动作的赢家。真机路径下 grounder 是关闭的
#: （`jevduo_eval/entry.py` 显式传 `grounder=None`），因此 `visual_fallback`
#: 与 `return_to_planner` **都**编译不出动作，两者都是真空转。
#: （2026-10-25 修正：旧口径只数 `return_to_planner`，把真实空转率低估了一半。）
NOOP_OPERATIONS = ("return_to_planner", "visual_fallback")

#: 快路径：从原始行里抠出 winner 的 operation（整批事件流上 GB 级，逐行 json.loads 太慢）
_WINNER_RE = re.compile(r'"winner":\s*\{[^{}]*?"operation":\s*"([^"]+)"')
#: gui 事件里"没有可执行动作"的标志（json.dumps 的默认分隔符是 ": "）
_NULL_ACTION = '"action": null'


def winner_stats(episode_dir: Path) -> dict[str, Any]:
    """统计一个 episode 的排序赢家分布与空转率。

    空转口径 = "排序后没有编译出任何动作"的决策占比（gui 事件里 `action` 为 null），
    而不是"winner 是不是某个回退 operation"——两者在真机路径下等价，
    但前者对将来打开 grounder 的情形仍然正确。
    """
    winners = Counter()
    noop = 0
    events = episode_dir / "events.jsonl"
    if events.is_file():
        for line in events.open(encoding="utf-8"):
            if '"winner"' not in line:
                continue
            match = _WINNER_RE.search(line)
            if match:
                winners[match.group(1)] += 1
            else:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or record
                winner = payload.get("winner")
                if isinstance(winner, dict):
                    winners[str(winner.get("operation"))] += 1
            if _NULL_ACTION in line:
                noop += 1
    total = sum(winners.values())
    fallback = sum(count for op, count in winners.items() if op == "visual_fallback")
    return {
        "winners": total,
        "noop": noop,
        "fallback": fallback,
        "noop_rate": (noop / total) if total else 0.0,
        "fallback_rate": (fallback / total) if total else 0.0,
    }


def enrich(rows: list[dict[str, Any]], runs_root: Path, prefix: str) -> dict[str, dict[str, Any]]:
    """给每行补上所属 run 目录与空转率统计。"""
    enriched: dict[str, dict[str, Any]] = {}
    for row in rows:
        stats = winner_stats(runs_root / row["run"] / "episodes" / row["task"])
        enriched[row["task"]] = {**row, **stats}
    return enriched


def compare(a_rows: dict[str, dict[str, Any]], b_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """两个批次逐题对照，产出可比任务集合与批次级差异。"""
    shared = sorted(set(a_rows) & set(b_rows))
    pairs = []
    for task in shared:
        a, b = a_rows[task], b_rows[task]
        pairs.append({
            "task": task,
            "a_score": a["score"], "b_score": b["score"],
            "a_stop": a["stop"], "b_stop": b["stop"],
            "a_steps": a["steps"], "b_steps": b["steps"],
            "a_noop": a["noop_rate"], "b_noop": b["noop_rate"],
            "a_tokens": a["prompt_tokens"] + a["completion_tokens"],
            "b_tokens": b["prompt_tokens"] + b["completion_tokens"],
        })

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def scores(rows: dict[str, dict[str, Any]]) -> list[float]:
        return [row["score"] for row in rows.values()
                if row["status"] == "scored" and isinstance(row["score"], (int, float))]

    return {
        "pairs": pairs,
        "shared": len(shared),
        "a_total": len(a_rows), "b_total": len(b_rows),
        "a_avg": avg(scores(a_rows)), "b_avg": avg(scores(b_rows)),
        "a_noop": avg([row["noop_rate"] for row in a_rows.values()]),
        "b_noop": avg([row["noop_rate"] for row in b_rows.values()]),
        "a_steps": avg([row["steps"] or 0 for row in a_rows.values()]),
        "b_steps": avg([row["steps"] or 0 for row in b_rows.values()]),
        "a_tokens": sum(row["prompt_tokens"] + row["completion_tokens"] for row in a_rows.values()),
        "b_tokens": sum(row["prompt_tokens"] + row["completion_tokens"] for row in b_rows.values()),
        "b_better": sum(1 for p in pairs
                        if isinstance(p["b_score"], (int, float)) and isinstance(p["a_score"], (int, float))
                        and p["b_score"] > p["a_score"]),
        "a_better": sum(1 for p in pairs
                        if isinstance(p["b_score"], (int, float)) and isinstance(p["a_score"], (int, float))
                        and p["a_score"] > p["b_score"]),
    }


def render(result: dict[str, Any], label_a: str, label_b: str) -> str:
    """渲染 Markdown 对照报告。"""
    lines = [f"# 批次对照 · {label_a} → {label_b}", ""]
    lines.append(
        f"- 可比题数 **{result['shared']}**"
        f"（{label_a} 共 {result['a_total']} 题，{label_b} 共 {result['b_total']} 题）"
    )
    lines.append(f"- 平均分：{label_a} **{result['a_avg']:.4f}** → {label_b} **{result['b_avg']:.4f}**")
    lines.append(
        f"- 平均空转率（排序后没有编译出任何动作的决策占比）："
        f"{label_a} **{result['a_noop']:.0%}** → {label_b} **{result['b_noop']:.0%}**"
    )
    lines.append(f"- 平均步数：{label_a} {result['a_steps']:.1f} → {label_b} {result['b_steps']:.1f}")
    lines.append(f"- token 合计：{label_a} {result['a_tokens']:,} → {label_b} {result['b_tokens']:,}")
    lines.append(
        f"- 逐题胜负：{label_b} 更好 {result['b_better']} 题 / "
        f"{label_a} 更好 {result['a_better']} 题"
    )
    lines.append("")
    lines.append("| 题 | A 得分 | B 得分 | A 步 | B 步 | A 空转 | B 空转 | A 终态 | B 终态 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for pair in result["pairs"]:
        a_score = "—" if pair["a_score"] is None else f"{pair['a_score']:.3f}"
        b_score = "—" if pair["b_score"] is None else f"{pair['b_score']:.3f}"
        lines.append(
            f"| {pair['task']} | {a_score} | {b_score} | {pair['a_steps']} | {pair['b_steps']} | "
            f"{pair['a_noop']:.0%} | {pair['b_noop']:.0%} | "
            f"{batch_summary._brief(pair['a_stop'])} | {batch_summary._brief(pair['b_stop'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _collect(runs_root: Path, prefixes: list[str]) -> dict[str, dict[str, Any]]:
    """合并多个前缀的 run 目录（同题取**最新批次**那次：已出分优先，其次 run-id 时间戳晚）。"""
    merged: dict[str, dict[str, Any]] = {}
    for prefix in prefixes:
        rows = enrich(batch_summary.collect_rows(runs_root, prefix), runs_root, prefix)
        for task, row in rows.items():
            current = merged.get(task)
            if current is None or _rank(row) > _rank(current):
                merged[task] = row
    return merged


def _rank(row: dict[str, Any]) -> tuple[int, str]:
    return (1 if row["status"] == "scored" else 0, batch_summary.run_timestamp(row["run"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="两个批次逐题对照")
    parser.add_argument("--runs-root", type=Path, default=Path("rjob_logs"))
    parser.add_argument("--a", default="osw21-500-kev", help="对照组（基线）前缀，逗号分隔可多个")
    parser.add_argument("--b", default="osw21-500-jev-ab", help="实验组前缀，逗号分隔可多个")
    parser.add_argument("--out", type=Path, default=None, help="把 Markdown 写到该文件")
    args = parser.parse_args(argv)

    a_prefixes = [p.strip() for p in args.a.split(",") if p.strip()]
    b_prefixes = [p.strip() for p in args.b.split(",") if p.strip()]
    a_rows = _collect(args.runs_root, a_prefixes)
    b_rows = _collect(args.runs_root, b_prefixes)
    if not a_rows or not b_rows:
        print(f"缺少可对照的 run：{args.a}={len(a_rows)} 题，{args.b}={len(b_rows)} 题")
        return 1
    result = compare(a_rows, b_rows)
    report = render(result, ",".join(a_prefixes), ",".join(b_prefixes))
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"\n[已写入] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
