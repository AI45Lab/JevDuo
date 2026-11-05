"""批量汇总器：多 run 聚合、重复题去重、环境失败分类、Markdown 渲染。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "batch_summary.py"
_spec = importlib.util.spec_from_file_location("batch_summary", _TOOL)
batch_summary = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(batch_summary)


def _make_run(root: Path, name: str, episodes: dict[str, dict]) -> Path:
    run_dir = root / name
    for task, payload in episodes.items():
        episode_dir = run_dir / "episodes" / task
        episode_dir.mkdir(parents=True, exist_ok=True)
        (episode_dir / "episode.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        (episode_dir / "timing.json").write_text(
            json.dumps({"wall_seconds": payload.get("_wall", 60)}), encoding="utf-8"
        )
        (episode_dir / "llm_calls.jsonl").write_text(
            json.dumps({
                "role": "plan_step",
                "response": {"usage": {"prompt_tokens": 100, "completion_tokens": 10}},
            }) + "\n",
            encoding="utf-8",
        )
    return run_dir


def test_collect_rows_merges_duplicates_preferring_scored(tmp_path):
    """同一题在补跑片与被杀的片都有时，取已出分的那条。"""
    _make_run(tmp_path, "batch-s1", {
        "004": {"task_id": "004", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "TimeoutError",
                "error": "VM failed to become ready within timeout period", "steps": 0},
    })
    _make_run(tmp_path, "batch-retry", {
        "004": {"task_id": "004", "status": "scored", "score": 0.5,
                "stop_reason": "done", "steps": 7},
    })
    rows = batch_summary.collect_rows(tmp_path, "batch")
    assert len(rows) == 1
    assert rows[0]["status"] == "scored"
    assert rows[0]["score"] == 0.5


def test_summarize_classifies_env_versus_model_failures(tmp_path):
    _make_run(tmp_path, "batch", {
        "001": {"task_id": "001", "status": "scored", "score": 1.0, "stop_reason": "done", "steps": 5},
        "002": {"task_id": "002", "status": "scored", "score": 0.0,
                "stop_reason": "no_action_limit", "steps": 3},
        "003": {"task_id": "003", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "TimeoutError",
                "error": "VM failed to become ready within timeout period", "steps": 0},
        "004": {"task_id": "004", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "SlowModelSchemaError",
                "error": "slow model response is missing required fields", "steps": 0},
    })
    rows = batch_summary.collect_rows(tmp_path, "batch")
    stats = batch_summary.summarize(rows)
    assert stats["total"] == 4
    assert stats["scored"] == 2
    assert [row["task"] for row in stats["env_failed"]] == ["003"]
    assert [row["task"] for row in stats["model_failed"]] == ["004"]
    assert stats["full_hits"] == 1
    assert stats["zeros"] == 1
    assert stats["average_scored"] == 0.5
    assert stats["calls"] == 4
    assert stats["prompt_tokens"] == 400


def test_render_markdown_lists_rerun_tasks(tmp_path):
    _make_run(tmp_path, "batch", {
        "003": {"task_id": "003", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "TimeoutError",
                "error": "VM failed to become ready within timeout period", "steps": 0},
    })
    rows = batch_summary.collect_rows(tmp_path, "batch")
    report = batch_summary.render_markdown(rows, batch_summary.summarize(rows), "T")
    assert "未出分的执行侧失败" in report
    assert "TASKS=003" in report
    assert "003" in report


def test_collect_rows_ignores_unrelated_prefix(tmp_path):
    _make_run(tmp_path, "other-batch", {
        "001": {"task_id": "001", "status": "scored", "score": 1.0, "stop_reason": "done", "steps": 1},
    })
    assert batch_summary.collect_rows(tmp_path, "batch") == []


def test_classify_task_covers_every_failure_mode():
    """每种结束方式都要落到一个标签，方便按题定位 harness 模块。"""
    cases = {
        "env_failure": {"status": "failed_unscored", "error": "VM failed to become ready",
                         "exception": "TimeoutError"},
        "model_schema": {"status": "failed_unscored", "exception": "SlowModelSchemaError",
                         "error": "slow model response is missing required fields"},
        "infeasible": {"status": "failed_unscored", "error": "", "exception": None,
                       "stop": "BudgetWise is a personal-finance website, not a wireless carrier"},
        "no_action": {"status": "scored", "score": 0.0, "stop": "no_action_limit"},
        "wall_limit": {"status": "scored", "score": 0.0, "stop": "wall_time_limit"},
        "max_steps": {"status": "scored", "score": 0.0, "stop": "max_steps"},
        "done_full": {"status": "scored", "score": 1.0, "stop": "done"},
        "done_partial": {"status": "scored", "score": 0.4, "stop": "done"},
        "done_zero": {"status": "scored", "score": 0.0, "stop": "done"},
    }
    for expected, payload in cases.items():
        assert batch_summary.classify_task(payload) == expected, expected
    # 未出分但停止原因是 max_steps：仍归到 max_steps，不落到 done_zero
    assert batch_summary.classify_task(
        {"status": "failed_unscored", "score": None, "stop": "max_steps"}) == "max_steps"


def test_render_markdown_groups_tasks_by_failure_mode(tmp_path):
    _make_run(tmp_path, "batch", {
        "001": {"task_id": "001", "status": "scored", "score": 1.0, "stop_reason": "done", "steps": 3},
        "002": {"task_id": "002", "status": "scored", "score": 0.0,
                "stop_reason": "no_action_limit", "steps": 2},
    })
    rows = batch_summary.collect_rows(tmp_path, "batch")
    report = batch_summary.render_markdown(rows, batch_summary.summarize(rows), "T")
    assert "失败模式分布" in report
    assert "done_full" in report and "no_action" in report


def test_duplicate_task_prefers_newer_run_on_tie(tmp_path):
    """补跑片与原片同题同分时取更新的那次（旧错误不应掩盖新结果）。"""
    _make_run(tmp_path, "batch-20261024-000000-s1", {
        "075": {"task_id": "075", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "ValueError",
                "error": "No API key found for provider 'openai'", "steps": 0},
    })
    _make_run(tmp_path, "batch-20261025-023704-s1", {
        "075": {"task_id": "075", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "BadRequestError",
                "error": "media exceeds size limit: max 10485760 bytes", "steps": 0},
    })
    rows = batch_summary.collect_rows(tmp_path, "batch")
    assert len(rows) == 1
    # 补跑批次时间戳更晚 → 应被选中（旧的缺凭据错误已被取代）
    assert "media exceeds size limit" in rows[0]["error"]
    assert batch_summary.run_timestamp("batch-20261025-023704-s1") > \
        batch_summary.run_timestamp("batch-20261024-000000-s1")


def test_steps_fall_back_to_events_when_episode_json_says_zero(tmp_path):
    """异常路径下 episode.json 的 steps=0，用事件流回补真实步数。"""
    run = _make_run(tmp_path, "batch", {
        "071": {"task_id": "071", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "HTTPError",
                "error": "HTTP Error 422", "steps": 0},
    })
    events = run / "episodes" / "071" / "events.jsonl"
    events.write_text(
        "\n".join(json.dumps({"payload": {"step": i}}) for i in range(40)) + "\n",
        encoding="utf-8")
    rows = batch_summary.collect_rows(tmp_path, "batch")
    assert rows[0]["steps"] == 39


def test_classify_task_detects_judge_infra_failure():
    """judge 媒体上限属于评估基础设施（重跑无望），不是模型能力问题。"""
    row = {"status": "failed_unscored", "score": None, "stop": "exception",
           "exception": "BadRequestError",
           "error": "Error code: 400 - media exceeds size limit: max 10485760 bytes"}
    assert batch_summary.classify_task(row) == "judge_infra"
    assert batch_summary.is_judge_infra_failure(row)


def test_collect_rows_accepts_multiple_prefixes(tmp_path):
    """补跑片前缀常与主批次不同：逗号分隔多前缀要能一起收（取最新批次那次）。"""
    _make_run(tmp_path, "batch-a", {
        "001": {"task_id": "001", "status": "failed_unscored", "score": None,
                "stop_reason": "exception", "exception_type": "HTTPError",
                "error": "HTTP Error 429", "steps": 0},
    })
    _make_run(tmp_path, "batch-b-retry", {
        "001": {"task_id": "001", "status": "scored", "score": 1.0,
                "stop_reason": "done", "steps": 31},
    })
    rows = batch_summary.collect_rows(tmp_path, "batch-a,batch-b-retry")
    assert len(rows) == 1
    assert rows[0]["score"] == 1.0


def test_noop_rate_counts_every_decision_without_an_executable_action(tmp_path):
    """空转口径：看"动作有没有编译出来"，不是"winner 是不是 return_to_planner"。

    真机路径下 grounder 是关的（`jevduo_eval/entry.py` 显式传 `grounder=None`），
    所以 visual_fallback 与 return_to_planner 一样编译不出动作，两者都算真空转。
    """
    episode_dir = tmp_path / "episodes" / "004"
    episode_dir.mkdir(parents=True)
    events = [
        {"route": "gui", "winner": {"operation": "click"}, "action": {"kind": "gui"}},
        {"route": "gui", "winner": {"operation": "visual_fallback"}, "action": None},
        {"route": "gui", "winner": {"operation": "return_to_planner"}, "action": None},
        {"route": "planner", "plan": {"done": False}},
    ]
    (episode_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
        encoding="utf-8",
    )
    assert batch_summary._noop_rate(episode_dir) == 2 / 3
