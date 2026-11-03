"""组件契约（依赖注入边界，无插件框架、无隐藏分发）。
semantic_enricher 统一 planner 与 selector 的元素语义；goal 额外注入任务记忆、阶段验证与
按需视觉补全。step/stage 装配不变。compile_gui 只产出协议动作，不调用 VM。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from .config import SessionConfig
from .gui.candidates import ActionCandidateBuilder
from .gui.compile import gui_action
from .gui.grounding import ground_visual
from .gui.interaction import GuiInteractionController
from .gui.proposals import focused_candidates
from .gui.semantics import enrich_targets
from .gui.targets import A11yTargetBuilder
from .memory.state import TaskMemory
from .models.jev import JevOmniPolicy
from .planning.goal_planner import goal_requirements, plan_goal
from .planning.planner import plan_step
from .planning.stage_planner import plan_stage
from .verification.goal_verifier import verify_goal
from .verification.verifier import task_requirements, verify_observation


@dataclass
class Components:
    planner: Callable
    verifier: Callable
    requirements: Callable
    selector: object
    target_builder: object
    semantic_enricher: Callable
    candidate_builder: object
    focus_candidates: Callable
    grounder: Callable | None
    compile_gui: Callable
    memory_factory: Callable | None = None
    stage_verifier: Callable | None = None
    set_deadline: Callable | None = None
    interaction_factory: Callable = GuiInteractionController


def build_default(*, client, endpoint: str, config: SessionConfig,
                  image_stage: tuple[str, str] | None = None, endpoint_provider=None) -> Components:
    if config.fast_backend == "planner":
        from .models.planner_choice import PlannerChoicePolicy

        selector = PlannerChoicePolicy(client)
    elif config.fast_backend == "kev":
        from .models.kev import KevPolicy

        selector = KevPolicy(endpoint, timeout_seconds=config.fast_timeout_seconds)
    else:
        selector = JevOmniPolicy(endpoint, timeout_seconds=config.fast_timeout_seconds,
                                 image_stage=image_stage, endpoint_provider=endpoint_provider,
                                 joint_actions=config.execution_mode == "goal")
    return Components(
        planner=partial(
            plan_goal if config.execution_mode == "goal" else
            plan_stage if config.execution_mode == "stage" else plan_step,
            client,
            allow_cli=config.allow_cli and config.route_policy != "gui",
            route_policy=config.route_policy,
        ),
        verifier=partial(verify_observation, client),
        requirements=partial(
            goal_requirements if config.execution_mode == "goal" else task_requirements, client),
        selector=selector,
        target_builder=A11yTargetBuilder(),
        semantic_enricher=enrich_targets,
        candidate_builder=ActionCandidateBuilder(),
        focus_candidates=focused_candidates,
        grounder=partial(ground_visual, client) if config.execution_mode == "goal" else None,
        compile_gui=gui_action,
        memory_factory=TaskMemory if config.execution_mode == "goal" else None,
        stage_verifier=partial(verify_goal, client) if config.execution_mode == "goal" else None,
        set_deadline=getattr(client, "set_deadline", None),
    )
