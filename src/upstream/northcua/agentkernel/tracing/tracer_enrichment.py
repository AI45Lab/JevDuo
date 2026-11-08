"""Trace capture enrichment from NexAU tracer spans."""

from __future__ import annotations

import logging

from nexau.archs.tracer.adapters.in_memory import InMemoryTracer
from nexau.archs.tracer.composite import CompositeTracer
from nexau.archs.tracer.core import Span

from northcua.agentinfra.providers.provider_payloads import count_provider_images
from northcua.agentinfra.providers.provider_profiles import provider_family_from_api_type
from northcua.app.logging import log_runtime_event

from .state import TraceCaptureState
from .tool_schema import legacy_trace_tool_definitions

logger = logging.getLogger(__name__)


class TraceCaptureTracerEnricher:
    """Enrich trace capture state from spans created by NexAU tracers."""

    def snapshot_tracer(self, tracer: object | None) -> set[str]:
        """Capture current span ids so later enrichment can diff new spans.

        Args:
            tracer: Configured NexAU tracer object, if any.

        Returns:
            In-memory span ids present before a captured model call.
        """

        return self.current_span_ids(tracer)

    def current_span_ids(self, tracer: object | None) -> set[str]:
        """Return all span ids currently retained by in-memory tracers.

        Args:
            tracer: NexAU tracer or composite tracer to inspect.

        Returns:
            Span ids from every nested in-memory tracer.
        """

        span_ids: set[str] = set()
        for in_memory_tracer in self._resolve_in_memory_tracers(tracer):
            span_ids.update(in_memory_tracer.spans.keys())
        return span_ids

    def new_span_ids(self, tracer: object | None, baseline_span_ids: set[str]) -> set[str]:
        """Return span ids retained after ``baseline_span_ids`` were captured.

        Args:
            tracer: NexAU tracer or composite tracer to inspect.
            baseline_span_ids: Span ids present before the model call.

        Returns:
            Span ids that were added after the baseline snapshot.
        """

        return self.current_span_ids(tracer) - baseline_span_ids

    def enrich_from_tracer(
        self,
        *,
        state: TraceCaptureState,
        tracer: object | None,
        baseline_span_ids: set[str],
        fallback_api_type: str,
    ) -> None:
        """Prefer tracer-derived final request/response payloads when available.

        Args:
            state: Mutable capture state to enrich.
            tracer: Configured NexAU tracer object, if any.
            baseline_span_ids: Span ids observed before the captured model call.
            fallback_api_type: API type used when the state has not recorded a
                provider family yet.
        """

        self.enrich_from_tracer_span_ids(
            state=state,
            tracer=tracer,
            span_ids=self.new_span_ids(tracer, baseline_span_ids),
            fallback_api_type=fallback_api_type,
        )

    def enrich_from_tracer_span_ids(
        self,
        *,
        state: TraceCaptureState,
        tracer: object | None,
        span_ids: set[str],
        fallback_api_type: str,
    ) -> None:
        """Prefer tracer-derived payloads from an explicit span-id set.

        Args:
            state: Mutable capture state to update with final payloads.
            tracer: NexAU tracer or composite tracer to inspect.
            span_ids: Exact span ids that belong to the captured model call.
            fallback_api_type: API type used when state lacks provider metadata.
        """

        new_spans = self._spans_for_ids(tracer, span_ids)
        latest_span = self._select_final_llm_span(new_spans)
        if latest_span is None:
            log_runtime_event(
                logger,
                "trace_capture_tracer_enrichment_skipped",
                level=logging.DEBUG,
                reason="no_final_llm_span",
                new_span_count=len(new_spans),
            )
            return
        if latest_span.inputs:
            request_payload = dict(latest_span.inputs)
            if self._should_replace_request_payload(
                current=state.request_payload,
                candidate=request_payload,
            ):
                candidate_tools = request_payload.get("tools")
                if isinstance(candidate_tools, list) and candidate_tools:
                    # The final provider span is the source of truth for live
                    # transport fields, but public NexAU changed only the span's
                    # tool representation. Preserve the hook-captured neutral
                    # schema so llm_trace.jsonl stays private-pin compatible.
                    request_payload["tools"] = state.trace_tool_definitions or legacy_trace_tool_definitions(
                        candidate_tools
                    )
                state.request_payload = request_payload
            else:
                log_runtime_event(
                    logger,
                    "trace_capture_tracer_request_preserved",
                    level=logging.DEBUG,
                    span_name=latest_span.name,
                    reason="candidate_reinjects_tool_images",
                )
        if latest_span.outputs:
            response_payload = latest_span.outputs.get("result")
            if isinstance(response_payload, dict):
                state.response_payload = response_payload
            elif state.response_payload == {}:
                state.response_payload = latest_span.outputs
        if isinstance(state.response_payload, dict):
            usage = state.response_payload.get("usage")
            if isinstance(usage, dict) and not state.usage:
                state.usage = {key: int(value) for key, value in usage.items() if isinstance(value, int)}
            stop_reason = state.response_payload.get("stop_reason")
            if isinstance(stop_reason, str) and not state.stop_reason:
                state.stop_reason = stop_reason
        log_runtime_event(
            logger,
            "trace_capture_tracer_enriched",
            level=logging.DEBUG,
            span_name=latest_span.name,
            request_has_inputs=bool(latest_span.inputs),
            response_has_outputs=bool(latest_span.outputs),
            image_count=count_provider_images(
                state.request_payload,
                provider_family=provider_family_from_api_type(state.provider_family or fallback_api_type),
            ),
        )

    def clear_tracer_spans(self, tracer: object | None, span_ids: set[str]) -> int:
        """Drop an explicit set of span ids from all in-memory tracers.

        Args:
            tracer: NexAU tracer or composite tracer to mutate.
            span_ids: Root span ids to prune, including descendants.

        Returns:
            Number of retained spans removed from tracer memory.
        """

        if not span_ids:
            return 0
        removed_count = 0
        for in_memory_tracer in self._resolve_in_memory_tracers(tracer):
            removed_count += self._clear_spans_from_in_memory_tracer(in_memory_tracer, span_ids)
        if removed_count == 0:
            return 0
        log_runtime_event(
            logger,
            "trace_capture_tracer_spans_pruned",
            level=logging.DEBUG,
            cleared_span_count=removed_count,
            retained_span_count=sum(len(item.spans) for item in self._resolve_in_memory_tracers(tracer)),
        )
        return removed_count

    def clear_new_tracer_spans(self, tracer: object | None, baseline_span_ids: set[str]) -> int:
        """Drop in-memory spans captured after ``baseline_span_ids``.

        The in-memory tracer is only needed long enough to enrich the persisted
        LLM trace. Provider inputs can include large inline images, so retaining
        completed spans for the full worker lifetime can make long tasks grow
        RSS by hundreds of MiB. Baseline spans are preserved for callers that
        installed their own tracer before this model call.

        Args:
            tracer: NexAU tracer or composite tracer to mutate.
            baseline_span_ids: Span ids present before the model call.

        Returns:
            Number of spans pruned from tracer memory.
        """

        return self.clear_tracer_spans(tracer, self.new_span_ids(tracer, baseline_span_ids))

    @classmethod
    def _spans_for_ids(cls, tracer: object | None, span_ids: set[str]) -> list[Span]:
        spans: list[Span] = []
        if not span_ids:
            return spans
        for in_memory_tracer in cls._resolve_in_memory_tracers(tracer):
            spans.extend(span for span_id, span in in_memory_tracer.spans.items() if span_id in span_ids)
        return spans

    @staticmethod
    def _clear_spans_from_in_memory_tracer(in_memory_tracer: InMemoryTracer, span_ids_to_clear: set[str]) -> int:
        if not span_ids_to_clear:
            return 0
        existing_span_ids = set(in_memory_tracer.spans).intersection(span_ids_to_clear)
        if not existing_span_ids:
            return 0
        pending_descendant_scan = list(existing_span_ids)
        while pending_descendant_scan:
            parent_id = pending_descendant_scan.pop()
            for child_id in in_memory_tracer.children.get(parent_id, []):
                if child_id in in_memory_tracer.spans and child_id not in existing_span_ids:
                    existing_span_ids.add(child_id)
                    pending_descendant_scan.append(child_id)

        for span_id in existing_span_ids:
            in_memory_tracer.spans.pop(span_id, None)
            in_memory_tracer.children.pop(span_id, None)

        in_memory_tracer.root_spans[:] = [
            span_id for span_id in in_memory_tracer.root_spans if span_id not in existing_span_ids
        ]
        for parent_id, child_ids in list(in_memory_tracer.children.items()):
            retained_children = [child_id for child_id in child_ids if child_id not in existing_span_ids]
            if retained_children and parent_id in in_memory_tracer.spans:
                in_memory_tracer.children[parent_id] = retained_children
            else:
                in_memory_tracer.children.pop(parent_id, None)
        return len(existing_span_ids)

    @classmethod
    def _resolve_in_memory_tracers(cls, tracer: object | None) -> list[InMemoryTracer]:
        """Find every in-memory tracer inside possible nested composites."""

        if isinstance(tracer, InMemoryTracer):
            return [tracer]
        if isinstance(tracer, CompositeTracer):
            tracers: list[InMemoryTracer] = []
            for inner in tracer.tracers:
                tracers.extend(cls._resolve_in_memory_tracers(inner))
            return tracers
        return []

    @staticmethod
    def _resolve_in_memory_tracer(tracer: object | None) -> InMemoryTracer | None:
        """Find an in-memory tracer inside a possible composite tracer."""

        tracers = TraceCaptureTracerEnricher._resolve_in_memory_tracers(tracer)
        return tracers[0] if tracers else None

    @staticmethod
    def _should_replace_request_payload(*, current: object, candidate: object) -> bool:
        """Return whether tracer inputs should replace the captured request.

        The before-model hook builds a provider payload with NorthCUA-specific
        message policy applied. Some NexAU tracer spans still serialize
        tool-result images using the historical chat fallback shape: a text-only
        tool message followed by an extra user image message. For trace/replay
        parity we keep the hook-built request when it already embeds tool images
        and the tracer candidate would re-inject them as user messages.
        """

        if not isinstance(current, dict) or not current:
            return True
        if not isinstance(candidate, dict) or not candidate:
            return False
        if TraceCaptureTracerEnricher._has_embedded_tool_images(
            current,
        ) and TraceCaptureTracerEnricher._has_injected_tool_image_user_messages(candidate):
            return False
        return True

    @staticmethod
    def _openai_chat_messages(payload: dict[object, object]) -> list[object]:
        messages = payload.get("messages")
        return list(messages) if isinstance(messages, list) else []

    @staticmethod
    def _has_embedded_tool_images(payload: dict[object, object]) -> bool:
        for message in TraceCaptureTracerEnricher._openai_chat_messages(payload):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if any(TraceCaptureTracerEnricher._is_image_block(block) for block in content):
                return True
        return False

    @staticmethod
    def _has_injected_tool_image_user_messages(payload: dict[object, object]) -> bool:
        previous_role: object = None
        for message in TraceCaptureTracerEnricher._openai_chat_messages(payload):
            if not isinstance(message, dict):
                previous_role = None
                continue
            role = message.get("role")
            if role == "user" and previous_role == "tool" and TraceCaptureTracerEnricher._message_has_image(message):
                return True
            previous_role = role
        return False

    @staticmethod
    def _message_has_image(message: dict[object, object]) -> bool:
        content = message.get("content")
        if isinstance(content, str):
            return "<image>" in content
        if not isinstance(content, list):
            return False
        return any(TraceCaptureTracerEnricher._is_image_block(block) for block in content)

    @staticmethod
    def _is_image_block(block: object) -> bool:
        if not isinstance(block, dict):
            return False
        return block.get("type") in {"image_url", "input_image", "image"} or "image_url" in block

    @staticmethod
    def _select_final_llm_span(spans: list[Span]) -> Span | None:
        """Choose the most provider-final LLM span from newly created spans.

        Args:
            spans: Candidate spans created during the captured run.

        Returns:
            The most specific final LLM span, or None when no LLM span exists.
        """

        llm_spans = [span for span in spans if span.type.value == "LLM"]
        if not llm_spans:
            return None

        def sort_key(span: Span) -> tuple[int, int, float, float]:
            """Rank spans by provider specificity, outputs, and recency.

            Args:
                span: Candidate LLM span created during the captured run.

            Returns:
                Tuple ordered so the final provider-specific span sorts last.
            """

            name = span.name.lower()
            provider_specific = int(
                "responses" in name
                or "chat.completions" in name
                or "messages.create" in name
                or "generatecontent" in name
            )
            has_outputs = int(bool(span.outputs))
            return (
                provider_specific,
                has_outputs,
                span.end_time or span.start_time,
                span.start_time,
            )

        llm_spans.sort(key=sort_key)
        return llm_spans[-1]


__all__ = ["TraceCaptureTracerEnricher"]
