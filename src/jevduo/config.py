"""harness 侧会话配置。环境生命周期/预算强制/落盘配置属于 jevduo_eval（M2），不在此处。

字段按**角色**命名，不按厂商命名：`slow_*` 是大脑（慢模型）的旋钮，
`fast_*` 是小脑（快模型排序后端）的旋钮。换厂商只换配置里的 API 条目，
不改这里，也不改状态机。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionConfig:
    allow_cli: bool = True
    route_policy: str = "auto"
    cli_timeout_seconds: int = 90
    execution_mode: str = "step"
    stage_max_actions: int = 24
    goal_recovery_max_calls: int = 2
    goal_recovery_max_seconds: float = 300.0  # Checked before another grounding call, not an HTTP interrupt.
    slow_max_tokens: int = 16384
    slow_verify_max_tokens: int = 16384
    slow_verify_reasoning_effort: str | None = None
    stage_verify_max_chars: int = 48000
    slow_timeout_seconds: float = 120
    slow_direct_api: bool = False
    slow_reasoning_effort: str | None = None  # None=服务端默认；off=enable_thinking:false
    slow_image_quality: int = 85
    slow_max_request_bytes: int = 2 * 1024 * 1024
    fast_backend: str = "jev"  # jev / kev；planner 显式复用慢模型 API 选择候选。
    fast_timeout_seconds: float = 300

    def __post_init__(self):
        if self.execution_mode not in {"step", "stage", "goal"}:
            raise ValueError("execution_mode must be step, stage or goal")
        if type(self.stage_max_actions) is not int or self.stage_max_actions < 1:
            raise ValueError("stage_max_actions must be a positive integer")
        if type(self.goal_recovery_max_calls) is not int or self.goal_recovery_max_calls < 1:
            raise ValueError("goal_recovery_max_calls must be a positive integer")
        if self.goal_recovery_max_seconds <= 0:
            raise ValueError("goal_recovery_max_seconds must be positive")
        if self.route_policy not in {"auto", "gui", "cli"}:
            raise ValueError("route_policy must be auto, gui or cli")
        if not 1 <= self.cli_timeout_seconds <= 120:
            raise ValueError("cli_timeout_seconds must be within 1..120 (协议上限)")
        if self.fast_backend not in {"jev", "kev", "planner"}:
            raise ValueError("fast_backend must be jev, kev or planner")
        if self.slow_reasoning_effort not in {None, "off", "low", "medium", "high", "max"}:
            raise ValueError("slow_reasoning_effort must be null/off/low/medium/high/max")
        if self.slow_verify_reasoning_effort not in {None, "off", "low", "medium", "high", "max"}:
            raise ValueError("slow_verify_reasoning_effort must be null/off/low/medium/high/max")
