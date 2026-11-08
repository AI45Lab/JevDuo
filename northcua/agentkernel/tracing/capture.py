"""Hook-based request/response/tool trace capture for NexAU execution."""

from __future__ import annotations

import json
import logging

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    AfterModelHookResult,
    AfterToolHookInput,
    AfterToolHookResult,
    BeforeModelHookInput,
    BeforeModelHookResult,
)

from northcua.agentinfra.providers.provider_payloads import (
    build_provider_request_payload,
    count_provider_images,
    prepare_runtime_messages_for_provider,
)
from northcua.agentinfra.providers.provider_profiles import provider_family_from_api_type
from northcua.agentkernel.json_types import JsonObject, ProviderToolSpecs
from northcua.agentkernel.tracing.assistant_text import _extract_assistant_text
from northcua.agentkernel.tracing.fallback_payload import build_openai_responses_fallback_payload
from northcua.agentkernel.tracing.payload_dump import _safe_model_dump
from northcua.agentkernel.tracing.state import ProviderRequestPayload, TraceCaptureState
from northcua.agentkernel.tracing.tool_schema import legacy_trace_tool_definitions
from northcua.agentkernel.tracing.tracer_enrichment import TraceCaptureTracerEnricher
from northcua.app.logging import log_runtime_event
from northcua.core.models import AssistantToolCallRecord, LLMTraceRecord, ToolExecutionRecord

logger = logging.getLogger(__name__)


