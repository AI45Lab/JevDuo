"""runner：任务主循环。驱动 TandemSession 与环境交换协议消息，落盘与评分。"""

from __future__ import annotations

import shutil
import time
from dataclasses import replace
from pathlib import Path

from tandem.models.slow import TaskBudgetExceeded
from tandem.protocol.models import Observation
from tandem.runtime.goal_stage import ObservationRequired
from tandem.runtime.session import TandemSession

from .env import budget_of
from .manifest import TaskRef, write_resolved
from .recorder import EpisodeRecorder, write_heartbeat, write_summary
from .scoring import episode_status, official_score

TERMINAL_KINDS = {"finish", "fail"}


def run_episode(env, session: TandemSession, recorder: EpisodeRecorder, task: dict,
                *, max_steps: int, max_wall_seconds: float,
                extra_exports: list[str] | None = None, started_monotonic: float | None = None) -> dict:
    """单题循环：obs → session.act → env.execute → 回执灌回，终态导出交付物并评分。"""
    started = time.perf_counter() if started_monotonic is None else started_monotonic
    steps_used = 0
    stop_reason = "max_steps"

    def environment_call(phase, function, *args, **kwargs):
        before = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            recorder.event({"route": "environment_timing", "phase": phase,
                            "seconds": time.perf_counter() - before})

    observation: Observation = environment_call("setup", env.reset, task)
    outputs: list[dict] = []
    events_start = len(session.events)  # 本题事件起点：act 后增量落盘（planner/快模型/验证）

    def drain_events() -> None:
        nonlocal events_start
        for event in session.events[events_start:]:
            recorder.event(event)
        events_start = len(session.events)
        if session.goal is not None:
            recorder.task_memory(session.goal.memory.snapshot())

    while True:
        budget = budget_of(
            max_steps=max_steps,
            max_wall_seconds=max_wall_seconds,
            steps_used=steps_used,
            started_monotonic=started,
            now_monotonic=time.perf_counter(),
        )
        if budget.steps_remaining <= 0:
            stop_reason = "max_steps"
            break
        if budget.wall_seconds_remaining <= 0:
            stop_reason = "wall_time_limit"
            break
        observation = replace(observation, budget=budget)
        try:
            action = session.act(observation)
        except ObservationRequired as refresh:
            recorder.event({"route": "observation_refresh", "step": steps_used,
                            "reason": str(refresh), "action_replayed": False})
            observation = environment_call("observe", env.observe, task, budget=budget, previous=observation)
            continue
        except TaskBudgetExceeded as exhausted:
            stop_reason = "wall_time_limit"
            recorder.event({"route": "task_budget_exhausted", "step": steps_used,
                            "reason": str(exhausted), "next": "official_evaluation"})
            break
        finally:
            drain_events()
        recorder.event({"step": steps_used, "kind": "action", "action": action.to_dict()})
        if getattr(action, "kind", "") in TERMINAL_KINDS:
            stop_reason = "done" if action.kind == "finish" else action.reason
            # 终态仍须送达 VM（DONE/FAIL 触发评分钩子，旧 runtime 同款）
            environment_call("execute", env.execute, action, budget=budget, task=task)
            if action.kind == "finish":
                outputs = environment_call(
                    "export", env.export_outputs,
                    list(action.outputs) + list(extra_exports or []), recorder.dir
                )
            break
        receipt, observation = environment_call("execute", env.execute, action, budget=budget, task=task)
        recorder.traj(observation, action, receipt)
        # 环境提供素材属性时写入 episode 目录（真实环境 M3 同样暴露 last_* 属性）
        shot = getattr(env, "last_screenshot", None)
        if shot is not None:
            recorder.screenshot(steps_used, shot)
        a11y_text = getattr(env, "last_a11y", None)
        if a11y_text:
            recorder.a11y(steps_used, a11y_text)
        steps_used += 1
    raw = environment_call("evaluate", env.score, task)
    if session.goal is not None:
        recorder.task_memory(session.goal.memory.snapshot())
    score = official_score(raw) if raw is not None else None
    return recorder.finalize(
        stop_reason=stop_reason,
        outputs=outputs,
        raw_score=raw,
        score=score,
        status=episode_status(scored=score is not None, exception=None),
        steps=steps_used,
    )


def run(spec: dict, *, env, session: TandemSession, tasks: list[TaskRef],
        run_root: Path, llm_tap=None) -> list[dict]:
    """逐题驱动；单题异常记 failed_unscored 后继续（failed_unscored 纪律）。"""
    run_root.mkdir(parents=True, exist_ok=True)
    write_resolved(run_root, tasks, spec)
    budget = spec.get("budget", {})
    max_steps = int(budget.get("max_steps", 50))
    max_wall = float(budget.get("max_wall_seconds", 1800))
    episodes: list[dict] = []
    for ref in tasks:
        write_heartbeat(run_root, done=len(episodes), total=len(tasks), current=ref.task_id)
        episode_dir = run_root / "episodes" / (
            f"{ref.domain}-{ref.task_id}" if ref.domain else ref.task_id
        )
        recorder: EpisodeRecorder | None = None
        error: BaseException | None = None
        task: dict = {}
        started = time.perf_counter()
        try:
            task = env.fetch_task(ref.task_id, ref.domain)
            recorder = EpisodeRecorder(
                episode_dir, task, a11y_full=bool(spec.get("record", {}).get("a11y_full", False))
            )
            if llm_tap is not None:
                llm_tap.current = recorder.record_llm_call
            if session.components.set_deadline is not None:
                session.components.set_deadline(started + max_wall)
            session.begin(task["instruction"], spec.get("requirements"))
            episodes.append(
                run_episode(env, session, recorder, task,
                            max_steps=max_steps, max_wall_seconds=max_wall,
                            extra_exports=list(spec.get("export_paths", [])), started_monotonic=started)
            )
        except BaseException as exc:  # 单题失败不终止整个 run
            error = exc
            if recorder is None:
                recorder = EpisodeRecorder(episode_dir, task or {"id": ref.task_id})
            episodes.append(
                recorder.finalize(
                    stop_reason="exception",
                    outputs=[],
                    raw_score=None,
                    score=None,
                    status=episode_status(scored=False, exception=error),
                    exception=error,
                )
            )
        finally:
            if recorder is not None and session.goal is not None:
                recorder.task_memory(session.goal.memory.snapshot())
            if llm_tap is not None:
                llm_tap.current = None
            if session.components.set_deadline is not None:
                session.components.set_deadline(None)
            write_summary(run_root, episodes, spec)
            # obs_stage 是 episode 内工作区（截图正本已由 recorder 写入 episode 目录，
            # a11y 摘要已入 traj）；episode 结束即清理，避免双份存储
            staging = run_root / "obs_stage" / str(ref.task_id)
            shutil.rmtree(staging, ignore_errors=True)
    write_summary(run_root, episodes, spec)
    write_heartbeat(run_root, done=len(episodes), total=len(tasks), current="")
    return episodes
