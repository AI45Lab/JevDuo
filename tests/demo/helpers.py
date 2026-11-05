"""demo 单测的合成结果目录构造器。

造的是符合 `docs/RESULT_DIR_CONTRACT.md` v0.2 的 run 目录，不依赖
`rjob_logs/` 里的真实 run（CI 上没有），也不 import `jevduo` /
`jevduo_eval`——demo 与 harness 完全解耦，这条边界本身就是被测项之一。
"""

from __future__ import annotations

import json
from pathlib import Path


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def usage(prompt: int, completion: int, reasoning: int = 0) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def scored(task_id: str, score: float, steps: int = 1, wall: float = 10.0) -> dict:
    """已出分的任务定义。"""
    return {"task_id": task_id, "score": score, "status": "scored", "steps": steps, "wall": wall}


def make_run(root: Path, run_id: str, tasks: list[dict]) -> Path:
    """造一个符合契约 v0.2 的 run 目录。

    tasks 每项：{"task_id", "domain", "score", "status", "steps", "wall"}。
    """
    run_dir = root / run_id
    write_json(
        run_dir / "code_version.json",
        {
            "harness_version": "0.1.0",
            "protocol_version": "0.1",
            "git_describe": "abc1234",
            "images": {"inner": "img:inner"},
            "run_spec": {
                "budget": {"max_steps": 30, "max_wall_seconds": 1800.0},
                "harness": {"name": "tandem", "mode": "planner_led",
                            "models": {"slow": "kimi-k3", "fast": "kev"}},
                "harness_config": {"fast_backend": "kev"},
            },
        },
    )
    write_json(
        run_dir / "summary.json",
        {
            "run_id": run_id,
            "benchmark": "osworld2",
            "total": len(tasks),
            "scored": sum(1 for t in tasks if t["status"] == "scored"),
            "failed_unscored": sum(1 for t in tasks if t["status"] != "scored"),
            "average_score": None,
        },
    )
    entries = []
    for task in tasks:
        task_id = task["task_id"]
        domain = task.get("domain", "tasks")
        episode_dir = run_dir / "episodes" / f"{domain}-{task_id}"
        write_json(episode_dir / "task.json", {"id": task_id, "instruction": "do it", "snapshot": "chrome"})
        write_json(
            episode_dir / "episode.json",
            {
                "task_id": task_id,
                "harness": "tandem 0.1.0",
                "stop_reason": task.get("stop_reason", "done"),
                "steps": task["steps"],
                "status": task["status"],
                "score": task.get("score"),
                "exception_type": task.get("exception_type"),
            },
        )
        write_json(episode_dir / "timing.json", {"wall_seconds": task["wall"], "status": task["status"]})
        if task.get("score") is not None:
            write_json(
                episode_dir / "evaluation.json",
                {"source": "official_benchmark_evaluator", "raw": task["score"], "score": task["score"]},
            )
        steps = task["steps"]
        events = []
        for index in range(steps):
            events.append({"step": index, "route": "planner", "plan": {"current_goal": f"goal {index}"}})
            events.append(
                {
                    "step": index,
                    "route": "gui",
                    "plan": {"current_goal": f"goal {index}"},
                    "candidates": [{"id": "click:a11y:t0:0", "operation": "click", "description": "d"}],
                    "ranking": {
                        "winner": {"id": "click:a11y:t0:0", "operation": "click"},
                        "operation_ranked": [{"operation": "click", "score": 0.9}],
                        "action_ranked": [{"id": "click:a11y:t0:0", "score": 0.8}],
                        "operation_margin": 0.5,
                        "latency_ms": 120.0,
                        "score_type": "fast_model_choice_softmax",
                        "score_is_calibrated_correctness_probability": False,
                    },
                    "action": {"kind": "gui", "action_type": "click", "parameters": {"x": 1, "y": 2}},
                }
            )
            events.append({"step": index, "kind": "action", "action": {"kind": "gui"}})
            # 验证事件落在下一步骤（契约实测语义）
            events.append(
                {
                    "step": index + 1,
                    "route": "gui_verified",
                    "execution": {"returncode": 0, "stdout": "", "stderr": "", "kind": "gui"},
                    "verification": {"outcome": "achieved", "state_changed": True, "evidence": "ok"},
                }
            )
        write_jsonl(episode_dir / "events.jsonl", events)
        write_jsonl(
            episode_dir / "traj.jsonl",
            [
                {
                    "step_idx": index + 1,
                    "wall_time": 1000.0 + index,
                    "observation": {
                        "budget": {"steps_remaining": 30 - index},
                        "last_receipt": {"returncode": 0, "stdout": "", "stderr": "", "kind": "gui"},
                    },
                    "action": {"kind": "gui", "action_type": "click"},
                }
                for index in range(steps)
            ],
        )
        write_jsonl(
            episode_dir / "llm_calls.jsonl",
            [
                {
                    "role": "task_requirements",
                    "time": 1.0,
                    "attempt": 1,
                    "request": {"payload": {"messages": [{"role": "system", "content": "sys"}]}},
                    "response": {"usage": usage(10, 20, 5)},
                },
                {
                    "role": "plan_step",
                    "time": 2.0,
                    "attempt": 1,
                    "request": {"payload": {"messages": [{"role": "user", "content": "u"}]}},
                    "response": {"usage": usage(100, 200, 50)},
                },
            ],
        )
        shots = episode_dir / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        for index in range(steps):
            (shots / f"step_{index:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        entries.append({"task_id": task_id, "domain": domain, "episode_dir": f"{domain}-{task_id}"})
    write_json(
        run_dir / "episodes" / "manifest.resolved.json",
        {"run_id": run_id, "benchmark": "osworld2", "count": len(entries), "tasks": entries},
    )
    return run_dir


# ---------------------------------------------------------------------------
# 现行契约（episode.json / evaluation.json / events.jsonl / llm_calls.jsonl）
# 的合成 run：给 `demo.reader` 用。旧契约的 `make_run` 保留给老测试。
# ---------------------------------------------------------------------------


def make_contract_episode(
    run_dir: Path,
    *,
    task_dir: str = "tasks-004",
    score: float | None = 0.6556,
    stop: str = "done",
    steps: int = 3,
) -> Path:
    """造一个符合现行结果目录契约的 episode 目录。"""
    episode_dir = run_dir / "episodes" / task_dir
    write_json(
        episode_dir / "episode.json",
        {
            "task_id": "004",
            "harness": "tandem 0.1.0",
            "stop_reason": stop,
            "steps": steps,
            "status": "scored" if score is not None else "failed_unscored",
            "score": score,
            "exception_type": None if score is not None else "ValueError",
        },
    )
    write_json(
        episode_dir / "evaluation.json",
        {"source": "official_benchmark_evaluator", "raw": score, "score": score},
    )
    write_json(episode_dir / "timing.json", {"wall_seconds": 2944.5, "status": "scored"})
    write_json(
        episode_dir / "task.json",
        {"id": "004", "instruction": "Update only these new slides in the deck."},
    )
    write_jsonl(
        episode_dir / "events.jsonl",
        [
            {"route": "planner", "step": 0,
             "plan": {"stage_goal": "查样式", "current_goal": "看页脚",
                      "milestones": [
                          {"route": "gui", "current_goal": "看页脚"},
                          {"route": "gui", "current_goal": "打开字体面板"},
                          {"route": "gui", "current_goal": "改成目标字体"},
                      ]}},
            {
                "route": "gui",
                "step": 1,
                "candidates": [
                    {"id": "click:a11y:t0:1", "operation": "click", "source": "a11y",
                     "description": "Click the visible push button \"Font Name\"."},
                    {"id": "click:a11y:t0:2", "operation": "click", "source": "a11y",
                     "description": "Click the visible list item \"Calibri\"."},
                    {"id": "key:a11y:t0:3", "operation": "keypress", "source": "global",
                     "description": "Press ctrl+a."},
                    {"id": "wait", "operation": "wait", "source": "global",
                     "description": "Wait for the font list to finish rendering."},
                    {"id": "scroll:a11y:t0:4", "operation": "scroll", "source": "a11y",
                     "description": "Scroll the font list down."},
                ],
                "ranking": {"operation_margin": 0.31, "target_margin": 0.12, "latency_ms": 42.0,
                            "used_image": True,
                            "operation_ranked": [{"operation": "click", "score": 0.7},
                                                 {"operation": "wait", "score": 0.2}],
                            "action_ranked": [
                                {"id": "click:a11y:t0:1", "operation": "click", "score": 0.81},
                                {"id": "click:a11y:t0:2", "operation": "click", "score": 0.09},
                                {"id": "key:a11y:t0:3", "operation": "keypress", "score": 0.05},
                                {"id": "wait", "operation": "wait", "score": 0.03},
                                {"id": "scroll:a11y:t0:4", "operation": "scroll", "score": 0.02},
                            ]},
                "winner": {"id": "click:a11y:t0:1", "operation": "click",
                           "description": "Click the visible push button \"Font Name\"."},
                "action": {"operation": "click"},
            },
            {"route": "gui", "step": 2, "candidates": [], "winner": {"id": "x"}, "action": None},
            {
                "route": "gui_verified",
                "step": 3,
                "verification": {
                    "outcome": "not_achieved",
                    "evidence": "没变化",
                    "state_changed": False,
                    "requirement_checks": [
                        {"id": "font_name", "status": "failed", "evidence": "字体没改"}
                    ],
                },
            },
            {"route": "stage_replan", "step": 4, "reason": "execution_failed_or_unverified",
             "stage_goal": "重查样式", "milestone": 0},
            {"kind": "action", "step": 5, "action": {"operation": "click"}},
            {"route": "planner", "step": 6,
             "plan": {"stage_goal": "换条路重查样式", "current_goal": "用格式菜单改字体",
                      "milestones": [
                          {"route": "gui", "current_goal": "用格式菜单改字体"},
                          {"route": "gui", "current_goal": "核对最终字体名"},
                      ]}},
        ],
    )
    write_jsonl(
        episode_dir / "llm_calls.jsonl",
        [
            {"role": "plan_step", "attempt": 1, "time": 1.0,
             "request": {"messages": [{"content": "x" * 5000}]},
             "response": {"choices": [{"message": {"content": "{\"current_goal\": \"看页脚\"}"}}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                    "completion_tokens_details": {"reasoning_tokens": 20},
                                    "prompt_tokens_details": {"cached_tokens": 7}}}},
            {"role": "plan_step", "attempt": 2, "time": 2.0,
             "request": {"messages": []},
             "response": {"choices": [{"message": {"content": "第二段原文"}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}},
        ],
    )
    write_jsonl(
        episode_dir / "traj.jsonl",
        [
            {"step_idx": 1, "wall_time": 100.0,
             "observation": {
                 "screenshot": {"path": "/mnt/shared-storage-user/x/step_00.png",
                                "sha256": "ab" * 32, "width": 1920, "height": 1080},
                 "accessibility_tree": {"present": True, "node_count": 4948},
                 "budget": {"steps_remaining": 10, "wall_seconds_remaining": 100.0}},
             "action": {"operation": "click"}},
        ],
    )
    write_jsonl(episode_dir / "a11y_digests.jsonl",
                [{"step_idx": 1, "sha256": "cd" * 32, "node_count": 4948}])
    return episode_dir


def make_contract_run(root: Path, run_id: str = "osw21-run-1") -> Path:
    """造一个符合现行契约的 run 目录（osworld21 基准、stage 模式）。"""
    run_dir = root / run_id
    write_json(
        run_dir / "summary.json",
        {"run_id": run_id, "benchmark": "osworld21",
         "harness": {"name": "tandem", "mode": "stage",
                     "models": {"slow": "m3.1-flash", "fast": "jev"}},
         "total": 1, "scored": 1, "failed_unscored": 0, "average_score": 0.6556, "tasks": []},
    )
    write_json(
        run_dir / "code_version.json",
        {"git_describe": "unknown(experiment)", "images": {"inner": "weavebench@sha256:0e6497a9"}},
    )
    make_contract_episode(run_dir)
    return run_dir
