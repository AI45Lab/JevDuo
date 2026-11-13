"""生成 agent 的 app.yaml / agent.yaml（osworld 后端，连 worker 内 VM 的 HTTP 服务）。

模型 key 不落盘：agent.yaml 只写 ${env.JEVDUO_API_KEY} 引用，真实值由子进程环境注入。
文件内容是 JSON（YAML 子集），因此不依赖 PyYAML。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from launch import API_KEY_ENV

COORDINATE_SPACES = ("image_pixels", "normalized_1000")


def execution_harness(harness: dict, actor_seconds: float) -> dict:
    """Tell Slow the actual remaining acting time, including across task phases."""
    scoped = deepcopy(harness)
    orchestration = scoped.get("orchestration", {})
    if orchestration.get("enabled"):
        orchestration["max_seconds"] = min(
            float(orchestration.get("max_seconds", actor_seconds)), max(1., float(actor_seconds)),
        )
    return scoped


def agent_config(model: dict) -> dict:
    """配置里的模型凭据 + 行为旋钮 → NexAU agent 配置（OpenAI 兼容 Chat）。"""
    llm = {
        "model": model["model"],
        "base_url": model["base_url"],
        "api_key": "${env." + API_KEY_ENV + "}",
        "api_type": "openai_chat_completion",
        "max_tokens": int(model.get("max_tokens", 16384)),
        "max_context_tokens": 128000,
        "max_retries": int(model.get("max_retries", 3)),
        "timeout": float(model.get("timeout_seconds", 600)),
        "stream_idle_timeout_ms": int(float(model.get("timeout_seconds", 600)) * 1000),
        "connect_timeout_ms": 15000,
        "stream": bool(model.get("stream", False)),
    }
    effort = model.get("reasoning_effort")
    if effort == "off":
        llm["extra_body"] = {"enable_thinking": False}
    elif effort:
        llm["reasoning_effort"] = effort
    return {
        "name": "JevDuoDesktop",
        "llm_config": llm,
        "max_iterations": 1,
        "mcp_servers": [
            {"name": "DesktopNativeDataExecutor", "model_visible": False, "type": "in-memory"},
            {"name": "DesktopNativeActionExecutor", "model_visible": True, "type": "in-memory"},
        ],
    }


def app_config(*, vm_ip: str, server_port: int, max_steps: int, artifact_root: Path,
               harness: dict) -> dict:
    """app 配置：osworld 后端指向 VM；评估器关闭，分数只来自 OSWorld 官方评分。"""
    desktop = harness.get("desktop", {})
    coordinate = harness.get("coordinate", {})
    prompt = harness.get("prompt", {})
    execution = harness.get("execution", {})
    return {
        "agent_config_path": "agent.yaml",
        "artifacts": {"root_dir": str(artifact_root)},
        "execution": {
            "max_agent_iterations_per_round": int(max_steps),
            "enable_langfuse_tracer": False,
            "model_response_recovery": bool(execution.get("model_response_recovery", True)),
            "no_tool_call_attempts": int(execution.get("no_tool_call_attempts", 1)),
        },
        "orchestration": dict(harness.get("orchestration", {})),
        "context_management": {"mode": harness.get("context_management", {}).get("mode", "off")},
        "evaluation": {"enabled": bool(harness.get("evaluation", {}).get("enabled", False))},
        "observation": {
            "a11y": {"enabled": bool(harness.get("observation", {}).get("a11y_enabled", False))},
        },
        "desktop": {
            "backend": desktop.get("backend", "osworld"),
            "primary_screen_only": bool(harness.get("observation", {}).get("primary_screen_only", True)),
            "coordinate_policy": {
                "input_space": coordinate.get("input_space", "image_pixels"),
                "clamp_to_bounds": bool(coordinate.get("clamp_to_bounds", True)),
            },
            "osworld": {
                "vm_ip": vm_ip,
                "server_port": int(server_port),
                "command_timeout_seconds": float(desktop.get("command_timeout_seconds", 300)),
                "screenshot_timeout_seconds": float(desktop.get("screenshot_timeout_seconds", 30)),
                "retry_times": int(desktop.get("retry_times", 3)),
                "retry_interval_seconds": float(desktop.get("retry_interval_seconds", 3)),
                "enable_accessibility": bool(harness.get("observation", {}).get("a11y_enabled", False)),
            },
        },
        "prompt": {
            "history_window_rounds": int(prompt.get("history_window_rounds", 100)),
            "screenshot_window_images": int(prompt.get("screenshot_window_images", 3)),
            "include_initial_observation_image": bool(prompt.get("include_initial_observation_image", True)),
            "include_subsequent_observation_images": bool(
                prompt.get("include_subsequent_observation_images", True)),
        },
    }


def write_configs(directory: Path, *, model: dict, harness: dict, vm_ip: str, server_port: int,
                  max_steps: int, artifact_root: Path) -> Path:
    """写 app.yaml / agent.yaml，返回 app.yaml 路径（agent.yaml 按相对路径被引用）。"""
    input_space = harness.get("coordinate", {}).get("input_space", "image_pixels")
    if input_space not in COORDINATE_SPACES:
        raise ValueError(f"coordinate.input_space 应为 {' / '.join(COORDINATE_SPACES)}，收到 {input_space!r}")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(
        json.dumps(agent_config(model), ensure_ascii=False, indent=2), encoding="utf-8")
    app_path = directory / "app.yaml"
    app_path.write_text(json.dumps(app_config(
        vm_ip=vm_ip, server_port=server_port, max_steps=max_steps,
        artifact_root=artifact_root, harness=harness,
    ), ensure_ascii=False, indent=2), encoding="utf-8")
    return app_path
