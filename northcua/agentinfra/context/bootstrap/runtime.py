"""Build a fresh, session-scoped context coordinator for one model attempt."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message

from northcua.agentinfra.context.adapters import (
    LazySummaryClient,
    NexAUSummaryClient,
    NexAUTokenEstimator,
    UMPContextProjector,
    UMPSummaryMessageFactory,
    resolve_summary_llm_config,
    resolve_summary_provider_runtime,
    summary_prompt_fingerprint,
    summary_reasoning_fingerprint_mode,
    summary_source_capacity_bytes_from_hard_budget,
)
from northcua.agentinfra.context.adapters.provider_runtime import configured_summary_context_limit
from northcua.agentinfra.context.bootstrap.policies import BUILTIN_CONTEXT_POLICY_REGISTRY
from northcua.agentinfra.context.observability import ArtifactContextTraceSink
from northcua.agentinfra.context.persistence import ArtifactCheckpointRepository
from northcua.agentinfra.runtime.artifacts import sanitize_session_id
from northcua.agentkernel.context.application import CheckpointHistoryAssembler, ContextCoordinator
from northcua.agentkernel.context.domain.config import ContextSummaryConfig
from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.agentkernel.context.domain.models import ContextOwnerKey
from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy
from northcua.agentkernel.context.domain.runtime import ContextPreparationRequest, ContextPreparationResult
from northcua.core.config import NorthCUAAppConfig
from northcua.core.config_base import (
    CONTEXT_CHECKPOINT_DIR_NAME,
    CONTEXT_COMPACTION_LOG_NAME,
    CONTEXT_SUMMARY_TRACE_LOG_NAME,
)
from northcua.core.json_types import JsonObject

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedContextMessages:
    """Effective messages plus typed preparation result for logs/tests."""

    messages: list[Message]
    result: ContextPreparationResult[Message] | None


async def prepare_context_messages(
    *,
    app_config: NorthCUAAppConfig,
    agent_kind: str,
    user_id: str,
    session_id: str,
    task_epoch: str,
    logical_agent_path: str,
    logical_step_id: str,
    round_num: int,
    messages: list[Message],
    shadow_messages: list[Message] | None = None,
    tools: list[dict[str, object]],
    agent_config: AgentConfig,
    prepared_agent: Agent,
    primary_llm_config: LLMConfig,
    metadata: JsonObject | None = None,
    recover_provider_overflow: bool = False,
    context_limit_override: int | None = None,
    output_reserve_override: int | None = None,
    artifact_root_override: Path | None = None,
) -> PreparedContextMessages:
    """Apply a whole-pipeline fail-open boundary for observational shadow mode."""

    policy = app_config.context_management.resolve(agent_kind)
    if policy.mode == ContextManagementMode.OFF:
        return PreparedContextMessages(messages=messages, result=None)
    try:
        return await _prepare_context_messages_impl(
            app_config=app_config,
            agent_kind=agent_kind,
            user_id=user_id,
            session_id=session_id,
            task_epoch=task_epoch,
            logical_agent_path=logical_agent_path,
            logical_step_id=logical_step_id,
            round_num=round_num,
            messages=messages,
            shadow_messages=shadow_messages,
            tools=tools,
            agent_config=agent_config,
            prepared_agent=prepared_agent,
            primary_llm_config=primary_llm_config,
            metadata=metadata,
            recover_provider_overflow=recover_provider_overflow,
            context_limit_override=context_limit_override,
            output_reserve_override=output_reserve_override,
            artifact_root_override=artifact_root_override,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        if policy.mode != ContextManagementMode.SHADOW:
            raise
        logger.warning("Shadow context preparation failed open", exc_info=True)
        return PreparedContextMessages(messages=messages, result=None)


async def _prepare_context_messages_impl(
    *,
    app_config: NorthCUAAppConfig,
    agent_kind: str,
    user_id: str,
    session_id: str,
    task_epoch: str,
    logical_agent_path: str,
    logical_step_id: str,
    round_num: int,
    messages: list[Message],
    shadow_messages: list[Message] | None = None,
    tools: list[dict[str, object]],
    agent_config: AgentConfig,
    prepared_agent: Agent,
    primary_llm_config: LLMConfig,
    metadata: JsonObject | None = None,
    recover_provider_overflow: bool = False,
    context_limit_override: int | None = None,
    output_reserve_override: int | None = None,
    artifact_root_override: Path | None = None,
) -> PreparedContextMessages:
    """Prepare bounded context while preserving an exact no-op OFF path."""

    policy = app_config.context_management.resolve(agent_kind)
    if policy.mode == ContextManagementMode.OFF:
        return PreparedContextMessages(messages=messages, result=None)
    if policy.mode == ContextManagementMode.ENFORCE:
        _reject_double_compaction(agent_config)

    effective_llm_config = agent_config.llm_config or primary_llm_config
    estimator = NexAUTokenEstimator(model=str(primary_llm_config.model or effective_llm_config.model or "gpt-4o"))
    projector = UMPContextProjector(
        policy_profile=policy.policy_profile,
        recent_iterations=policy.budget.recent_iterations,
    )
    analysis_messages = (
        shadow_messages if policy.mode == ContextManagementMode.SHADOW and shadow_messages is not None else messages
    )
    segments = projector.project(analysis_messages, round_num=round_num)
    session_root = artifact_root_override or (Path(app_config.artifacts.root_dir) / sanitize_session_id(session_id))
    trace_sink = ArtifactContextTraceSink(
        summary_trace_path=session_root / CONTEXT_SUMMARY_TRACE_LOG_NAME,
        compaction_path=session_root / CONTEXT_COMPACTION_LOG_NAME,
        payload_mode=policy.tracing.payload,
    )
    repository = None
    summary_client = None
    summary_llm_config = primary_llm_config
    if policy.mode == ContextManagementMode.ENFORCE:
        summary_policy = BUILTIN_CONTEXT_POLICY_REGISTRY.resolve(policy.policy_profile)
        summary_llm_config = resolve_summary_llm_config(
            config=policy.summary,
            primary_llm_config=primary_llm_config,
        )
        repository = ArtifactCheckpointRepository(
            session_root / CONTEXT_CHECKPOINT_DIR_NAME,
        )
        summary_client = LazySummaryClient(
            lambda: _build_summary_client(
                config=policy.summary,
                primary_llm_config=primary_llm_config,
                prepared_agent=prepared_agent,
                session_id=session_id,
                summary_policy=summary_policy,
                primary_context_limit_tokens=int(agent_config.max_context_tokens or 0),
            ),
            source_capacity_bytes=_summary_source_capacity_bytes(
                context_limit_tokens=_resolve_summary_context_limit(
                    config=policy.summary,
                    primary_llm_config=primary_llm_config,
                    primary_context_limit_tokens=int(agent_config.max_context_tokens or 0),
                ),
                config=policy.summary,
            ),
        )
    coordinator = ContextCoordinator[Message, Mapping[str, object]](
        estimator=estimator,
        assembler=CheckpointHistoryAssembler(UMPSummaryMessageFactory()),
        repository=repository,
        summary_client=summary_client,
        trace_sink=trace_sink,
    )
    llm_config = effective_llm_config
    context_limit = int(context_limit_override or agent_config.max_context_tokens or 0)
    output_reserve = int(
        output_reserve_override
        if output_reserve_override is not None
        else llm_config.max_tokens or policy.summary.max_output_tokens
    )
    owner = ContextOwnerKey(
        user_id=user_id,
        session_id=session_id,
        task_epoch=task_epoch,
        logical_agent_path=logical_agent_path,
    )
    policy_fingerprint = policy.fingerprint()
    resolved_summary_policy = BUILTIN_CONTEXT_POLICY_REGISTRY.resolve(policy.policy_profile)
    prompt_fingerprint = summary_prompt_fingerprint(
        resolved_summary_policy,
        prompt_profile=policy.summary.prompt_profile,
    )
    model_fingerprint = _fingerprint(
        {
            "source": policy.summary.model.source.value,
            "model": str(summary_llm_config.model or ""),
            "api_type": str(summary_llm_config.api_type or ""),
            "base_url": str(summary_llm_config.base_url or ""),
            "reasoning_policy": summary_reasoning_fingerprint_mode(summary_llm_config),
        }
    )
    request = ContextPreparationRequest[Message, Mapping[str, object]](
        logical_step_id=logical_step_id,
        owner=owner,
        agent_kind=agent_kind,
        policy=policy,
        original_messages=tuple(analysis_messages),
        segments=segments,
        tools=tuple(tools),
        context_limit=context_limit,
        output_reserve=output_reserve,
        policy_fingerprint=policy_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        model_fingerprint=model_fingerprint,
        parent_main_invocation_id=logical_step_id,
        metadata=metadata,
    )
    try:
        result = (
            await coordinator.recover_overflow(request)
            if recover_provider_overflow
            else await coordinator.prepare(request)
        )
    finally:
        if summary_client is not None:
            await _close_summary_client_best_effort(summary_client)
    return PreparedContextMessages(
        messages=list(messages) if policy.mode == ContextManagementMode.SHADOW else list(result.messages),
        result=result,
    )


def _reject_double_compaction(agent_config: AgentConfig) -> None:
    if agent_config.sub_agents:
        raise ValueError(
            "context_management=enforce does not yet support NexAU in-process sub_agents; "
            "run child agents through AgentKit with an explicit logical_agent_path"
        )
    for middleware in agent_config.middlewares or []:
        middleware_type = type(middleware)
        qualified_name = f"{middleware_type.__module__}.{middleware_type.__qualname__}".lower()
        if "context_compaction" in qualified_name:
            raise ValueError("NorthCUA context_management=enforce cannot run with NexAU ContextCompactionMiddleware")


def _build_summary_client(
    *,
    config: ContextSummaryConfig,
    primary_llm_config: LLMConfig,
    prepared_agent: Agent,
    session_id: str,
    summary_policy: ContextSummaryPolicy,
    primary_context_limit_tokens: int,
) -> NexAUSummaryClient:
    runtime = resolve_summary_provider_runtime(
        config=config,
        primary_llm_config=primary_llm_config,
        prepared_agent=prepared_agent,
        primary_context_limit_tokens=primary_context_limit_tokens,
    )
    return NexAUSummaryClient(
        primary_llm_config=runtime.llm_config,
        openai_client=runtime.sync_client,
        async_openai_client=runtime.async_client,
        owned_clients=runtime.owned_clients,
        session_id=session_id,
        config=config,
        policy=summary_policy,
        context_limit_tokens=runtime.context_limit_tokens,
    )


def _resolve_summary_context_limit(
    *,
    config: ContextSummaryConfig,
    primary_llm_config: LLMConfig,
    primary_context_limit_tokens: int,
) -> int:
    if config.model.source.value == "inherit_primary":
        resolved = int(primary_context_limit_tokens or getattr(primary_llm_config, "max_context_tokens", 0) or 0)
        if resolved <= 0:
            raise ValueError("primary max_context_tokens is required for enforce context summary")
        return resolved
    config_path = Path(config.model.agent_config_path or "").expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    from nexau.archs.main_sub.utils import load_yaml_with_vars

    raw = load_yaml_with_vars(config_path)
    resolved = configured_summary_context_limit(raw)
    if resolved <= 0:
        raise ValueError(f"dedicated summary agent config requires max_context_tokens: {config_path}")
    return resolved


def _summary_source_capacity_bytes(*, context_limit_tokens: int, config: ContextSummaryConfig) -> int:
    base = context_limit_tokens - config.max_output_tokens
    hard = base - max(1024, int(base * 0.08))
    if hard <= 0:
        raise ValueError("summary model context window is too small for configured output reserve")
    return summary_source_capacity_bytes_from_hard_budget(
        hard_input_budget=hard,
        max_chunks=config.max_chunks,
        max_calls=config.max_calls,
        repair_attempts=config.repair_attempts,
    )


def _fingerprint(payload: JsonObject) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _close_summary_client_best_effort(summary_client: LazySummaryClient) -> None:
    """Release owned SDK clients without overriding the preparation outcome."""

    try:
        await summary_client.close()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Unable to close owned context summary client", exc_info=True)