class TraceCaptureService:
    """Build hook callables that capture provider-facing request/response/tool data."""

    def __init__(self, llm_config: LLMConfig) -> None:
        """Initialize trace capture for one LLM configuration.

        Args:
            llm_config: NexAU LLM configuration used by the captured run.
        """

        self.llm_config = llm_config
        self._tracer_enricher = TraceCaptureTracerEnricher()

    @property
    def provider_family_value(self) -> str:
        """Return the configured provider family used when no per-step state exists.

        Returns:
            Provider family value derived from the configured API type.
        """

        return provider_family_from_api_type(self.llm_config.api_type).value

    def build_hooks(self, state: TraceCaptureState) -> tuple[list[object], list[object], list[object]]:
        """Return before-model, after-model, and after-tool hooks.

        Args:
            state: Mutable capture state shared by the generated hooks.

        Returns:
            Hook lists in NexAU before-model, after-model, after-tool order.
        """

        return (
            [lambda hook_input: self._before_model(hook_input, state)],
            [lambda hook_input: self._after_model(hook_input, state)],
            [lambda hook_input: self._after_tool(hook_input, state)],
        )

    def build_trace_record(self, state: TraceCaptureState, cost: float = 0.0) -> LLMTraceRecord:
        """Convert captured state into one persisted trace record.

        Args:
            state: Mutable capture state filled by hooks.
            cost: Fallback cost when the state did not capture one.

        Returns:
            Persistable LLM trace record.
        """

        return LLMTraceRecord(
            request=state.request_payload,
            response=state.response_payload,
            usage=state.usage,
            stop_reason=state.stop_reason,
            cost=state.cost or cost,
            reasoning=state.reasoning,
            reasoning_encrypted_content=state.reasoning_encrypted_content,
            assistant_text=state.assistant_text,
            assistant_tool_calls=list(state.assistant_tool_calls),
            tool_executions=list(state.tool_executions),
        )

    def snapshot_tracer(self, tracer: object | None) -> set[str]:
        """Capture the current span ids so later we can diff newly created spans.

        Args:
            tracer: Configured tracer object, if any.

        Returns:
            Existing in-memory span ids, or an empty set when unavailable.
        """

        return self._tracer_enricher.snapshot_tracer(tracer)

    def new_tracer_span_ids(self, tracer: object | None, baseline_span_ids: set[str]) -> set[str]:
        """Return in-memory tracer span ids created after a model-call snapshot.

        Args:
            tracer: NexAU tracer attached to the running agent, if any.
            baseline_span_ids: Span ids captured before the model call began.

        Returns:
            Span ids that appeared after the baseline snapshot.
        """

        return self._tracer_enricher.new_span_ids(tracer, baseline_span_ids)

    def new_tracer_span_ids_best_effort(self, tracer: object | None, baseline_span_ids: set[str]) -> set[str]:
        """Return new tracer span ids without affecting model execution.

        Args:
            tracer: NexAU tracer attached to the running agent, if any.
            baseline_span_ids: Span ids captured before the model call began.

        Returns:
            New span ids, or an empty set when tracer inspection fails.
        """

        try:
            return self.new_tracer_span_ids(tracer, baseline_span_ids)
        except Exception as exc:  # pragma: no cover - defensive guard for diagnostics-only cleanup
            log_runtime_event(
                logger,
                "trace_capture_tracer_span_snapshot_failed",
                level=logging.WARNING,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return set()

    def enrich_from_tracer(self, state: TraceCaptureState, tracer: object | None, baseline_span_ids: set[str]) -> None:
        """Prefer tracer-derived final request/response payloads when available.

        Args:
            state: Mutable capture state to enrich.
            tracer: Configured tracer object, if any.
            baseline_span_ids: Span ids present before the captured run.
        """

        self._tracer_enricher.enrich_from_tracer(
            state=state,
            tracer=tracer,
            baseline_span_ids=baseline_span_ids,
            fallback_api_type=self.llm_config.api_type,
        )

    def enrich_from_tracer_span_ids(
        self,
        state: TraceCaptureState,
        tracer: object | None,
        span_ids: set[str],
    ) -> None:
        """Prefer tracer-derived final request/response payloads from exact spans.

        Args:
            state: Mutable capture state to enrich in place.
            tracer: NexAU tracer attached to the running agent, if any.
            span_ids: Exact span ids to inspect for final LLM payloads.
        """

        self._tracer_enricher.enrich_from_tracer_span_ids(
            state=state,
            tracer=tracer,
            span_ids=span_ids,
            fallback_api_type=self.llm_config.api_type,
        )

    def clear_tracer_spans(self, tracer: object | None, span_ids: set[str]) -> int:
        """Release an exact set of in-memory tracer spans.

        Args:
            tracer: Configured tracer object, if any.
            span_ids: In-memory tracer span ids to clear.

        Returns:
            Number of spans removed from the in-memory tracer.
        """

        return self._tracer_enricher.clear_tracer_spans(tracer, span_ids)

    def clear_tracer_spans_best_effort(self, tracer: object | None, span_ids: set[str]) -> int:
        """Release in-memory tracer spans without affecting model execution.

        Args:
            tracer: NexAU tracer attached to the running agent, if any.
            span_ids: Span ids to prune from in-memory tracer storage.

        Returns:
            Number of spans removed, or zero when pruning fails.
        """

        try:
            return self.clear_tracer_spans(tracer, span_ids)
        except Exception as exc:  # pragma: no cover - defensive guard for diagnostics-only cleanup
            log_runtime_event(
                logger,
                "trace_capture_tracer_span_prune_failed",
                level=logging.WARNING,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return 0

    def clear_new_tracer_spans(self, tracer: object | None, baseline_span_ids: set[str]) -> int:
        """Release in-memory tracer spans created after a model-call snapshot.

        Args:
            tracer: Configured tracer object, if any.
            baseline_span_ids: Span ids present before the captured run.

        Returns:
            Number of spans removed from the in-memory tracer.
        """

        return self._tracer_enricher.clear_new_tracer_spans(tracer, baseline_span_ids)

    def _before_model(self, hook_input: BeforeModelHookInput, state: TraceCaptureState) -> BeforeModelHookResult:
        """Capture and optionally rewrite provider-bound model input.

        Args:
            hook_input: NexAU before-model hook input.
            state: Mutable capture state for this run.

        Returns:
            Hook result preserving runtime behavior unless provider preparation
            rewrites messages.
        """

        state.llm_config = self.llm_config
        tool_defs: ProviderToolSpecs = []
        tool_registry = getattr(hook_input.agent_state, "_tool_registry", None)
        eager_tools = tool_registry.compute_eager_tools() if tool_registry is not None else []
        tool_defs = [tool.to_structured_definition() for tool in eager_tools]
        state.trace_tool_definitions = legacy_trace_tool_definitions(tool_defs or state.provider_tools)
        provider_family = provider_family_from_api_type(self.llm_config.api_type)
        runtime_messages = prepare_runtime_messages_for_provider(
            llm_config=self.llm_config,
            messages=list(hook_input.messages),
        )
        messages_changed = runtime_messages != hook_input.messages
        try:
            # This is a trace/logging snapshot built from the same provider
            # adapter contract as runtime execution. The live provider client
            # still owns the final transport payload, auth fields, and send.
            prepared = build_provider_request_payload(
                llm_config=self.llm_config,
                messages=runtime_messages,
                tool_definitions=tool_defs or None,
                provider_tools=state.provider_tools or None,
                tool_choice=state.provider_tool_choice,
                parallel_tool_calls=True,
            )
            request_payload: ProviderRequestPayload = prepared.request_payload
            provider_family = prepared.provider_family
            state.provider_family = prepared.provider_family.value
        except Exception as exc:
            state.trace_error = str(exc)
            request_payload = build_openai_responses_fallback_payload(
                llm_config=self.llm_config,
                messages=runtime_messages,
                state=state,
                tool_defs=tool_defs,
            )
            state.provider_family = provider_family.value
            log_runtime_event(
                logger,
                "trace_capture_provider_payload_fallback",
                level=logging.WARNING,
                api_type=self.llm_config.api_type,
                error=str(exc),
                fallback_provider_family=provider_family.value,
            )
        if state.trace_tool_definitions:
            # Private NexAU persisted the neutral ToolDefinition shape in the
            # main trace even though the actual Responses wire request was
            # provider-flattened. Keep this durable artifact contract separate
            # from the real LLMCaller send boundary.
            request_payload["tools"] = list(state.trace_tool_definitions)
        state.request_payload = request_payload
        request_items = request_payload.get("input")
        if not isinstance(request_items, list):
            request_items = request_payload.get("messages")
        request_item_count = len(request_items) if isinstance(request_items, list) else 0
        request_size_bytes = len(json.dumps(request_payload, ensure_ascii=False))
        log_runtime_event(
            logger,
            "trace_capture_before_model",
            level=logging.DEBUG,
            message_count=len(hook_input.messages),
            input_item_count=request_item_count,
            image_count=count_provider_images(request_payload, provider_family=provider_family),
            tool_count=len(request_payload.get("tools") or []),
            has_instructions=bool(request_payload.get("instructions") or request_payload.get("system")),
            tool_choice=request_payload.get("tool_choice"),
            api_type=self.llm_config.api_type,
            provider_family=provider_family.value,
            request_size_bytes=request_size_bytes,
            trace_error=state.trace_error,
            runtime_messages_rewritten=messages_changed,
        )
        if messages_changed:
            # The hook may rewrite runtime messages only when provider
            # normalization is required before the actual model call. The
            # logged request payload above remains a diagnostic snapshot, not
            # a second source of runtime truth.
            return BeforeModelHookResult.with_modifications(messages=runtime_messages)
        return BeforeModelHookResult.no_changes()

    def _after_model(self, hook_input: AfterModelHookInput, state: TraceCaptureState) -> AfterModelHookResult:
        """Capture model response, usage, reasoning, and tool calls.

        Args:
            hook_input: NexAU after-model hook input.
            state: Mutable capture state for this run.

        Returns:
            A no-change hook result; capture must not alter model output.
        """

        model_response = hook_input.model_response
        if model_response is None:
            return AfterModelHookResult.no_changes()

        state.response_payload = _safe_model_dump(model_response.raw_message)
        state.assistant_text = _extract_assistant_text(
            content=getattr(model_response, "content", None),
            original_response=hook_input.original_response,
            response_payload=state.response_payload,
        )
        state.reasoning = model_response.reasoning_content or ""
        usage_payload = getattr(model_response, "usage", None)
        if usage_payload is not None:
            to_dict = getattr(usage_payload, "to_dict", None)
            if callable(to_dict):
                state.usage = {key: int(value) for key, value in to_dict().items() if isinstance(value, int)}
            elif isinstance(usage_payload, dict):
                state.usage = {key: int(value) for key, value in usage_payload.items() if isinstance(value, int)}
        if isinstance(state.response_payload, dict):
            raw_cost = state.response_payload.get("cost")
            if isinstance(raw_cost, (int, float)):
                state.cost = float(raw_cost)
        if isinstance(state.response_payload, dict):
            response_items = state.response_payload.get("output")
            if isinstance(response_items, list):
                for item in response_items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "reasoning" and item.get("encrypted_content"):
                        state.reasoning_encrypted_content = str(item.get("encrypted_content") or "")
                        break
        state.assistant_tool_calls = [
            AssistantToolCallRecord(
                id=call.call_id or "",
                name=call.name,
                arguments=call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False),
                type=call.call_type,
            )
            for call in model_response.tool_calls
        ]
        finish_reason = state.response_payload.get("stop_reason") if isinstance(state.response_payload, dict) else None
        state.stop_reason = str(finish_reason) if finish_reason else None
        response_size_bytes = (
            len(json.dumps(state.response_payload, ensure_ascii=False))
            if isinstance(state.response_payload, dict)
            else 0
        )
        log_runtime_event(
            logger,
            "trace_capture_after_model",
            level=logging.DEBUG,
            assistant_text_length=len(state.assistant_text),
            tool_call_count=len(state.assistant_tool_calls),
            stop_reason=state.stop_reason,
            usage=state.usage,
            cost=state.cost,
            response_output_count=(
                len(state.response_payload.get("output") or []) if isinstance(state.response_payload, dict) else 0
            ),
            response_size_bytes=response_size_bytes,
        )
        return AfterModelHookResult.no_changes()

    def _after_tool(self, hook_input: AfterToolHookInput, state: TraceCaptureState) -> AfterToolHookResult:
        """Capture one tool execution observed by NexAU.

        Args:
            hook_input: NexAU after-tool hook input.
            state: Mutable capture state for this run.

        Returns:
            A no-change hook result; capture must not alter tool output.
        """

        tool_output = hook_input.tool_output
        llm_tool_output = hook_input.llm_tool_output
        state.tool_executions.append(
            ToolExecutionRecord(
                tool_call_id=hook_input.tool_call_id,
                tool_name=hook_input.tool_name,
                tool_input=dict(hook_input.tool_input or {}),
                tool_output=(
                    tool_output if isinstance(tool_output, (dict, str)) or tool_output is None else str(tool_output)
                ),
                llm_tool_output=(
                    llm_tool_output
                    if isinstance(llm_tool_output, (dict, str)) or llm_tool_output is None
                    else str(llm_tool_output)
                ),
                is_error=bool(isinstance(tool_output, dict) and tool_output.get("status") == "error"),
            )
        )
        log_runtime_event(
            logger,
            "trace_capture_after_tool",
            level=logging.DEBUG,
            tool_name=hook_input.tool_name,
            tool_call_id=hook_input.tool_call_id,
            is_error=bool(isinstance(tool_output, dict) and tool_output.get("status") == "error"),
            tool_input=hook_input.tool_input or {},
            has_llm_tool_output=llm_tool_output is not None,
        )
        return AfterToolHookResult.no_changes()


AgentTraceCaptureState = TraceCaptureState
AgentTraceCaptureService = TraceCaptureService
