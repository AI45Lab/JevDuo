"""demo 读取层单测：合成结果目录 → view model。

不依赖 `rjob_logs/` 里的真实 run（CI 上没有），也不 import `jevduo` /
`jevduo_eval`——demo 与 harness 完全解耦，这条边界本身就是被测项之一。

**最要紧的一条**：`test_llm_reader_never_reads_whole_file` 用一个"只许逐行读"的
假文件把 `llm_calls.jsonl`（单题 64MB、整批 39GB）罩住——读取层一旦整文件读，
测试立刻炸。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from helpers import make_contract_episode as make_episode  # noqa: E402
from helpers import make_contract_run as make_run  # noqa: E402

from demo.reader import (  # noqa: E402
    build_steps,
    build_view_model,
    discover_runs,
    list_episodes,
    normalize_task_id,
    read_llm_usage,
    scrub_paths,
)
from demo.reader.events import noop_stats  # noqa: E402
from demo.reader.llm import read_llm_texts  # noqa: E402

# ---------------------------------------------------------------- 题号归一


def test_normalize_task_id():
    assert normalize_task_id("tasks-004") == "004"
    assert normalize_task_id("004") == "004"
    assert normalize_task_id("4") == "004"
    assert normalize_task_id("task_013") == "013"


# ---------------------------------------------------------------- run / 题发现


def test_discover_runs_reads_metadata(tmp_path):
    make_run(tmp_path)
    runs = discover_runs(tmp_path, benchmark="osworld21")
    assert len(runs) == 1
    run = runs[0]
    assert run.run_id == "osw21-run-1"
    assert run.benchmark == "osworld21"
    assert run.harness["mode"] == "stage"
    assert run.code_version == "unknown(experiment)"
    assert "sha256" in (run.inner_image or "")


def test_list_episodes_normalizes_task_and_keeps_unscored(tmp_path):
    run_dir = make_run(tmp_path)
    make_episode(run_dir, task_dir="tasks-025", score=None, stop="exception", steps=0)
    episodes = list_episodes("osw21-run-1", run_dir / "episodes")
    assert [e.task_id for e in episodes] == ["004", "025"]
    unscored = episodes[1]
    assert unscored.score is None
    assert unscored.status == "failed_unscored"
    assert unscored.exception_type == "ValueError"
    assert episodes[0].native_evaluator is True


# ---------------------------------------------------------------- 事件流


def test_build_steps_normalizes_kinds(tmp_path):
    episode_dir = make_episode(tmp_path)
    steps = build_steps(episode_dir)
    kinds = [s["kind"] for s in steps]
    # 末尾多一个 planner：重规划之后慢模型会重写计划（机制段要展示新计划）
    assert kinds == ["planner", "gui", "gui", "gui_verified", "stage_replan", "action", "planner"]
    assert steps[1]["n_candidates"] == 5
    assert steps[1]["ranking"]["latency_ms"] == 42.0
    assert steps[1]["ranking"]["action_ranked"][0]["id"] == "click:a11y:t0:1"
    assert steps[1]["noop"] is False
    assert steps[2]["noop"] is True
    assert steps[3]["verification"]["outcome"] == "not_achieved"
    assert steps[3]["verification"]["requirement_checks"][0]["id"] == "font_name"
    assert steps[4]["reason"] == "execution_failed_or_unverified"
    assert len(steps[0]["plan"]["milestones"]) == 3
    assert len(steps[6]["plan"]["milestones"]) == 2


def test_noop_rate_uses_action_null_caliber(tmp_path):
    """空转口径：gui 事件里 action 为 null（与 tools/batch_summary 一致）。"""
    episode_dir = make_episode(tmp_path)
    stats = noop_stats(episode_dir)
    assert stats["decisions"] == 2
    assert stats["noop"] == 1
    assert stats["noop_rate"] == 0.5


# ---------------------------------------------------------------- 慢模型调用


class _NoReadFile:
    """只许逐行读的假文件：调用 read()/readlines() 直接炸。"""

    def __init__(self, path: Path, **kwargs):
        # 用 io.open 直开，绕开被 monkeypatch 掉的 Path.open（否则自我递归）
        self._handle = open(path, encoding="utf-8", errors="replace")

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._handle)

    def read(self, *args):
        raise AssertionError("读取层不允许整文件读 llm_calls.jsonl")

    def readlines(self, *args):
        raise AssertionError("读取层不允许整文件读 llm_calls.jsonl")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False


def test_llm_reader_never_reads_whole_file(monkeypatch, tmp_path):
    episode_dir = make_episode(tmp_path)
    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self.name == "llm_calls.jsonl":
            return _NoReadFile(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    usage = read_llm_usage(episode_dir)
    assert usage["total"]["calls"] == 2
    assert usage["total"]["prompt_tokens"] == 110
    assert usage["total"]["completion_tokens"] == 55
    assert usage["total"]["reasoning_tokens"] == 20
    assert usage["total"]["cached_tokens"] == 7


def test_llm_texts_are_transported_not_generated(tmp_path):
    episode_dir = make_episode(tmp_path)
    texts = read_llm_texts(episode_dir)
    assert texts[0]["text"] == '{"current_goal": "看页脚"}'
    assert texts[1]["text"] == "第二段原文"
    assert all("truncated" in item for item in texts)


# ---------------------------------------------------------------- view model


def test_build_view_model_shape(tmp_path):
    episode_dir = make_episode(tmp_path)
    episodes = list_episodes("osw21-run-1", episode_dir.parent)
    model = build_view_model(episodes[0], price={"input_per_1m": 0.15, "output_per_1m": 0.6,
                                                 "cached_per_1m": 0.015, "snapshot": "2026-10-23"})
    assert model["task_id"] == "004"
    assert model["score"] == 0.6556
    assert model["evaluator"]["native"] is True
    assert model["tokens"]["prompt"] == 110
    assert model["tokens"]["usd_estimate"] > 0
    assert model["tokens"]["snapshot"] == "2026-10-23"
    assert model["noop_rate"] == 0.5
    assert model["fast_model"]["rank_calls"] == 1  # 没有 ranking 的决策不算排序调用
    assert model["replans"][0]["reason"] == "execution_failed_or_unverified"
    assert model["requirements"][0]["id"] == "font_name"
    assert model["frames"][0]["node_count"] == 4948
    # 集群路径必须已脱敏
    assert "/mnt/shared-storage-user" not in json.dumps(model, ensure_ascii=False)


def test_build_view_model_without_price_has_no_usd(tmp_path):
    """没有价格表就不出美元数（红线 3）。"""
    episode_dir = make_episode(tmp_path)
    episodes = list_episodes("osw21-run-1", episode_dir.parent)
    model = build_view_model(episodes[0], price=None)
    assert model["tokens"]["usd_estimate"] is None
    assert model["tokens"]["snapshot"] is None


def test_scrub_paths_keeps_vm_paths():
    value = {"p": "/mnt/shared-storage-user/chenxinquan/x/episode.json",
             "vm": "/home/user/Desktop/a.pptx"}
    scrubbed = scrub_paths(value)
    assert scrubbed["p"].startswith("<cluster-path>/")
    assert scrubbed["vm"] == "/home/user/Desktop/a.pptx"


@pytest.mark.parametrize("raw", ["tasks-004", "004"])
def test_episode_dir_variants_both_normalize(tmp_path, raw):
    episode_dir = make_episode(tmp_path, task_dir=raw)
    episodes = list_episodes("run", episode_dir.parent)
    assert [e.task_id for e in episodes] == ["004"]
