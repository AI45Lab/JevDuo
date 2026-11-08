"""NexAU-backed typed summary client with bounded calls and validation."""

from __future__ import annotations

import asyncio
import copy
import inspect
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.execution.llm_caller import LLMCaller
from nexau.core.messages import Message, Role, TextBlock

from northcua.agentinfra.context.adapters.ump import NexAUTokenEstimator
from northcua.agentinfra.context.summary import BoundedOutputReserve as _BoundedOutputReserve
from northcua.agentinfra.context.summary import bounded_output_reserve as _bounded_output_reserve
from northcua.agentinfra.context.summary import chunk_segments as _chunk_segments
from northcua.agentinfra.context.summary import document_source_ids as _document_source_ids
from northcua.agentinfra.context.summary import minimum_source_characters as _minimum_source_characters
from northcua.agentinfra.context.summary import parse_summary_document as _parse_summary_document
from northcua.agentinfra.context.summary import rebalance_short_chunks as _rebalance_short_chunks
from northcua.agentinfra.context.summary import summary_messages as _summary_messages
from northcua.agentinfra.context.summary import (
    summary_prompt_fingerprint,
)
from northcua.agentinfra.context.summary import summary_repair_message as _summary_repair_message
from northcua.agentinfra.context.summary import (
    validate_and_expand_source_references as _validate_and_expand_source_references,
)
from northcua.agentinfra.context.summary import validate_output_safety as _validate_output_safety
from northcua.agentinfra.context.summary import with_bounded_source_markers as _with_bounded_source_markers
from northcua.agentinfra.nexau_compat.adapters.providers import (
    LegacyLLMCallRequest,
    NexAUProviderAdapter,
)
from northcua.agentinfra.providers.llm_fallback import runtime_llm_config
from northcua.agentkernel.context.domain.config import ContextSummaryConfig
from northcua.agentkernel.context.domain.enums import ContextSegmentKind
from northcua.agentkernel.context.domain.models import (
    ContextSummaryDocumentV1,
    SummaryCompressionContract,
    SummaryInvocationCallRecord,
    SummaryInvocationRequest,
    SummaryInvocationResult,
    SummarySourceSegment,
)
from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy
from northcua.agentkernel.context.strategies import ContextSummaryError, SummaryCompressionStrategy
from northcua.agentkernel.json_types import JsonArray, JsonObject, JsonValue

_SUMMARY_INPUT_SAFETY_RATIO = 0.08
_SUMMARY_INPUT_SAFETY_MIN_TOKENS = 1024
# JSON control characters such as NUL cost about 3.5 tokens per raw byte in
# NexAU's tiktoken-backed estimator after the segment is serialized into the
# provider message.  Four is the shared, deliberately rounded-up contract used
# by both the lazy coordinator capacity and the concrete client chunker.
_SUMMARY_WORST_CASE_TOKENS_PER_SOURCE_BYTE = 4
_CHECKPOINT_WRAPPER_CHARACTERS = len("<northcua_context_checkpoint>\n") + len("\n</northcua_context_checkpoint>")
_MINIMUM_NONEMPTY_DOCUMENT_CHARACTERS = len(ContextSummaryDocumentV1(goal=".").canonical_prompt_text())
_MINIMUM_NONEMPTY_DOCUMENT_BYTES = len(ContextSummaryDocumentV1(goal=".").canonical_prompt_text().encode("utf-8"))
_COMPRESSION_STRATEGY = SummaryCompressionStrategy(
    wrapper_characters=_CHECKPOINT_WRAPPER_CHARACTERS,
    minimum_document_characters=_MINIMUM_NONEMPTY_DOCUMENT_CHARACTERS,
)


