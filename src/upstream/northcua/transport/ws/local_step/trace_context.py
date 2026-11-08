"""Trace helpers for local websocket model steps."""

from __future__ import annotations

import logging

from nexau.archs.main_sub.agent import Agent
from nexau.archs.tracer.context import TraceContext
from nexau.archs.tracer.core import SpanType
from nexau.core.messages import Message

from northcua.agentinfra.providers.provider_payloads import build_provider_request_payload
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject

logger = logging.getLogger(__name__)


def _llm_span_name_for_api_type(api_type: str | None) -> str:
    """Return a provider-specific LLM span name for tracer enrichment."""

    if api_type == "openai_responses":
        return "openai.responses.create"
    if api_type == "openai_chat_completion":
        return "openai.chat.completions.create"
    if api_type == "anthropic_chat_completion":
        return "anthropic.messages.create"
    if api_type == "google_generate_content":
        return "google.generateContent"
    return "llm.call"


def _trace_inputs_for_local_step(
    *,
    agent: Agent,
    messages: list[Message],
    tools_payload: list[JsonObject] | None,
) -> JsonObject:
    """Build a provider-facing request snapshot for one local stepwise LLM span."""

    try:
        prepared = build_provider_request_payload(
            llm_config=agent.config.llm_config,
            messages=messages,
            provider_tools=tools_payload or None,
            parallel_tool_calls=True,
        )
        return prepared.request_payload
    except Exception as exc:  # pragma: no cover - defensive fallback
        log_runtime_event(
            logger,
            "transport_local_step_trace_payload_fallback",
            level=logging.DEBUG,
            api_type=getattr(agent.config.llm_config, "api_type", None),
            error=str(exc),
        )
        return {}


def _build_llm_trace_context(
    *,
    tracer: object | None,
    agent: Agent,
    messages: list[Message],
    tools_payload: list[JsonObject] | None,
    context: JsonObject | None = None,
) -> TraceContext | None:
    """Create a TraceContext for one local stepwise LLM call when tracing is enabled."""

    if tracer is None:
        return None
    trace_inputs = _trace_inputs_for_local_step(agent=agent, messages=messages, tools_payload=tools_payload)
    trace_attributes: JsonObject = {
        "agent_name": agent.agent_name,
        "agent_id": agent.agent_id,
        "model": getattr(agent.config.llm_config, "model", ""),
        "api_type": getattr(agent.config.llm_config, "api_type", ""),
    }
    trace_attributes.update(_trace_attributes_from_context(context))
    return TraceContext(
        tracer,
        _llm_span_name_for_api_type(getattr(agent.config.llm_config, "api_type", None)),
        SpanType.LLM,
        trace_inputs,
        trace_attributes,
    )


def _trace_attributes_from_context(context: JsonObject | None) -> JsonObject:
    """Extract compact session/round attributes from a runtime context."""

    if not context:
        return {}
    allowed_keys = (
        "agent_kind",
        "artifact_dir",
        "experiment_id",
        "northcua_session_id",
        "query_index",
        "round_num",
        "round_step",
        "session_id",
        "session_step",
        "user_query",
    )
    attributes: JsonObject = {}
    for key in allowed_keys:
        if key not in context:
            continue
        value = _trace_attribute_value(context[key])
        if value is not None:
            attributes[key] = value
    return attributes


def _trace_attribute_value(value: object) -> str | int | float | bool | None:
    """Return a Langfuse-safe scalar attribute value."""

    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, (bool, int, float)):
        return value
    return None


def _safe_model_dump(payload: object | None) -> JsonObject:
    """Best-effort dict conversion for provider response payloads."""

    if isinstance(payload, dict):
        return dict(payload)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        if isinstance(dumped, dict):
            return dict(dumped)
    return {}


def _usage_payload(usage: object | None) -> JsonObject:
    """Normalize token usage into a JSON object for tracer outputs."""

    if usage is None:
        return {}
    to_dict = getattr(usage, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, dict):
            return dict(dumped)
    if isinstance(usage, dict):
        return dict(usage)
    return {}


__all__ = [
    "_build_llm_trace_context",
    "_llm_span_name_for_api_type",
    "_safe_model_dump",
    "_trace_attribute_value",
    "_trace_attributes_from_context",
    "_trace_inputs_for_local_step",
    "_usage_payload",
]
