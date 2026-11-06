#!/usr/bin/env python3
"""构建 demo 站点：结果目录 → 单页工作台。

产物（默认 `demo/site/`）：
    index.html                              单页应用（总览/对照/机制/轨迹 四视图）
    assets/app.css|app.js|boot.js           应用内核与双主题 token
    assets/views.css|browser.css|mechanism.css  视图样式（按职责拆三份）
    assets/views/*.js                      四个视图
    assets/player/dual.js                   双轨播放器
    assets/data/overview.json|compare.json  站点数据（同时内联进 index.html）
    assets/data/sai_traj/<task>.json        官方参考 run 的轨迹摘要
    assets/data/episode/<run>/<task>.json   逐题 view model（只给对比子集）
    assets/frames/<run>/<task>/*.jpg        缩放帧（只给对比子集）

站点**离线可开**：总览与对照的数据内联进 index.html；逐题数据按需 fetch，
file:// 下会被浏览器拦截 → 页面给出明确提示（用 http.server 打开即可）。

用法：
    python3 demo/build_site.py --runs-root <stage_memory 批次的 rjob_logs> \
        --out demo/site --benchmark osworld21
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from demo.frames import process_frames  # noqa: E402
from demo.reader.aggregate import (  # noqa: E402
    build_overview,
    collect_merged_rows,
    compare_summary,
    load_official,
)
from demo.reader.episodes import list_episodes  # noqa: E402
from demo.reader.paths import normalize_task_id  # noqa: E402
from demo.reader.viewmodel import build_view_model  # noqa: E402

WEB_DIR = REPO_ROOT / "demo" / "web"
DATA_DIR = REPO_ROOT / "demo" / "data"

#: 合并基线清单（c10 + c20 合成 OSW v2.1 全量 108 题）
MANIFEST_PATH = REPO_ROOT / "demo" / "data" / "merged_manifest.json"


def copy_web(out_dir: Path) -> None:
    """把 demo/web 拷成站点骨架。"""
    for item in WEB_DIR.rglob("*"):
        if item.is_dir():
            continue
        target = out_dir / item.relative_to(WEB_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def inline_json(index_path: Path, slot_id: str, payload: Any) -> None:
    """把数据内联进 index.html 的 `<script type="application/json">` 槽位。

    内联是刻意的：`file://` 下 fetch 读不到本地文件，内联才能让总览与对照
    在双击打开时也可用。
    """
    text = index_path.read_text(encoding="utf-8")
    marker_open = f'<script type="application/json" id="{slot_id}">'
    start = text.index(marker_open) + len(marker_open)
    end = text.index("</script>", start)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = text[:start] + blob + text[end:]
    index_path.write_text(text, encoding="utf-8")


def episode_index(roots: list[Path], selection: dict[str, Any] | None = None) -> dict[str, Path]:
    """题号 → episode 目录。

    `selection` 是合并基线清单（题号 → run_id）。**清单指定的 run 优先**——
    它就是环境侧认定的"这道题该用哪次尝试"；清单没有的题才回退到
    「已出分优先、其次文件时间晚」。
    """
    index: dict[str, tuple[tuple, Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            episodes_dir = run_dir / "episodes"
            if not episodes_dir.is_dir():
                continue
            for episode_dir in sorted(p for p in episodes_dir.iterdir() if p.is_dir()):
                task = normalize_task_id(episode_dir.name)
                score_path = episode_dir / "episode.json"
                if not score_path.is_file():
                    continue
                try:
                    score = json.loads(score_path.read_text(encoding="utf-8")).get("score")
                except (OSError, json.JSONDecodeError):
                    score = None
                mtime = score_path.stat().st_mtime
                key = (0 if score is not None else 1, -mtime)
                if task not in index or key < index[task][0]:
                    index[task] = (key, episode_dir)
    # 清单指定的 run 覆盖
    if selection:
        for task, item in (selection.get("tasks") or {}).items():
            run_id = item.get("run_id")
            for root in roots:
                candidate = root / str(run_id) / "episodes"
                if not candidate.is_dir():
                    continue
                for episode_dir in candidate.iterdir():
                    if episode_dir.is_dir() and normalize_task_id(episode_dir.name) == task:
                        index[task] = ((-1, 0.0), episode_dir)
                        break
    return {task: value[1] for task, value in index.items()}


def disclaimer_notes(overview: dict[str, Any], compare: dict[str, Any]) -> list[str]:
    """首屏披露条（红线 9）。数字全部从数据算，不写死。"""
    ours = overview["ours"]
    release = overview.get("official", {}).get("release") or "v2.1"
    notes = [
        f"对比对象是官方 leaderboard 的 harness 行（release {release}、同步数预算 500 步）——"
        f"双方都是 OSWorld {release}、同题号、**原生官方评测器**，逐题可比。",
        f"底座不同：我们用 {((ours.get('price') or {}).get('model')) or '快模型底座'}，官方 Sai 用 "
        f"Claude Opus 5 —— 成本比 1/{compare.get('cost_ratio') or '?'} 主要来自底座，不是 harness 更省。",
        f"题集不同：我们只尝试 {ours['attempted']} 题（官方 108 题全量），"
        f"未出分的 {ours['unscored']} 题按「未出分」处理，不折算 0。",
        f"基础设施失败吃掉了分数：我们 {ours['unscored']} 题未出分，其中 "
        f"{compare.get('unscored_sai_full', 0)} 题官方 Sai 拿满分（含 0.999999… 的浮点尾差）"
        f"——这是 API/判分侧失败，不是能力差距。",
        f"墙钟是主约束：{ours['stop_reasons'].get('wall_time_limit', 0)} 题撞 4 小时墙钟"
        f"（步数中位 98/500）——调步数预算无效。",
        "本批是**另一代码树的实验**（git_describe=unknown，含仓库主干没有的模块），非仓库主干跑批。",
    ]
    return notes


def build_episodes(
    out_dir: Path,
    roots: list[Path],
    tasks: list[str],
    price: dict[str, Any] | None,
    selection: dict[str, Any] | None = None,
) -> int:
    """给对比子集生成逐题 view model 与缩放帧。"""
    index = episode_index(roots, selection)
    count = 0
    for task in tasks:
        episode_dir = index.get(task)
        if episode_dir is None:
            print(f"[build] 跳过 {task}：找不到 episode 目录")
            continue
        run_id = episode_dir.parent.parent.name
        episodes = list_episodes(run_id, episode_dir.parent)
        ref = next((item for item in episodes if item.task_id == task), None)
        if ref is None:
            continue
        model = build_view_model(ref, price=price)
        # 帧：缩放后写进站点，view model 里补站点内相对路径
        frames_dir = out_dir / "assets" / "frames" / run_id / task
        mapping = process_frames(ref.dir, frames_dir)
        # 帧的站点路径要同时写回 frames 与 timeline——播放器和任务卡缩略图
        # 都从这两处取（timeline 是逐步泳道的帧来源）
        timeline = model.get("timeline") or []
        for frame_index, frame in enumerate(model.get("frames") or []):
            source = (frame.get("screenshot") or {}).get("path") or ""
            name = source.rsplit("/", 1)[-1]
            target_name = mapping.get(name)
            if not target_name:
                continue
            frame["src"] = f"assets/frames/{run_id}/{task}/{target_name}"
            if frame_index < len(timeline):
                timeline[frame_index]["frame"] = frame["src"]
        write_json(out_dir / "assets" / "data" / "episode" / run_id / f"{task}.json", model)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 JevDuo demo 单页工作台")
    parser.add_argument("--runs-root", type=Path, action="append", default=None,
                        help="结果目录根，可给多次（默认：合并基线清单里的批次）")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "demo" / "site")
    parser.add_argument("--benchmark", default="osworld21")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--only-changed", action="store_true",
                        help="按 summary.json 的 mtime 跳过没变的 run（本版本只跳过逐题数据重建）")
    parser.add_argument("--site-only", action="store_true",
                        help="只重建前端：复用已有的 assets/data/*.json，不重读结果目录。"
                             "改了前端之后用这个（不用等几分钟重扫 39GB）")
    args = parser.parse_args(argv)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 只重建前端：先读出已有数据，再覆盖前端文件，最后重新注入内联数据
    # （顺序不能反：清目录会把要复用的数据一起删掉，这坑踩过一次）
    if args.site_only:
        data_dir = out_dir / "assets" / "data"
        overview = json.loads((data_dir / "overview.json").read_text(encoding="utf-8"))
        compare = json.loads((data_dir / "compare.json").read_text(encoding="utf-8"))
        for stale in ("views", "player"):
            shutil.rmtree(out_dir / "assets" / stale, ignore_errors=True)
        copy_web(out_dir)
        index_path = out_dir / "index.html"
        inline_json(index_path, "data-overview", overview)
        inline_json(index_path, "data-compare", compare)
        print(f"[build] 只重建前端：{out_dir}")
        print(f"[build] 逐题行 {len(overview.get('rows') or [])} 题 · 内联数据已重新注入")
        return 0

    # ---- 完整构建
    manifest = {}
    if MANIFEST_PATH.is_file():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    explicit = bool(args.runs_root)
    roots = args.runs_root or [Path(p) for p in (manifest.get("batch_roots") or {}).values()]
    roots = [root for root in roots if root.is_dir()]
    if not roots:
        print("[build] 没有可用的结果目录（先跑 demo/sync_merged.py 生成合并清单）")
        return 2

    # 清掉上一轮产物（只删我们自己生成的那几个名字，不动目标目录里的其它东西）
    for name in ("assets", "task"):
        stale = out_dir / name
        if stale.is_dir():
            shutil.rmtree(stale)
    for name in ("index.html", "compare.html"):
        stale = out_dir / name
        if stale.is_file():
            stale.unlink()

    official = load_official(args.data_dir)
    rows = collect_merged_rows(roots, None if explicit else manifest)
    overview = build_overview(
        rows,
        data_dir=args.data_dir,
        dataset_size=int(manifest.get("dataset_size") or 108),
        missing=[] if explicit else list(manifest.get("missing") or []),
    )

    subset = official["subset"]
    subset_tasks = sorted({task for tasks in (subset.get("groups") or {}).values() for task in tasks})
    overview["subset_tasks"] = subset_tasks
    compare = compare_summary(rows, official["sai_tasks"], overview, subset=subset)
    overview["disclaimer_notes"] = disclaimer_notes(overview, compare)

    copy_web(out_dir)
    write_json(out_dir / "assets" / "data" / "overview.json", overview)
    write_json(out_dir / "assets" / "data" / "compare.json", compare)

    # 官方参考轨迹的文本摘要（对照页要读）
    sai_traj_src = args.data_dir / "official" / "sai_traj"
    if sai_traj_src.is_dir():
        target_traj = out_dir / "assets" / "data" / "sai_traj"
        target_traj.mkdir(parents=True, exist_ok=True)
        for item in sai_traj_src.glob("*.json"):
            shutil.copyfile(item, target_traj / item.name)

    # 官方参考轨迹：把缓存拷进站点（站点自包含，不依赖外部与缓存目录）
    sai_frames_src = REPO_ROOT / "demo" / ".cache" / "sai_frames"
    sai_frame_count = 0
    if sai_frames_src.is_dir():
        for task_dir in sorted(p for p in sai_frames_src.iterdir() if p.is_dir()):
            target_dir = out_dir / "assets" / "sai_frames" / task_dir.name
            target_dir.mkdir(parents=True, exist_ok=True)
            for frame in task_dir.glob("*.jpg"):
                shutil.copyfile(frame, target_dir / frame.name)
                sai_frame_count += 1

    price = (overview["ours"] or {}).get("price")
    episodes = build_episodes(out_dir, roots, subset_tasks, price, manifest)

    index_path = out_dir / "index.html"
    inline_json(index_path, "data-overview", overview)
    inline_json(index_path, "data-compare", compare)

    size = sum(path.stat().st_size for path in out_dir.rglob("*") if path.is_file())
    print(f"[build] 站点：{out_dir}")
    print(f"[build] 逐题行 {len(rows)} 题 · 对比子集 {len(subset_tasks)} 题（逐题数据 {episodes} 份）")
    print(f"[build] 官方参考帧 {sai_frame_count} 张")
    print(f"[build] 体积 {size / 1e6:.1f} MB")
    print(f"[build] 官方数据快照 {overview['official'].get('snapshot')} · "
          f"价格表 {(overview['ours'].get('price') or {}).get('snapshot')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
