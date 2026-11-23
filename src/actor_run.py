"""Run one actor in the existing VM; callers own setup, budgets and scoring."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from appcfg import execution_harness, write_configs
from launch import classify, run_agent


class ActorError(RuntimeError):
    def __init__(self, reason: str, stdout: Path, info: dict):
        super().__init__(f"Agent failed: {reason} (see {stdout})")
        self.info = info


def run_actor(vm, *, model: dict, harness: dict, query: str, session_id: str,
              directory: Path, log_dir: Path, started: float) -> tuple[dict, str]:
    reserve = float(harness.get("evaluation_reserve_seconds", 0))
    actor_seconds = float(harness["max_wall_seconds"]) - (time.perf_counter() - started) - reserve
    max_steps = int(harness["max_steps"])
    app = write_configs(
        directory / "config", model=model, harness=execution_harness(harness, actor_seconds),
        vm_ip=vm.vm_ip, server_port=vm.server_port, max_steps=max_steps, artifact_root=log_dir,
    )
    # Configuration generation consumes the same task deadline, including across phases.
    actor_seconds = float(harness["max_wall_seconds"]) - (time.perf_counter() - started) - reserve
    stdout = directory / "agent_stdout.log"
    interaction_env = {}
    if harness.get("user_interaction", {}).get("enabled", False):
        config = vm.user_simulator_config()
        if config is not None:
            private_path = directory / "user_interaction.spec.json"
            descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"runtime_root": str(vm.task_root), "instruction": query,
                           "user_simulator": config, "model": model,
                           "log_dir": str(directory / "user_interactions")}, stream, ensure_ascii=False)
            interaction_env["JEVDUO_USER_SIMULATOR_SPEC"] = str(private_path)
    info = run_agent(
        query=query, session_id=session_id, app_config=app, log_dir=log_dir,
        result_path=directory / "agent_result.json", stdout_path=stdout, max_steps=max_steps,
        timeout_seconds=max(1, actor_seconds),
        inactivity_seconds=float(harness.get("inactivity_timeout_seconds", 0)),
        api_key=model["api_key"], direct_api=bool(model.get("direct_api", False)),
        extra_env=interaction_env,
    )
    action, stop_reason = classify(info)
    if action == "error":
        raise ActorError(stop_reason, stdout, info)
    if action:
        try:
            vm.execute(action, pause=1)
        except Exception as error:
            raise ActorError(str(error), stdout, info) from error
    return info, stop_reason
