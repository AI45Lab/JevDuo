"""entry：job spec → 环境/harness/runner 组装（worker 侧唯一入口，infra/worker.sh 调用）。

benchmark=mock：MockEnv + 确定性脚本组件（全链路自检）。
真实基准：环境适配器（共享目录 runtime/VM）+ SlowModelClient + build_default 组件，
凭据与端点路径由提交侧从 infra/config.json 注入 spec（secrets 段）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tandem.components import Components
from tandem.config import SessionConfig
from tandem.models.ranking import Ranking
from tandem.runtime.session import TandemSession

from .env import MockEnv
from .manifest import resolve_manifest
from .recorder import write_code_version
from .runner import run


class LlmTap:
    """逐题重定向的 llm_sink：runner 每题把 current 指向该 episode 的 recorder。"""

    def __init__(self):
        self.current = None

    def __call__(self, **kwargs):
        if self.current is not None:
            self.current(**kwargs)


_EVOBOX_CANONICAL = "/mnt/shared-storage-gpfs2/evobox-share-gpfs2"
_EVOBOX_USER = "/mnt/shared-storage-user/evobox-share-gpfs2"


def read_endpoint_file(path: str) -> str:
    """从部署侧维护的 endpoint 文件读 base_url；符号链接在当前挂载视角断裂时
    尝试规范/用户两种前缀。"""
    candidates = [Path(path)]
    translated = str(path).replace(_EVOBOX_CANONICAL, _EVOBOX_USER)
    if translated != str(path):
        candidates.append(Path(translated))
    reverse = str(path).replace(_EVOBOX_USER, _EVOBOX_CANONICAL)
    if reverse != str(path):
        candidates.append(Path(reverse))
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("ready") is False or data.get("status") not in (None, "ready"):
            raise ValueError(
                f"发现文件 {candidate} 显示服务未就绪（status={data.get('status')!r}, "
                f"ready={data.get('ready')!r}），稍后重跑或检查服务部署"
            )
        base_url = data.get("base_url")
        if base_url:
            return str(base_url).rstrip("/")
    raise ValueError(
        f"无法从 {path} 读取 endpoint（文件缺失/损坏，或符号链接在当前挂载视角下断裂）"
    )


def resolve_fast_model_endpoint(fast: dict) -> str:
    """快模型地址：显式 endpoint 优先；为空则从发现文件读取（闲时部署，地址常变）。

    fast 段来自 job spec 的 secrets.fast_model（已由提交侧按选中的 API 条目填好）。
    """
    explicit = fast.get("endpoint")
    if explicit:
        return str(explicit).rstrip("/")
    file_path = fast.get("endpoint_file")
    if file_path:
        return read_endpoint_file(file_path)
    raise ValueError(
        "快模型地址为空且未配置 endpoint_file"
        "（infra/config.json → harness.fast_model.apis.<名字>）"
    )


def git_describe(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


class _ScriptedPlanner:
    """mock 模式的确定性 planner：先点第一个目标，再声称完成。"""

    def __init__(self, *, stage_mode=False):
        self.calls = 0
        self.stage_mode = stage_mode

    def __call__(self, instruction, screenshot, targets, recent, tree,
                 verified_focus=None, requirements=None, remaining_steps=None, *, memory=None,
                 remaining_seconds=None):
        self.calls += 1
        if memory is not None and self.calls == 1:
            return {"done": False, "work_item_id": memory.ready_items()[0]["id"],
                    "route": "gui", "purpose": "produce", "goal": "mock click",
                    "completion_criteria": "mock receipt", "method": "mock click",
                    "inputs": [], "input_fact_keys": [], "guidance": "", "max_actions": 1,
                    "command": ""}
        if self.calls == 1 and targets:
            target = targets[0]
            plan = {
                "done": False, "task_status": "running",
                "current_goal": "操作第一个控件", "expected_observation": "界面变化",
                "action_proposals": [{
                    "operation": "click", "target_id": target.id,
                    "target_name": target.name, "target_role": target.role,
                }],
            }
            if self.stage_mode:
                plan.update(route="gui", checks=[])
                return {"done": False, "stage_goal": "mock stage", "milestones": [plan]}
            return plan
        return {"done": True, "task_status": "completed", "current_goal": "完成",
                "expected_observation": ""}


class _ScriptedVerifier:
    def __call__(self, instruction, screenshot, **kwargs):
        claim = kwargs.get("terminal_claim")
        status = "completed" if claim else "incomplete"
        return {
            "outcome": "achieved", "evidence": "mock 证据", "task_status": status,
            "path_input_focused": False, "state_changed": True,
            "requirement_checks": [],
        }


class _FirstCandidateSelector:
    def rank(self, **kwargs):
        candidates = kwargs["action_space"].candidates()
        winner = candidates[0] if candidates else None
        return Ranking(
            winner=winner,
            action_ranked=[{"id": winner.id, "score": 1.0}] if winner else [],
            operation_ranked=[{"operation": winner.operation, "score": 1.0}] if winner else [],
            operation_margin=0.0, target_margin=0.0, latency_ms=0.1, used_image=False,
        )


def mock_components(*, stage_mode=False, goal_mode=False) -> Components:
    from tandem.gui.candidates import ActionCandidateBuilder
    from tandem.gui.compile import gui_action
    from tandem.gui.proposals import focused_candidates
    from tandem.gui.semantics import enrich_targets
    from tandem.gui.targets import A11yTargetBuilder
    from tandem.memory.state import TaskMemory

    def goal_verifier(*args, **kwargs):
        return {"stage_outcome": "met", "unit_met": True, "delivery_state": "verified",
                "summary": "mock evidence only", "remaining": [],
                "evidence_refs": list(kwargs["evidence"]), "facts": [], "materials": [],
                "requirement_checks": [], "artifacts": [], "invalidated_work_item_ids": []}

    def requirements(instruction):
        return [{"id": "R1", "requirement": instruction, "kind": "delivery", "units": [instruction],
                 "unit_fact_keys": [[]], "facts_needed": [], "output_path": None}] if goal_mode else []

    return Components(
        planner=_ScriptedPlanner(stage_mode=stage_mode), verifier=_ScriptedVerifier(),
        requirements=requirements, selector=_FirstCandidateSelector(),
        target_builder=A11yTargetBuilder(), candidate_builder=ActionCandidateBuilder(),
        semantic_enricher=enrich_targets,
        focus_candidates=focused_candidates, grounder=None, compile_gui=gui_action,
        memory_factory=TaskMemory if goal_mode else None,
        stage_verifier=goal_verifier if goal_mode else None,
    )


def build_session(spec: dict, *, llm_tap: LlmTap | None = None) -> TandemSession:
    config = SessionConfig(**spec.get("harness_config", {}))
    benchmark = spec.get("benchmark")
    if benchmark == "mock":
        return TandemSession(mock_components(stage_mode=config.execution_mode == "stage",
                                             goal_mode=config.execution_mode == "goal"), config=config)
    if benchmark in {"osworld1", "osworld2", "osworld21"}:
        from tandem.components import build_default
        from tandem.models.slow import SlowModelClient

        secrets = spec["secrets"]
        credentials = dict(secrets["slow_model"])
        endpoint = ("" if config.fast_backend == "planner" else
                    resolve_fast_model_endpoint(secrets.get("fast_model") or {}))
        run_root = Path(spec["output_root"])
        run_root.mkdir(parents=True, exist_ok=True)
        client = SlowModelClient(credentials, config, run_root / "slow_model_http.jsonl",
                            llm_sink=llm_tap)
        stage = spec.get("image_stage") or {}
        image_stage = None
        if stage.get("write") and stage.get("read"):
            image_stage = (stage["write"], stage["read"])
        components = build_default(
            client=client, endpoint=endpoint, config=config,
            image_stage=image_stage,
            endpoint_provider=lambda: resolve_fast_model_endpoint(secrets.get("fast_model") or {}),
        )
        return TandemSession(components, config=config)
    raise NotImplementedError(f"未知 benchmark: {benchmark}")


def build_env(spec: dict):
    benchmark = spec.get("benchmark")
    if benchmark == "mock":
        base = Path(spec["output_root"]) / "mock_env"
        return MockEnv(base_dir=base)
    if benchmark in {"osworld1", "osworld2", "osworld21"}:
        from .osworld import OsWorldEnv

        env_spec = spec["env"]
        return OsWorldEnv(
            benchmark=benchmark,
            task_root=Path(env_spec["root"]),
            vm_path=Path(env_spec["vm"]),
            run_root=Path(spec["output_root"]),
            asset_root=Path(env_spec["asset_root"]) if env_spec.get("asset_root") else None,
            setup_cache_root=Path(env_spec["setup_cache_root"]) if env_spec.get("setup_cache_root") else None,
            startup_wait_seconds=float(env_spec.get("startup_wait_seconds", 30.0)),
            volume_size=env_spec.get("volume_size"),
        )
    raise NotImplementedError(f"未知 benchmark: {benchmark}")


def _mask_secrets(spec: dict) -> dict:
    """run_spec 落盘前脱敏：secrets 段的值全部打码（密钥/registry 凭据不进结果目录）。"""
    masked = json.loads(json.dumps(spec, ensure_ascii=False))
    secrets = masked.get("secrets")
    if isinstance(secrets, dict):
        masked["secrets"] = {
            key: {subkey: "***" for subkey in value} if isinstance(value, dict) else "***"
            for key, value in secrets.items()
        }
    return masked


def run_from_spec(spec: dict) -> list[dict]:
    os.environ.update(spec.get("secrets", {}).get("environment", {}))
    run_root = Path(spec["output_root"])
    run_root.mkdir(parents=True, exist_ok=True)
    repo_root = Path(spec.get("repo_root", "."))
    write_code_version(
        run_root,
        {
            "git_describe": spec.get("code", {}).get("git_describe", git_describe(repo_root)),
            "resources": spec.get("code", {}).get("resources", {}),
            "images": spec.get("code", {}).get("images", {}),
            "run_spec": _mask_secrets(spec),
        },
    )
    llm_tap = LlmTap()
    env = build_env(spec)
    session = build_session(spec, llm_tap=llm_tap)
    tasks = resolve_manifest(spec)
    if not tasks:
        raise ValueError("manifest 解析结果为空")
    return run(spec, env=env, session=session, tasks=tasks, run_root=run_root, llm_tap=llm_tap)


def entry_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="tandem-eval entry")
    parser.add_argument("--spec", required=True, help="job spec JSON 路径")
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    episodes = run_from_spec(spec)
    finished = sum(1 for item in episodes if item.get("status") == "scored")
    print(json.dumps({"done": len(episodes), "scored": finished}, ensure_ascii=False))
    return 0