@dataclass(slots=True)
class _CallState:
    calls: int = 0
    call_limit: int = 1
    repairs_remaining: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    requests: JsonArray = field(default_factory=list)
    responses: JsonArray = field(default_factory=list)
    call_records: list[SummaryInvocationCallRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _MergeNode:
    document: ContextSummaryDocumentV1
    source_ids: tuple[str, ...]


class NexAUSummaryClient:
    """Use NexAU's provider gateway without depending on its compaction middleware."""

    def __init__(
        self,
        *,
        primary_llm_config: LLMConfig,
        openai_client: object,
        async_openai_client: object | None,
        owned_clients: tuple[object, ...] = (),
        session_id: str,
        config: ContextSummaryConfig,
        policy: ContextSummaryPolicy,
        context_limit_tokens: int = 128_000,
    ) -> None:
        self._config = config
        self._policy = policy
        self._llm_config = _derive_summary_llm_config(primary_llm_config, config)
        self._provider_adapter = NexAUProviderAdapter()
        self._caller = self._provider_adapter.create_llm_caller(
            openai_client=openai_client,
            async_openai_client=async_openai_client,
            llm_config=self._llm_config,
            retry_attempts=1,
            retry_backoff_max_seconds=2,
            # Summary invocations have their own durable trace. Reusing the primary
            # agent storage would expose summary spans to main-trace enrichment.
            global_storage=None,
            session_id=f"{session_id}:context-summary",
        )
        self._closed = False
        self._owned_clients = owned_clients
        base_input_capacity = context_limit_tokens - config.max_output_tokens
        safety_margin = max(
            _SUMMARY_INPUT_SAFETY_MIN_TOKENS,
            int(base_input_capacity * _SUMMARY_INPUT_SAFETY_RATIO),
        )
        self._hard_input_budget = base_input_capacity - safety_margin
        if self._hard_input_budget <= 0:
            raise ValueError(
                "summary model context window is too small for output reserve and safety margin "
                f"({context_limit_tokens=}, max_output_tokens={config.max_output_tokens})"
            )
        self._input_estimator = NexAUTokenEstimator(model=str(self._llm_config.model or "gpt-4o"))
        # A checkpoint is nested as JSON text in the merge request, where
        # backslashes/quotes can double its wire size. Reserving at most one
        # tenth of the input budget per document leaves room for two worst-
        # case escaped partials plus the stable prompt/schema envelope.
        self._document_byte_limit = min(16_384, max(256, self._hard_input_budget // 10))

    async def summarize(self, request: SummaryInvocationRequest) -> SummaryInvocationResult:
        if self._closed:
            raise RuntimeError("summary client is closed")
        started = time.monotonic()
        state = _CallState(
            call_limit=min(
                self._config.max_calls,
                request.max_calls if request.max_calls > 0 else self._config.max_calls,
            ),
            repairs_remaining=self._config.repair_attempts,
        )
        try:
            return await self._summarize_with_state(request=request, started=started, state=state)
        except BaseException as exc:
            # Coordinator persists these physical calls even when orchestration
            # fails after one or more successful chunks.
            setattr(exc, "northcua_summary_call_records", tuple(state.call_records))
            setattr(exc, "northcua_summary_model", str(self._llm_config.model or ""))
            setattr(exc, "northcua_summary_provider", str(self._llm_config.api_type or ""))
            raise

    @property
    def source_capacity_bytes(self) -> int:
        return summary_source_capacity_bytes_from_hard_budget(
            hard_input_budget=self._hard_input_budget,
            max_chunks=self._config.max_chunks,
            max_calls=self._config.max_calls,
            repair_attempts=self._config.repair_attempts,
        )

    async def _summarize_with_state(
        self,
        *,
        request: SummaryInvocationRequest,
        started: float,
        state: _CallState,
    ) -> SummaryInvocationResult:
        output_reserve = _bounded_output_reserve(request.segments)
        if _MINIMUM_NONEMPTY_DOCUMENT_BYTES + output_reserve.bytes > self._document_byte_limit:
            raise ContextSummaryError("bounded-source markers cannot fit the summary document byte limit")
        final_contract = _compression_contract(
            previous_summary=request.previous_summary,
            segments=request.segments,
            minimum_reduction_ratio=self._config.minimum_reduction_ratio,
            reserved_output_characters=output_reserve.characters,
        )
        output_reserve = _BoundedOutputReserve(
            characters=output_reserve.characters,
            bytes=output_reserve.bytes,
            maximum_model_characters=final_contract.maximum_model_canonical_characters,
            final_checkpoint_ceiling_characters=final_contract.maximum_canonical_characters,
        )
        chunks = _chunk_segments(
            request.segments,
            max_chunks=self._config.max_chunks,
            fits=lambda candidate: self._messages_fit(
                # Conservatively reserve the durable previous checkpoint even
                # though multi-chunk leaf calls omit it; a one-chunk plan must
                # include it and the merge call needs equivalent headroom.
                previous_summary=request.previous_summary,
                segments=candidate,
            ),
        )
        if len(chunks) > 1:
            chunks = _rebalance_short_chunks(
                chunks,
                minimum_source_characters=_minimum_source_characters(
                    _COMPRESSION_STRATEGY, self._config.minimum_reduction_ratio
                ),
                fits=lambda candidate: self._messages_fit(
                    previous_summary=request.previous_summary,
                    segments=candidate,
                ),
            )
            # This is intentionally before the first provider call. A paid
            # leaf graph must never discover an impossible short tail later.
            for chunk in chunks:
                _compression_contract(
                    previous_summary=None,
                    segments=chunk,
                    minimum_reduction_ratio=self._config.minimum_reduction_ratio,
                )
        source_segment_ids = frozenset(segment.segment_id for segment in request.segments)
        if len(chunks) == 1:
            document = await self._summarize_payload(
                request=request,
                previous_summary=request.previous_summary,
                segments=chunks[0],
                state=state,
                purpose="context_summary",
                allowed_source_ids=source_segment_ids,
                output_reserve=output_reserve,
            )
        else:
            self._validate_merge_feasibility(request=request, chunks=chunks)
            partials: list[ContextSummaryDocumentV1] = []
            for chunk_index, chunk in enumerate(chunks):
                partials.append(
                    await self._summarize_payload(
                        request=request,
                        previous_summary=None,
                        segments=chunk,
                        state=state,
                        purpose=f"context_summary_chunk_{chunk_index + 1}",
                        allowed_source_ids=frozenset(segment.segment_id for segment in chunk),
                    )
                )
            nodes = [
                _MergeNode(
                    document=partial,
                    source_ids=tuple(
                        sorted(_document_source_ids(partial)) or [segment.segment_id for segment in chunks[index]]
                    ),
                )
                for index, partial in enumerate(partials)
            ]
            document = await self._merge_nodes(
                request=request,
                state=state,
                allowed_source_ids=source_segment_ids,
                nodes=nodes,
                output_reserve=output_reserve,
            )

        document = _with_bounded_source_markers(document, request.segments)
        self._validate_document_size(document)
        _validate_compression(document, final_contract, final_document=True)
        return SummaryInvocationResult(
            invocation_id=request.invocation_id,
            document=document,
            model=str(self._llm_config.model or ""),
            provider=str(self._llm_config.api_type or ""),
            usage=cast(JsonObject, dict(state.usage)),
            request_payload={
                "purpose": "northcua_context_summary",
                "calls": state.requests,
            },
            response_payload={
                "calls": state.responses,
                "summary": cast(JsonObject, document.model_dump(mode="json")),
            },
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            call_records=tuple(state.call_records),
        )

    def _validate_merge_feasibility(
        self,
        *,
        request: SummaryInvocationRequest,
        chunks: tuple[tuple[SummarySourceSegment, ...], ...],
    ) -> None:
        """Reject an impossible call graph before paying for leaf summaries."""

        placeholder_size = self._document_byte_limit
        placeholders = tuple(
            SummarySourceSegment(
                segment_id=f"partial-{index + 1}",
                kind=request.segments[0].kind,
                text="\\" * placeholder_size,
            )
            for index in range(len(chunks))
        )
        pair = placeholders[:2]
        if not self._messages_fit(previous_summary=None, segments=pair):
            raise ContextSummaryError("summary model budget cannot merge two bounded partial checkpoints")
        if request.previous_summary is not None and not self._messages_fit(
            previous_summary=request.previous_summary,
            segments=pair,
        ):
            raise ContextSummaryError(
                "summary model budget cannot merge bounded partial checkpoints with the previous checkpoint"
            )
        if state_call_limit := (request.max_calls if request.max_calls > 0 else self._config.max_calls):
            call_limit = min(self._config.max_calls, state_call_limit)
        else:  # pragma: no cover - Pydantic constrains values to non-negative
            call_limit = self._config.max_calls
        # The repair allowance is global, but it can be consumed by any leaf.
        # Reserve it before the first paid call so merge completion is certain.
        minimum_calls = len(chunks) + 1 + self._config.repair_attempts
        if call_limit < minimum_calls:
            raise ContextSummaryError(
                f"summary call budget cannot complete the planned leaf and merge graph ({call_limit}<{minimum_calls})"
            )
        if self._messages_fit(previous_summary=request.previous_summary, segments=placeholders):
            return
        worst_case_calls = len(chunks) * 2 - 1 + self._config.repair_attempts
        if call_limit < worst_case_calls:
            raise ContextSummaryError(
                f"summary max_calls cannot safely merge worst-case chunk outputs ({call_limit}<{worst_case_calls})"
            )

    async def _merge_nodes(
        self,
        *,
        request: SummaryInvocationRequest,
        state: _CallState,
        allowed_source_ids: frozenset[str],
        nodes: list[_MergeNode],
        output_reserve: _BoundedOutputReserve,
    ) -> ContextSummaryDocumentV1:
        merge_round = 0
        while len(nodes) > 1:
            merge_round += 1
            selected_count = 0
            for count in range(2, len(nodes) + 1):
                candidate = self._merge_segments(nodes[:count], round_num=merge_round)
                previous = request.previous_summary if count == len(nodes) else None
                if self._messages_fit(previous_summary=previous, segments=candidate):
                    selected_count = count
                else:
                    break
            if selected_count < 2:
                raise ContextSummaryError("summary model budget cannot hold two partial checkpoints")
            selected = nodes[:selected_count]
            is_final = selected_count == len(nodes)
            merge_segments = self._merge_segments(selected, round_num=merge_round)
            aliases = {segment.segment_id: selected[index].source_ids for index, segment in enumerate(merge_segments)}
            document = await self._summarize_payload(
                request=request,
                previous_summary=request.previous_summary if is_final else None,
                segments=merge_segments,
                state=state,
                purpose=("context_summary_merge" if merge_round == 1 else f"context_summary_merge_{merge_round}"),
                allowed_source_ids=allowed_source_ids,
                source_aliases=aliases,
                output_reserve=output_reserve if is_final else None,
            )
            merged_sources = tuple(sorted({source for node in selected for source in node.source_ids}))
            nodes = [_MergeNode(document=document, source_ids=merged_sources), *nodes[selected_count:]]
        return nodes[0].document

    @staticmethod
    def _merge_segments(nodes: list[_MergeNode], *, round_num: int) -> tuple[SummarySourceSegment, ...]:
        return tuple(
            SummarySourceSegment(
                segment_id=(f"partial-{index + 1}" if round_num == 1 else f"partial-{round_num}-{index + 1}"),
                kind=ContextSegmentKind.ASSISTANT_TOOL_TURN,
                text=node.document.canonical_prompt_text(),
            )
            for index, node in enumerate(nodes)
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._caller.shutdown_thread_pool()
        for client in self._owned_clients:
            close = getattr(client, "close", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _summarize_payload(
        self,
        *,
        request: SummaryInvocationRequest,
        previous_summary: ContextSummaryDocumentV1 | None,
        segments: tuple[SummarySourceSegment, ...],
        state: _CallState,
        purpose: str,
        allowed_source_ids: frozenset[str],
        source_aliases: Mapping[str, tuple[str, ...]] | None = None,
        output_reserve: _BoundedOutputReserve | None = None,
    ) -> ContextSummaryDocumentV1:
        reserve = output_reserve or _BoundedOutputReserve()
        contract = _compression_contract(
            previous_summary=previous_summary,
            segments=segments,
            minimum_reduction_ratio=self._config.minimum_reduction_ratio,
        )
        if reserve.maximum_model_characters is not None:
            maximum_model_characters = min(
                contract.maximum_model_canonical_characters,
                reserve.maximum_model_characters,
            )
            contract = contract.model_copy(
                update={
                    "reserved_output_characters": reserve.characters,
                    "global_final_ceiling_characters": (
                        reserve.final_checkpoint_ceiling_characters
                        if reserve.final_checkpoint_ceiling_characters is not None
                        else contract.maximum_canonical_characters
                    ),
                    "maximum_model_canonical_characters": maximum_model_characters,
                    "maximum_json_characters": max(
                        0,
                        maximum_model_characters - _CHECKPOINT_WRAPPER_CHARACTERS,
                    ),
                }
            )
        payload = _summary_payload(
            previous_summary=previous_summary,
            segments=segments,
            contract=contract,
        )
        messages = _summary_messages(
            self._policy,
            payload,
            prompt_profile=self._config.prompt_profile,
        )
        bounded_output_tokens = min(
            request.max_output_tokens,
            max(128, self._document_byte_limit // 4),
        )
        try:
            document = await self._invoke_and_validate(
                messages=messages,
                state=state,
                purpose=purpose,
                max_output_tokens=bounded_output_tokens,
            )
            _validate_output_safety(document, profile=self._policy.profile)
            document = _validate_and_expand_source_references(
                document,
                previous_summary=previous_summary,
                allowed_source_ids=allowed_source_ids,
                source_aliases=source_aliases,
            )
            self._validate_document_size(document, reserved_output_bytes=reserve.bytes)
            _validate_compression(document, contract)
            return document
        except (ValueError, ContextSummaryError) as first_error:
            if state.repairs_remaining <= 0:
                raise
            state.repairs_remaining -= 1
            repair_messages = [
                *messages,
                _summary_repair_message(
                    prompt_profile=self._config.prompt_profile,
                    validation_error=f"{type(first_error).__name__}: {first_error}",
                    maximum_canonical_characters=contract.maximum_model_canonical_characters,
                    maximum_json_characters=contract.maximum_json_characters,
                ),
            ]
            document = await self._invoke_and_validate(
                messages=repair_messages,
                state=state,
                purpose=f"{purpose}_repair",
                max_output_tokens=bounded_output_tokens,
            )
            _validate_output_safety(document, profile=self._policy.profile)
            document = _validate_and_expand_source_references(
                document,
                previous_summary=previous_summary,
                allowed_source_ids=allowed_source_ids,
                source_aliases=source_aliases,
            )
            self._validate_document_size(document, reserved_output_bytes=reserve.bytes)
            _validate_compression(document, contract)
            return document

    def _validate_document_size(
        self,
        document: ContextSummaryDocumentV1,
        *,
        reserved_output_bytes: int = 0,
    ) -> None:
        size = len(document.canonical_prompt_text().encode("utf-8")) + reserved_output_bytes
        if size > self._document_byte_limit:
            raise ContextSummaryError(
                f"summary document exceeds the bounded merge representation ({size}>{self._document_byte_limit} bytes)"
            )

    def _messages_fit(
        self,
        *,
        previous_summary: ContextSummaryDocumentV1 | None,
        segments: tuple[SummarySourceSegment, ...],
    ) -> bool:
        contract = _compression_contract(
            previous_summary=previous_summary,
            segments=segments,
            minimum_reduction_ratio=self._config.minimum_reduction_ratio,
            enforce_minimum=False,
        )
        payload = _summary_payload(
            previous_summary=previous_summary,
            segments=segments,
            contract=contract,
        )
        messages = _summary_messages(
            self._policy,
            payload,
            prompt_profile=self._config.prompt_profile,
        )
        return self._input_estimator.estimate(tuple(messages), ()).total_tokens <= self._hard_input_budget

    async def _invoke_and_validate(
        self,
        *,
        messages: list[Message],
        state: _CallState,
        purpose: str,
        max_output_tokens: int,
    ) -> ContextSummaryDocumentV1:
        if state.calls >= state.call_limit:
            raise ContextSummaryError("summary call budget exhausted")
        estimate = self._input_estimator.estimate(tuple(messages), ())
        if estimate.total_tokens > self._hard_input_budget:
            raise ContextSummaryError(
                f"summary provider input exceeds safe model budget ({estimate.total_tokens}>{self._hard_input_budget})"
            )
        state.calls += 1
        request_record: JsonObject = {
            "call_index": state.calls,
            "purpose": purpose,
            "messages": cast(JsonArray, [_trace_message(message) for message in messages]),
            "max_output_tokens": max_output_tokens,
        }
        state.requests.append(request_record)
        shutdown_event = threading.Event()
        call_started = time.monotonic()
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                response = await self._provider_adapter.call_llm_async(
                    self._caller,
                    LegacyLLMCallRequest(
                        messages=messages,
                        api_type=str(self._llm_config.api_type or ""),
                        max_tokens=max_output_tokens,
                        tool_call_mode="structured",
                        tools=None,
                        shutdown_event=shutdown_event,
                    ),
                )
        except BaseException as exc:
            shutdown_event.set()
            state.call_records.append(
                SummaryInvocationCallRecord(
                    call_index=state.calls,
                    purpose=purpose,
                    request=request_record,
                    latency_ms=max(0, int((time.monotonic() - call_started) * 1000)),
                    status="failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            raise
        if response is None or not (response.content or "").strip():
            state.call_records.append(
                SummaryInvocationCallRecord(
                    call_index=state.calls,
                    purpose=purpose,
                    request=request_record,
                    latency_ms=max(0, int((time.monotonic() - call_started) * 1000)),
                    status="failed",
                    error_type="ContextSummaryError",
                    error_message="summary model returned an empty response",
                )
            )
            raise ContextSummaryError("summary model returned an empty response")
        content = (response.content or "").strip()
        usage = response.usage.to_dict()
        _merge_usage(state.usage, usage)
        state.responses.append(
            {
                "call_index": state.calls,
                "purpose": purpose,
                "content": content,
                "usage": cast(JsonObject, usage),
            }
        )
        response_record = cast(JsonObject, state.responses[-1])
        state.call_records.append(
            SummaryInvocationCallRecord(
                call_index=state.calls,
                purpose=purpose,
                request=request_record,
                response=response_record,
                usage=cast(JsonObject, usage),
                latency_ms=max(0, int((time.monotonic() - call_started) * 1000)),
                status="succeeded",
            )
        )
        return _parse_summary_document(content)


def summary_source_capacity_bytes_from_hard_budget(
    *,
    hard_input_budget: int,
    max_chunks: int,
    max_calls: int,
    repair_attempts: int,
) -> int:
    """Conservative raw-source bytes safe under JSON and call-graph expansion."""

    document_limit = min(16_384, max(256, hard_input_budget // 10))
    fixed_envelope_reserve = min(2048, max(1, hard_input_budget // 2))
    # A previous canonical checkpoint is nested as JSON text and consumes up
    # to this bounded document reserve.  Source capacity uses the measured
    # estimator upper bound above instead of reversing the UTF-8 token floor:
    # the tokenizer can be stricter than that floor for escaped controls.
    remaining_tokens = max(1, hard_input_budget - fixed_envelope_reserve - document_limit)
    per_chunk = max(1, remaining_tokens // _SUMMARY_WORST_CASE_TOKENS_PER_SOURCE_BYTE)
    # If bounded partials cannot all fit in one merge, n chunks can require
    # 2*n-1 calls plus one repair.  Advertising only the executable chunk
    # count prevents a paid leaf graph that the configured call cap cannot
    # deterministically finish.
    call_bounded_chunks = max(1, (max_calls + 1 - repair_attempts) // 2)
    executable_chunks = min(max_chunks, call_bounded_chunks)
    return min(60_000 * executable_chunks, per_chunk * executable_chunks)


def _derive_summary_llm_config(primary: LLMConfig, config: ContextSummaryConfig) -> LLMConfig:
    # Strip every NorthCUA-only capability/budget field before NexAU builds the
    # provider payload. This is the same boundary used for main-model calls.
    derived = copy.copy(runtime_llm_config(primary))
    derived.stream = False
    derived.max_tokens = config.max_output_tokens
    derived.max_retries = 0
    derived.timeout = config.timeout_seconds
    derived.temperature = 0
    extra = dict(getattr(derived, "extra_params", {}) or {})
    primary_requested_reasoning = summary_reasoning_fingerprint_mode(primary) == "explicit_none"
    for key in (
        "include",
        "include_thoughts",
        "parallel_tool_calls",
        "prompt_cache_key",
        "reasoning",
        "reasoning_effort",
        "response_format",
        "thinking",
        "thinking_budget",
        "tool_choice",
        "tools",
    ):
        extra.pop(key, None)
    # OpenAI Responses reasoning models use provider reasoning by default when
    # the parameter is merely removed. Preserve compatibility for ordinary
    # Responses models by sending the explicit opt-out only when the primary
    # configuration already declared reasoning support.
    if str(derived.api_type or "") == "openai_responses" and primary_requested_reasoning:
        extra["reasoning"] = {"effort": "none"}
    derived.extra_params = extra
    return derived


def summary_reasoning_fingerprint_mode(primary: LLMConfig) -> str:
    """Return the non-secret effective reasoning policy used by summaries."""

    extra = dict(getattr(primary, "extra_params", {}) or {})
    declares_reasoning = "reasoning" in extra or "reasoning_effort" in extra
    if str(primary.api_type or "") == "openai_responses" and declares_reasoning:
        return "explicit_none"
    return "stripped"


def _compression_contract(
    *,
    previous_summary: ContextSummaryDocumentV1 | None,
    segments: Sequence[SummarySourceSegment],
    minimum_reduction_ratio: float,
    reserved_output_characters: int = 0,
    enforce_minimum: bool = True,
) -> SummaryCompressionContract:
    new_source_characters = sum(len(segment.text) for segment in segments)
    retained_baseline_characters = len(previous_summary.canonical_prompt_text()) if previous_summary is not None else 0
    # A durable checkpoint is a retained semantic baseline, not disposable raw
    # history. The shared strategy compresses only newly covered source.
    return _COMPRESSION_STRATEGY.resolve(
        retained_baseline_characters=retained_baseline_characters,
        new_source_characters=new_source_characters,
        minimum_reduction_ratio=minimum_reduction_ratio,
        reserved_output_characters=reserved_output_characters,
        enforce_minimum=enforce_minimum,
    )


def _summary_payload(
    *,
    previous_summary: ContextSummaryDocumentV1 | None,
    segments: Sequence[SummarySourceSegment],
    contract: SummaryCompressionContract,
) -> JsonObject:
    return {
        "schema_version": 1,
        "previous_summary": (previous_summary.canonical_payload() if previous_summary is not None else None),
        "source_segments": cast(JsonArray, [segment.model_dump(mode="json") for segment in segments]),
        # Dynamic constraints deliberately live in the user payload so the
        # stable system prefix remains cache-friendly across checkpoint epochs.
        "compression_contract": cast(JsonObject, contract.model_dump(mode="json")),
    }


def _validate_compression(
    document: ContextSummaryDocumentV1,
    contract: SummaryCompressionContract,
    *,
    final_document: bool = False,
) -> None:
    _COMPRESSION_STRATEGY.validate(
        document,
        contract,
        final_document=final_document,
    )


def _trace_message(message: Message) -> JsonObject:
    return {
        "role": message.role.value,
        "content": cast(JsonArray, [block.model_dump(mode="json", exclude_none=True) for block in message.content]),
    }


def _merge_usage(target: dict[str, int], incoming: dict[str, int]) -> None:
    for key, value in incoming.items():
        if isinstance(value, int):
            target[key] = target.get(key, 0) + max(0, value)
