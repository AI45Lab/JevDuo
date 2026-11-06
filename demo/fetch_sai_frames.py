#!/usr/bin/env python3
"""抓取官方 Sai 参考 run 的**帧**（用于与我们的轨迹并排对照）。

官方参考 run 的媒体包 `media/<run>/<task>.tar` 里混着逐步 PNG 和一段 mp4 录屏
（12 题合计 1.15GB，其中绝大部分是 mp4）。本脚本：

1. **流式**下载 tar（不落整包到磁盘，边下边解）；
2. 只取 PNG（跳过 mp4 与其它）；
3. 缩放成站点用的 JPEG（960 宽，q72），写进 `demo/.cache/sai_frames/<task>/`；
4. 把「帧文件名 ↔ 步骤」的对应关系记进 `sai_traj/<task>.json`（站点据此把
   我们的第 N 帧和 Sai 的第 N 帧并排）。

缓存目录 `.cache/` 不进 git（见 `.gitignore`）；构建时由 `demo/build_site.py`
拷进站点，站点因此自包含。

用法：
    python3 demo/fetch_sai_frames.py                    # 抓对比子集 12 题
    python3 demo/fetch_sai_frames.py --tasks 001,004     # 只抓指定题
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from demo.frames import Image  # noqa: E402

CACHE_DIR = REPO_ROOT / "demo" / ".cache" / "sai_frames"
SAI_REPO = "https://huggingface.co/datasets/simular-ai/sai-osworld-v21-reference-runs/resolve/main"
USER_AGENT = "jevduo-demo/0.1 (+frame fetch for trajectory comparison)"

FRAME_WIDTH = 960
FRAME_QUALITY = 72


def load_index() -> dict[str, list[dict[str, Any]]]:
    index_path = REPO_ROOT / "demo" / "data" / "official" / "sai_tasks.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    return payload.get("tasks") or {}


def subset_tasks() -> list[str]:
    subset_path = REPO_ROOT / "demo" / "data" / "official" / "compare_subset.json"
    subset = json.loads(subset_path.read_text(encoding="utf-8"))
    return sorted({task for tasks in (subset.get("groups") or {}).values() for task in tasks})


def extract_task(task: str, run: str, media_tar: str) -> dict[str, Any]:
    """流式下载并解出一个任务的帧。返回 {帧名: 站点内文件名}。"""
    url = f"{SAI_REPO}/{media_tar}"
    target_dir = CACHE_DIR / task
    target_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    mapping: dict[str, str] = {}
    with urllib.request.urlopen(request, timeout=180) as response:
        with tarfile.open(fileobj=response, mode="r|") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(".png"):
                    continue
                name = Path(member.name).name
                if name in mapping:
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                out_name = name.rsplit(".", 1)[0] + ".jpg"
                out_path = target_dir / out_name
                if out_path.exists():
                    mapping[name] = out_name
                    continue
                data = extracted.read()
                if Image is not None:
                    try:
                        import io

                        with Image.open(io.BytesIO(data)) as image:
                            if image.width > FRAME_WIDTH:
                                height = round(image.height * FRAME_WIDTH / image.width)
                                image = image.resize((FRAME_WIDTH, height), Image.LANCZOS)
                            image.convert("RGB").save(out_path, "JPEG", quality=FRAME_QUALITY, optimize=True)
                        mapping[name] = out_name
                        continue
                    except OSError:
                        pass
                out_path.with_suffix(".png").write_bytes(data)
                mapping[name] = name
    return mapping


def reattach(task: str) -> int:
    """只用缓存目录重建帧映射（不重新下载 tar）。"""
    task_dir = CACHE_DIR / task
    if not task_dir.is_dir():
        return 0
    mapping = {}
    for frame in sorted(task_dir.iterdir()):
        if frame.suffix == ".jpg":
            mapping[frame.stem + ".png"] = frame.name
        elif frame.suffix == ".png":
            mapping[frame.name] = frame.name
    if not mapping:
        return 0
    traj_path = REPO_ROOT / "demo" / "data" / "official" / "sai_traj" / f"{task}.json"
    if not traj_path.is_file():
        return 0
    payload = json.loads(traj_path.read_text(encoding="utf-8"))
    payload["frames"] = mapping
    payload["frames_dir"] = f"assets/sai_frames/{task}"
    traj_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(mapping)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取官方 Sai 参考 run 的帧")
    parser.add_argument("--tasks", default="", help="逗号分隔题号；默认取对比子集")
    parser.add_argument("--reattach", action="store_true",
                        help="不下载，只用缓存目录重建帧映射（同步脚本覆盖轨迹后用）")
    args = parser.parse_args(argv)

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()] or subset_tasks()
    index = load_index()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        if args.reattach:
            count = reattach(task)
            print(f"[frames] {task}: 重挂映射 {count} 帧")
            continue
        info = index.get(task) or {}
        run = info.get("run")
        media_tar = info.get("media_tar")
        if not run or not media_tar:
            print(f"[frames] 跳过 {task}：Sai 没有可用的干净运行")
            continue
        started = time.time()
        try:
            mapping = extract_task(task, run, media_tar)
        except (urllib.error.URLError, TimeoutError, tarfile.TarError) as error:
            print(f"[frames] {task} 失败：{error}")
            continue
        # 把帧映射写回轨迹摘要，站点据此并排对照
        traj_path = REPO_ROOT / "demo" / "data" / "official" / "sai_traj" / f"{task}.json"
        payload = json.loads(traj_path.read_text(encoding="utf-8")) if traj_path.is_file() else {}
        payload["frames"] = mapping
        payload["frames_dir"] = f"assets/sai_frames/{task}"
        traj_path.parent.mkdir(parents=True, exist_ok=True)
        traj_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        size = sum(path.stat().st_size for path in (CACHE_DIR / task).glob("*"))
        print(f"[frames] {task}: {len(mapping)} 帧 · {size / 1e6:.1f} MB · {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
