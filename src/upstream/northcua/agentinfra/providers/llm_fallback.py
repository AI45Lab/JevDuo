"""NorthCUA-owned multi-model fallback helpers built on NexAU LLMConfig.

Fallback definitions live outside NexAU's schema, then are attached to
``AgentConfig.global_storage`` so runtime code can iterate primary and fallback
model candidates without changing provider payload shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.config import AgentConfig

# pyright: reportMissingImports=false


LLM_FALLBACKS_STORAGE_KEY = "__northcua_llm_fallbacks__"
type LLMConfigPayload = dict[str, object]

_COMMON_LLM_FIELDS: tuple[str, ...] = (
    "base_url",
    "api_key",
    "temperature",
    "max_tokens",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "timeout",
    "max_retries",
    "debug",
    "stream",
    "additional_drop_params",
    "api_type",
    "cache_control_ttl",
    "tokenizer_path",
    "stream_idle_timeout_ms",
    "connect_timeout_ms",
)
_INHERITED_EXTRA_PARAM_KEYS: tuple[str, ...] = (
    "provider_profile",
    "gateway_profile",
    "provider_gateway",
    "image_transport",
    "transcode_images_to_jpeg",
    "supports_images",
    "supports_tool_calls",
    "supports_structured_output",
    "preferred_for_multimodal",
    "supports_responses_api",
    "supports_reasoning_content_replay",
    "reasoning_replay_policy",
    "tool_image_policy",
)
_PROVIDER_PAYLOAD_BLOCKLIST_KEYS: tuple[str, ...] = ("max_context_tokens",)


@dataclass(slots=True, frozen=True)
class LLMFallbackCandidate:
    """One model candidate in an agent-local fallback chain.

    Attributes:
        name: Human-readable fallback name.
        llm_config: NexAU LLM configuration for the candidate.
        index: Candidate order in the fallback chain.
        is_primary: Whether this candidate is the primary model.
    """

    name: str
    llm_config: LLMConfig
    index: int
    is_primary: bool = False


def parse_llm_fallbacks(
    raw_fallbacks: object, *, primary_llm_config: LLMConfig | None
) -> tuple[LLMFallbackCandidate, ...]:
    """Parse NorthCUA's top-level ``llm_fallbacks`` config extension.

    The primary model remains ``llm_config``. Fallback entries inherit common
    connection/retry fields and NorthCUA provider capability flags from the
    primary config unless explicitly overridden.

    Args:
        raw_fallbacks: Raw ``llm_fallbacks`` config extension.
        primary_llm_config: Primary NexAU LLM config used for inheritance.

    Returns:
        Parsed fallback candidates.

    Raises:
        ValueError: If the fallback extension is not list-like or contains non-mapping entries.
    """

    if primary_llm_config is None or raw_fallbacks in (None, False):
        return ()
    if isinstance(raw_fallbacks, Mapping):
        if raw_fallbacks.get("enabled") is False:
            return ()
        raw_items = (
            raw_fallbacks.get("candidates") or raw_fallbacks.get("models") or raw_fallbacks.get("fallbacks") or []
        )
    else:
        raw_items = raw_fallbacks
    if not isinstance(raw_items, list):
        raise ValueError("llm_fallbacks must be a list or a mapping with candidates/models/fallbacks")

    base_config = _llm_config_inheritance_base(primary_llm_config)
    candidates: list[LLMFallbackCandidate] = []
    for zero_index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise ValueError("Each llm_fallbacks entry must be a mapping")
        item = dict(raw_item)
        name = str(item.pop("name", "") or "").strip()
        inherit = bool(item.pop("inherit", True))
        raw_llm_config = item.pop("llm_config", item)
        if not isinstance(raw_llm_config, Mapping):
            raise ValueError("Each llm_fallbacks entry must contain mapping LLM config fields")
        merged = dict(base_config if inherit else {})
        merged.update({str(key): value for key, value in raw_llm_config.items()})
        llm_config = _llm_config_from_payload(merged)
        candidates.append(
            LLMFallbackCandidate(
                name=name or str(llm_config.model or f"fallback_{zero_index + 1}"),
                llm_config=llm_config,
                index=zero_index + 1,
            )
        )
    return tuple(candidates)


def attach_llm_fallbacks(agent_config: AgentConfig, fallbacks: tuple[LLMFallbackCandidate, ...]) -> AgentConfig:
    """Attach fallback candidates to a NexAU AgentConfig without changing NexAU schema.

    Args:
        agent_config: NexAU agent config to update in place.
        fallbacks: Parsed fallback candidates.

    Returns:
        The same agent config with fallback metadata attached when present.
    """

    if not fallbacks:
        return agent_config
    global_storage = dict(agent_config.global_storage or {})
    global_storage[LLM_FALLBACKS_STORAGE_KEY] = fallbacks
    agent_config.global_storage = global_storage
    return agent_config


def llm_fallback_candidates(agent_config: AgentConfig) -> tuple[LLMFallbackCandidate, ...]:
    """Return primary + fallback LLM candidates for one AgentConfig.

    Args:
        agent_config: NexAU agent config containing primary and optional fallback metadata.

    Returns:
        Tuple containing the primary candidate followed by fallback candidates.
    """

    primary_llm_config = agent_config.llm_config
    if primary_llm_config is None:
        return ()
    primary = LLMFallbackCandidate(
        name=str(primary_llm_config.model or "primary"),
        llm_config=primary_llm_config,
        index=0,
        is_primary=True,
    )
    raw_fallbacks = (agent_config.global_storage or {}).get(LLM_FALLBACKS_STORAGE_KEY, ())
    if isinstance(raw_fallbacks, tuple):
        return (primary, *raw_fallbacks)
    if isinstance(raw_fallbacks, list):
        return (primary, *tuple(item for item in raw_fallbacks if isinstance(item, LLMFallbackCandidate)))
    return (primary,)


def agent_config_for_llm_candidate(agent_config: AgentConfig, candidate: LLMFallbackCandidate) -> AgentConfig:
    """Clone an AgentConfig for one fallback candidate while preserving NexAU runtime infra.

    Args:
        agent_config: Source NexAU agent config.
        candidate: Candidate LLM config to install on the clone.

    Returns:
        Shallow-copied agent config with a runtime-safe LLM config.
    """

    candidate_config = agent_config.model_copy(deep=False)
    candidate_config.llm_config = runtime_llm_config(candidate.llm_config)
    candidate_config.global_storage = dict(agent_config.global_storage or {})
    return candidate_config


def conservative_context_budget_inputs(
    agent_config: AgentConfig,
    candidates: tuple[LLMFallbackCandidate, ...],
) -> tuple[int, int]:
    """Return the safest context limit and output reserve across candidates."""

    context_limits = [int(agent_config.max_context_tokens or 0)]
    output_reserves: list[int] = []
    for candidate in candidates:
        candidate_limit = candidate.llm_config.get_param("max_context_tokens")
        if isinstance(candidate_limit, int) and candidate_limit > 0:
            context_limits.append(candidate_limit)
        candidate_output = candidate.llm_config.max_tokens
        if isinstance(candidate_output, int) and candidate_output >= 0:
            output_reserves.append(candidate_output)
    positive_limits = [value for value in context_limits if value > 0]
    if not positive_limits:
        raise ValueError("context management requires a positive model context limit")
    return min(positive_limits), max(output_reserves, default=0)


def runtime_llm_config(llm_config: LLMConfig) -> LLMConfig:
    """Return a NexAU-safe LLMConfig without NorthCUA-only provider capability flags.

    Args:
        llm_config: NorthCUA-extended LLM config.

    Returns:
        NexAU-safe LLM config.
    """

    payload = _llm_config_full_payload(llm_config)
    for key in _INHERITED_EXTRA_PARAM_KEYS:
        payload.pop(key, None)
    for key in _PROVIDER_PAYLOAD_BLOCKLIST_KEYS:
        payload.pop(key, None)
    payload["base_url"] = _runtime_base_url(
        base_url=payload.get("base_url"),
        api_type=str(payload.get("api_type") or ""),
    )
    return _llm_config_from_payload(payload)


def _llm_config_from_payload(payload: Mapping[str, object]) -> LLMConfig:
    """Build a NexAU LLMConfig from dynamic NorthCUA config payloads."""

    return LLMConfig(**dict(payload))  # pyright: ignore[reportArgumentType]


def _llm_config_inheritance_base(llm_config: LLMConfig) -> LLMConfigPayload:
    base: LLMConfigPayload = {"model": llm_config.model}
    for field_name in _COMMON_LLM_FIELDS:
        value = getattr(llm_config, field_name, None)
        if value is not None:
            base[field_name] = value
    extra_params = getattr(llm_config, "extra_params", None)
    if isinstance(extra_params, dict):
        for key in _INHERITED_EXTRA_PARAM_KEYS:
            if key in extra_params:
                base[key] = extra_params[key]
    return base


def _llm_config_full_payload(llm_config: LLMConfig) -> LLMConfigPayload:
    payload = _llm_config_inheritance_base(llm_config)
    payload["api_key"] = llm_config.api_key
    extra_params = getattr(llm_config, "extra_params", None)
    if isinstance(extra_params, dict):
        payload.update(extra_params)
    return payload


def _runtime_base_url(*, base_url: object, api_type: str) -> object:
    if not isinstance(base_url, str) or api_type != "anthropic_chat_completion":
        return base_url
    normalized = base_url.rstrip("/")
    if not normalized.endswith("/v1"):
        return base_url
    return normalized[: -len("/v1")]
