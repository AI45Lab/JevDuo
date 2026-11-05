#!/usr/bin/env python3
"""刷新 demo 的官方数据快照（榜 / Sai 逐题分 / 对比子集轨迹）。

站点是**离线可开**的，所以官方数据要 vendored 进 `demo/data/`；本脚本负责
"重新拉一遍 + 校验 + 写回"，保证快照不是手工维护的。

只用标准库（无新增依赖），不 import `jevduo` / `jevduo_eval`（demo 层纪律）。

用法：
    python3 demo/sync_official.py                  # 刷新全部
    python3 demo/sync_official.py --offline        # 只校验现有快照，不联网
    python3 demo/sync_official.py --skip-trajectories   # 只更分数，不下轨迹

数据源：
- 官方榜：https://osworld-v2.xlang.ai/static/data/leaderboard/official-results.json
- Sai 逐题参考 run：https://huggingface.co/datasets/simular-ai/sai-osworld-v21-reference-runs
  （index.jsonl 每行一次干净运行；scorecard/ref-scorecard.json 是官方聚合口径）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "demo" / "data"
OFFICIAL_DIR = DATA_DIR / "official"
TRAJ_DIR = OFFICIAL_DIR / "sai_traj"

LEADERBOARD_URL = "https://osworld-v2.xlang.ai/static/data/leaderboard/official-results.json"
SAI_REPO = "https://huggingface.co/datasets/simular-ai/sai-osworld-v21-reference-runs/resolve/main"
SAI_INDEX_URL = f"{SAI_REPO}/index.jsonl"
SAI_SCORECARD_URL = f"{SAI_REPO}/scorecard/ref-scorecard.json"

USER_AGENT = "tandem-demo-sync/0.1 (+offline snapshot refresh)"


def fetch(url: str, *, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[sync] 写入 {path.relative_to(REPO_ROOT)}")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sync_leaderboard(offline: bool) -> dict[str, Any]:
    path = OFFICIAL_DIR / "leaderboard.json"
    if offline:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    payload = json.loads(fetch(LEADERBOARD_URL).decode("utf-8"))
    snapshot = {
        "source_url": LEADERBOARD_URL,
        "fetched_at": now_iso(),
        "dataset_size": payload.get("datasetSize"),
        "default_step_budget": payload.get("defaultStepBudget"),
        "release_versions": payload.get("releaseVersions"),
        "metrics": payload.get("metrics"),
        "results": payload.get("results") or [],
    }
    write_json(path, snapshot)
    return snapshot


def _sai_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in fetch(SAI_INDEX_URL).decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def sync_sai_tasks(offline: bool) -> dict[str, Any]:
    """Sai 逐题分：每题的 runs / mean / first / best（官方聚合口径）。"""
    path = OFFICIAL_DIR / "sai_tasks.json"
    if offline:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    rows = _sai_rows()
    scorecard: dict[str, Any] = {}
    try:
        raw = json.loads(fetch(SAI_SCORECARD_URL).decode("utf-8"))
        if isinstance(raw, list):
            scorecard = {str(item.get("task")): item for item in raw if isinstance(item, dict)}
        elif isinstance(raw, dict):
            scorecard = raw
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
        print(f"[sync] scorecard 拉取失败（用 index 自算）：{error}")

    per_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task = str(row.get("task") or "")
        if not task or not isinstance(row.get("score"), (int, float)):
            continue
        per_task.setdefault(task, []).append(row)

    tasks: dict[str, Any] = {}
    for task, items in per_task.items():
        scores = [float(item["score"]) for item in items]
        best_row = max(items, key=lambda item: (float(item["score"]), str(item.get("run"))))
        card = scorecard.get(task) or {}
        tasks[task] = {
            "runs": len(items),
            "mean": round(sum(scores) / len(scores), 4),
            "first": scores[0],
            "best": max(scores),
            "run": best_row.get("run"),
            "build": best_row.get("build"),
            "media_tar": best_row.get("media_tar"),
            "text_dir": best_row.get("text_dir"),
            "tag": card.get("tag"),
        }
    best_mean = sum(item["best"] for item in tasks.values()) / len(tasks) if tasks else None
    payload = {
        "source_url": SAI_REPO,
        "fetched_at": now_iso(),
        "model": "anthropic/claude-opus-5",
        "release": "osworld-v2.1",
        "runs_total": len(rows),
        "tasks_total": len(tasks),
        "headline": {
            "mean_of_task_means": round(
                sum(item["mean"] for item in tasks.values()) / len(tasks), 4
            ) if tasks else None,
            "mean_of_task_best": round(best_mean, 4) if best_mean is not None else None,
        },
        "anchor_note": "per-task best 均值 = 官方榜 Sai w/ Claude Opus 5 的 partial（79.38）",
        "tasks": tasks,
    }
    write_json(path, payload)
    return payload


def _trim(text: Any, limit: int = 240) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + "…"
    return text


def sync_sai_trajectories(offline: bool) -> int:
    """对比子集里每道题的 Sai 轨迹摘要（动作序列 + 逐步 reward）。"""
    subset_path = OFFICIAL_DIR / "compare_subset.json"
    tasks_path = OFFICIAL_DIR / "sai_tasks.json"
    if not subset_path.is_file() or not tasks_path.is_file():
        print("[sync] 缺 compare_subset.json / sai_tasks.json，先跑前面的步骤")
        return 0
    subset = json.loads(subset_path.read_text(encoding="utf-8"))
    sai_tasks = json.loads(tasks_path.read_text(encoding="utf-8")).get("tasks") or {}
    wanted = sorted({task for tasks in (subset.get("groups") or {}).values() for task in tasks})
    count = 0
    for task in wanted:
        info = sai_tasks.get(task) or {}
        run = info.get("run")
        if not run:
            print(f"[sync] 跳过 {task}：Sai 没有干净运行")
            continue
        target = TRAJ_DIR / f"{task}.json"
        if offline:
            count += 1
            continue
        try:
            raw = fetch(f"{SAI_REPO}/runs/{run}/{task}/traj.jsonl").decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"[sync] {task} 轨迹拉取失败：{error}")
            continue
        steps: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            steps.append(
                {
                    "step_num": record.get("step_num"),
                    "action_timestamp": record.get("action_timestamp"),
                    "action": _trim(record.get("action"), 200),
                    "response": _trim(record.get("response"), 400),
                    "reward": record.get("reward"),
                    "done": record.get("done"),
                    "screenshot_file": record.get("screenshot_file"),
                    "osworld_action": _trim(record.get("osworld_action"), 200),
                }
            )
        # 保留已有的帧映射（抓帧是另一个步骤，别把彼此的产物冲掉）
        previous = {}
        if target.is_file():
            try:
                previous = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
        payload = {
            "task": task,
            "run": run,
            "score": info.get("best"),
            "mean": info.get("mean"),
            "source_url": f"{SAI_REPO}/runs/{run}/{task}/traj.jsonl",
            "fetched_at": now_iso(),
            "steps": steps,
        }
        for key in ("frames", "frames_dir"):
            if previous.get(key):
                payload[key] = previous[key]
        write_json(target, payload)
        count += 1
    return count


def check_prices(stale_days: int = 30) -> None:
    """价格表是人工维护的（要从定价页抓），这里只检查新鲜度。"""
    path = DATA_DIR / "prices.json"
    if not path.is_file():
        print("[sync] 缺 prices.json —— 成本轴将不显示美元数（红线 3）")
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot = str(payload.get("snapshot") or "")
    try:
        age = (time.time() - time.mktime(time.strptime(snapshot, "%Y-%m-%d"))) / 86400
    except (TypeError, ValueError):
        print(f"[sync] 价格表 snapshot 格式异常：{snapshot!r}")
        return
    if age > stale_days:
        print(f"[sync] ⚠ 价格表已 {age:.0f} 天未更新（{snapshot}），页面需提示核对")
    else:
        print(f"[sync] 价格表新鲜（{snapshot}，{age:.0f} 天前）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷新 demo 的官方数据快照")
    parser.add_argument("--offline", action="store_true", help="只校验现有快照，不联网")
    parser.add_argument("--skip-trajectories", action="store_true", help="不下载 Sai 轨迹")
    args = parser.parse_args(argv)

    sync_leaderboard(args.offline)
    sync_sai_tasks(args.offline)
    if not args.skip_trajectories:
        count = sync_sai_trajectories(args.offline)
        print(f"[sync] Sai 轨迹覆盖 {count} 题")
    check_prices()
    return 0


if __name__ == "__main__":
    sys.exit(main())
