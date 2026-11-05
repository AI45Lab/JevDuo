"""M2 门禁：mock 全链路（spec → entry → runner → recorder → 结果目录 → 渲染 → worker.sh）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jevduo_eval.entry import entry_main, run_from_spec
from jevduo_eval.env import MockEnv
from jevduo_eval.manifest import TaskRef
from jevduo_eval.runner import run

from ..harness.conftest import (
    FixedSelector,
    ScriptedPlanner,
    ScriptedVerifier,
    make_components,
    valid_verifier_response,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import render_summary  # noqa: E402


def _spec(tmp_path: Path, **overrides) -> dict:
    base = {
        "run_id": "mock-001",
        "repo_root": str(Path(__file__).resolve().parents[2]),
        "output_root": str(tmp_path / "run"),
        "benchmark": "mock",
        "harness": {"name": "tandem", "mode": "planner_led"},
        "budget": {"max_steps": 5, "max_wall_seconds": 60},
        "record": {"a11y_full": True},
        "requirements": [],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("mode", ["step", "stage", "goal"])
def test_full_mock_chain_produces_renderable_result_dir(tmp_path, mode):
    episodes = run_from_spec(_spec(tmp_path, harness_config={"execution_mode": mode},
                                   requirements=None if mode == "goal" else []))
    assert episodes and episodes[0]["status"] == "scored"
    run_root = tmp_path / "run"
    episode_dir = run_root / "episodes" / "mock-mock-000"

    # 结果目录契约（docs/RESULT_DIR_CONTRACT.md）
    for name in ("task.json", "traj.jsonl", "events.jsonl", "llm_calls.jsonl",
                 "episode.json", "evaluation.json", "result.txt", "timing.json",
                 "artifacts.json", "a11y_digests.jsonl", "code_version.json",
                 "summary.json", "heartbeat.json"):
        assert (run_root / name).is_file() or (episode_dir / name).is_file(), f"缺 {name}"
    assert (episode_dir / "a11y" / "step_00.xml").is_file()  # a11y_full=True
    assert (episode_dir / "screenshots" / "step_00.png").is_file()

    traj = [json.loads(line) for line in (episode_dir / "traj.jsonl").read_text().splitlines()]
    assert traj and traj[0]["protocol_version"] == "0.2"
    assert traj[0]["action"]["kind"] == "gui"
    episode = json.loads((episode_dir / "episode.json").read_text())
    assert episode["stop_reason"] == "done" and episode["score"] == 1.0
    summary = json.loads((run_root / "summary.json").read_text())
    assert summary["scored"] == 1 and summary["average_score"] == 1.0
    if mode == "goal":
        memory = json.loads((episode_dir / "task_memory.json").read_text())
        assert memory["work_items"][0]["status"] == "done"
        events = [json.loads(line) for line in (episode_dir / "events.jsonl").read_text().splitlines()]
        assert any(e.get("route") == "stage_verified" for e in events)
        assert any(e.get("route") == "observation_refresh" for e in events)

    # 渲染（demo 占位）可离线打开
    page = render_summary.render(run_root)
    assert "mock-000" in page and "1.0" in page


def test_failed_task_marks_unscored_and_run_continues(tmp_path):
    class FlakyEnv(MockEnv):
        def fetch_task(self, task_id: str, domain: str = "") -> dict:
            if task_id == "bad":
                raise RuntimeError("VM 起不来")
            return super().fetch_task(task_id, domain)

    spec = _spec(tmp_path)
    env = FlakyEnv(base_dir=tmp_path / "run" / "mock_env")
    from jevduo.config import SessionConfig
    from jevduo.runtime.session import JevDuoSession

    planner = ScriptedPlanner([{"done": True, "task_status": "completed",
                                "current_goal": "g", "expected_observation": ""}])
    session = JevDuoSession(make_components(planner, ScriptedVerifier(
        [valid_verifier_response(task_status="completed")]), FixedSelector()),
        config=SessionConfig())
    episodes = run(spec, env=env, session=session,
                   tasks=[TaskRef("good", "mock"), TaskRef("bad", "mock")],
                   run_root=tmp_path / "run")
    assert [item["status"] for item in episodes] == ["scored", "failed_unscored"]
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert summary["failed_unscored"] == 1 and summary["average_score"] == 1.0


def test_agent_budget_exhausted_inside_model_still_reaches_official_scoring(tmp_path):
    from jevduo.config import SessionConfig
    from jevduo.models.slow import TaskBudgetExceeded
    from jevduo.runtime.session import JevDuoSession

    def planner(*args, **kwargs):
        raise TaskBudgetExceeded("Agent task budget exhausted")

    components = make_components(planner, lambda *a, **kw: None, FixedSelector())
    root = tmp_path / "run"
    episodes = run(_spec(tmp_path), env=MockEnv(base_dir=root / "mock_env"),
                   session=JevDuoSession(components, config=SessionConfig()),
                   tasks=[TaskRef("one", "mock")], run_root=root)
    assert episodes[0]["stop_reason"] == "wall_time_limit"
    assert episodes[0]["status"] == "scored"
    assert episodes[0]["score"] is not None


def test_requirement_timeout_has_a_deadline_and_flushes_summary_before_next_task(tmp_path):
    from jevduo.config import SessionConfig
    from jevduo.runtime.session import JevDuoSession

    deadlines = []
    root = tmp_path / "run"

    class Env(MockEnv):
        def fetch_task(self, task_id, domain=""):
            if task_id == "second":
                assert json.loads((root / "summary.json").read_text())["failed_unscored"] == 1
            return super().fetch_task(task_id, domain)

    def requirements(instruction):
        assert isinstance(deadlines[-1], float)
        raise TimeoutError("Task deadline exhausted during requirements")

    components = make_components(lambda *a, **kw: None, lambda *a, **kw: None, FixedSelector())
    components.requirements = requirements
    components.set_deadline = deadlines.append
    episodes = run(_spec(tmp_path, requirements=None), env=Env(base_dir=root / "mock_env"),
                   session=JevDuoSession(components, config=SessionConfig()),
                   tasks=[TaskRef("first"), TaskRef("second")], run_root=root)
    assert [item["status"] for item in episodes] == ["failed_unscored", "failed_unscored"]
    assert deadlines[1] is None and deadlines[3] is None


def test_entry_cli_from_spec_file(tmp_path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(tmp_path), ensure_ascii=False), encoding="utf-8")
    assert entry_main(["--spec", str(spec_path)]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"done": 1, "scored": 1}


def test_worker_sh_mock_mode_end_to_end(tmp_path):
    """infra/worker.sh（rjob worker 内入口）在 mock 基准下真实可执行。"""
    repo_root = Path(__file__).resolve().parents[2]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(tmp_path), ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo_root / "infra" / "worker.sh"), str(spec_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "run" / "worker" / "entry.log").is_file()
    assert json.loads((tmp_path / "run" / "summary.json").read_text())["scored"] == 1


def test_obs_stage_cleaned_after_episode(tmp_path):
    """episode 结束后 obs_stage 任务目录应被清理（截图正本在 episode 目录）。"""
    run_from_spec(_spec(tmp_path))
    staging = tmp_path / "run" / "obs_stage"
    assert not staging.exists() or not any(staging.iterdir())
    # 契约正本仍在
    assert (tmp_path / "run" / "episodes" / "mock-mock-000" / "screenshots").is_dir()


def test_session_events_drained_to_events_jsonl(tmp_path):
    """planner/JEV 排序/验证事件必须落盘（session.events 曾只在内存，run 后丢失）。"""
    run_from_spec(_spec(tmp_path))
    import glob
    events_file = glob.glob(str(tmp_path / "run" / "episodes" / "*" / "events.jsonl"))[0]
    events = [json.loads(line) for line in open(events_file)]
    routes = [e.get("route") or e.get("kind") for e in events]
    assert "planner" in routes
    gui_events = [e for e in events if e.get("route") == "gui"]
    assert gui_events and "ranking" in gui_events[0]
    ranking = gui_events[0]["ranking"]
    assert "operation_request" in ranking and "action_ranked" in ranking
    assert "winner" in ranking and ranking["winner"] is not None
    assert "terminal" in routes


def test_events_not_duplicated_across_steps(tmp_path):
    """排水游标必须推进：否则每次 act 重写全部历史事件（kev-5 的 45 条 planner 事件实为 ~9 次）。"""
    run_from_spec(_spec(tmp_path))
    import glob
    events_file = glob.glob(str(tmp_path / "run" / "episodes" / "*" / "events.jsonl"))[0]
    events = [json.loads(line) for line in open(events_file)]
    planner_events = [e for e in events if e.get("route") == "planner"]
    # mock 链路：第一题 1 次 planner（click）+ 1 次（done）= 2；重复排水会得到 3+
    assert len(planner_events) == 2, [e for e in planner_events]
