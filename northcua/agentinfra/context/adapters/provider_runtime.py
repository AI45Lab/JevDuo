"""Isolated provider-client construction for internal summary calls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import anthropic
import openai
from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.utils import load_yaml_with_vars

from northcua.agentkernel.context.domain.config import ContextSummaryConfig
from northcua.agentkernel.context.domain.enums import SummaryModelSource


@dataclass(frozen=True, slots=True)
class SummaryProviderRuntime:
    """Resolved LLM config and clients, with explicit ownership."""

    llm_config: LLMConfig
    sync_client: object | None
    async_client: object | None
    context_limit_tokens: int
    owned_clients: tuple[object, ...] = ()


def resolve_summary_provider_runtime(
    *,
    config: ContextSummaryConfig,
    primary_llm_config: LLMConfig,
    prepared_agent: Agent,
    primary_context_limit_tokens: int | None = None,
) -> SummaryProviderRuntime:
    """Resolve inherited or dedicated summary clients without constructing an Agent."""

    del prepared_agent
    if config.model.source == SummaryModelSource.INHERIT_PRIMARY:
        resolve_summary_llm_config(config=config, primary_llm_config=primary_llm_config)
        context_limit_tokens = int(
            primary_context_limit_tokens or getattr(primary_llm_config, "max_context_tokens", 0) or 0
        )
        if context_limit_tokens <= 0:
            raise ValueError("primary max_context_tokens is required for enforce context summary")
        sync_client, async_client = _create_clients(primary_llm_config)
        return SummaryProviderRuntime(
            llm_config=primary_llm_config,
            sync_client=sync_client,
            async_client=async_client,
            context_limit_tokens=context_limit_tokens,
            owned_clients=tuple(client for client in (async_client, sync_client) if client is not None),
        )

    llm_config = resolve_summary_llm_config(config=config, primary_llm_config=primary_llm_config)
    config_path = Path(config.model.agent_config_path or "").expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    raw_config = load_yaml_with_vars(config_path)
    context_limit_tokens = configured_summary_context_limit(raw_config)
    if context_limit_tokens <= 0:
        raise ValueError(f"dedicated summary agent config requires max_context_tokens: {config_path}")
    sync_client, async_client = _create_clients(llm_config)
    return SummaryProviderRuntime(
        llm_config=llm_config,
        sync_client=sync_client,
        async_client=async_client,
        context_limit_tokens=context_limit_tokens,
        owned_clients=tuple(client for client in (async_client, sync_client) if client is not None),
    )


def configured_summary_context_limit(raw_config: object) -> int:
    if not isinstance(raw_config, dict):
        return 0
    root_value = int(raw_config.get("max_context_tokens", 0) or 0)
    raw_llm = raw_config.get("llm_config")
    nested_value = int(raw_llm.get("max_context_tokens", 0) or 0) if isinstance(raw_llm, dict) else 0
    if root_value and nested_value and root_value != nested_value:
        raise ValueError("summary agent config has conflicting root and llm_config max_context_tokens")
    return root_value or nested_value


def resolve_summary_llm_config(*, config: ContextSummaryConfig, primary_llm_config: LLMConfig) -> LLMConfig:
    """Resolve summary model metadata without constructing provider clients."""

    if config.model.source == SummaryModelSource.INHERIT_PRIMARY:
        if primary_llm_config.api_type == "generate_with_token":
            raise ValueError(
                "generate_with_token is unsupported for context summary because it requires a training token session"
            )
        return primary_llm_config
    config_path = Path(config.model.agent_config_path or "").expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.is_file():
        raise ValueError(f"summary agent config does not exist: {config_path}")
    raw_config = load_yaml_with_vars(config_path)
    raw_llm_config = raw_config.get("llm_config") if isinstance(raw_config, dict) else None
    if not isinstance(raw_llm_config, dict):
        raise ValueError(f"summary agent config has no llm_config: {config_path}")
    llm_config = LLMConfig(**raw_llm_config)  # pyright: ignore[reportArgumentType]
    if llm_config.api_type == "generate_with_token":
        raise ValueError(
            "generate_with_token is unsupported for context summary because it requires a training token session"
        )
    return llm_config


def _create_clients(llm_config: LLMConfig) -> tuple[object | None, object | None]:
    if llm_config.api_type in {"gemini_rest", "generate_with_token"}:
        return None, None
    client_kwargs = llm_config.to_client_kwargs()
    # One summary call record must correspond to one physical HTTP attempt.
    # Provider SDK defaults retry twice when the zero value is omitted.
    client_kwargs["max_retries"] = 0
    if llm_config.api_type == "anthropic_chat_completion":
        return anthropic.Anthropic(**client_kwargs), anthropic.AsyncAnthropic(**client_kwargs)
    if llm_config.api_type in {"openai_responses", "openai_chat_completion"}:
        return openai.OpenAI(**client_kwargs), openai.AsyncOpenAI(**client_kwargs)
    raise ValueError(f"unsupported summary LLM api_type: {llm_config.api_type}")
