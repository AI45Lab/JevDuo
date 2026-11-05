"""demo 读取层：结果目录 → 站点数据契约。

**边界**（见 `demo/README.md`）：不 import `jevduo` / `jevduo_eval`、不调模型、不碰 VM。
唯一输入是 `docs/RESULT_DIR_CONTRACT.md` 描述的结果目录契约。

**为什么拆成包**：老 `demo/adapter.py` 一个模块同时干了"读目录 / 算指标 / 拼 HTML"，
三件事互相牵制（改口径就得动 HTML）。这里按**数据流**拆：

    runs.py      发现 run、读 run 级元数据
    episodes.py  读题级终态（分数 / 停止原因 / 评测来源 / 用时）
    events.py    流式解析 events.jsonl → 步骤（plan / 决策 / 核验 / replan）
    llm.py       流式解析 llm_calls.jsonl → 只取 usage 与原文（丢弃 base64 截图）
    traj.py      流式解析 traj.jsonl / a11y_digests.jsonl → 帧索引
    paths.py     题号归一 + 集群路径脱敏
    viewmodel.py 把上面拼成站点唯一数据契约
    aggregate.py 逐题行与汇总（委托 `tools/batch_summary.py`，口径不分叉）

**硬约束**：`llm_calls.jsonl` 单题 64MB、整批 39GB —— 必须逐行读，
禁止 `read()` / `readlines()`（见 `llm.MAX_LINE_BYTES` 与 `tests/demo/test_reader.py`）。
"""

from __future__ import annotations

from .aggregate import build_overview, collect_rows, compare_rows, load_official
from .episodes import EpisodeRef, list_episodes, read_episode_meta
from .events import build_steps, noop_stats, rank_stats, read_events
from .llm import read_llm_texts, read_llm_usage
from .paths import normalize_task_id, scrub_paths
from .runs import RunInfo, discover_runs
from .traj import read_frames
from .viewmodel import build_view_model

__all__ = [
    "EpisodeRef",
    "RunInfo",
    "build_overview",
    "build_steps",
    "build_view_model",
    "collect_rows",
    "compare_rows",
    "discover_runs",
    "list_episodes",
    "load_official",
    "noop_stats",
    "normalize_task_id",
    "rank_stats",
    "read_episode_meta",
    "read_events",
    "read_frames",
    "read_llm_texts",
    "read_llm_usage",
    "scrub_paths",
]
