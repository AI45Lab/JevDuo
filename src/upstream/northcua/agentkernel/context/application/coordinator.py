"""Shared context-management façade used by every NorthCUA agent runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import AsyncIterator, Generic, TypeVar, cast
from uuid import uuid4

from northcua.agentkernel.context.domain.enums import (
    CompactionOutcome,
    CompactionTrigger,
    ContextManagementMode,
    FailurePolicy,
    RetentionClass,
    SummaryInvocationKind,
)
from northcua.agentkernel.context.domain.models import (
    ContextBudget,
    ContextCheckpoint,
    ContextCompactionEvent,
    ContextCoverageEntry,
    ContextTokenEstimate,
    ModelInvocationTraceEnvelope,
    SummaryInvocationCallRecord,
    SummaryInvocationRequest,
    SummarySourceSegment,
    TokenUsageRecord,
)
from northcua.agentkernel.context.domain.runtime import (
    ContextPreparationRequest,
    ContextPreparationResult,
    ContextSegment,
)
from northcua.agentkernel.context.ports import (
    CheckpointRepository,
    ContextTraceSink,
    SummaryClient,
    TokenEstimator,
)
from northcua.agentkernel.context.ports.contracts import NullContextTraceSink
from northcua.agentkernel.context.strategies import (
    ContextBudgetExceededError,
    ContextStaleRequestError,
    ContextSummaryLifecycleError,
)
from northcua.agentkernel.context.strategies.digests import coverage_digest, coverage_entries_digest

from .budget import ContextBudgetPlanner
from .history import CheckpointHistoryAssembler

MessageT = TypeVar("MessageT")
ToolT = TypeVar("ToolT")
logger = logging.getLogger(__name__)


class ContextCoordinator(Generic[MessageT, ToolT]):
    """Plan, summarize, persist and assemble bounded context for one step."""

    def __init__(
        self,
        *,
        estimator: TokenEstimator[MessageT, ToolT],
        assembler: CheckpointHistoryAssembler[MessageT],
        repository: CheckpointRepository | None = None,
        summary_client: SummaryClient | None = None,
        trace_sink: ContextTraceSink | None = None,
        budget_planner: ContextBudgetPlanner | None = None,
    ) -> None:
        self._estimator = estimator
        self._assembler = assembler
        self._repository = repository
        self._summary_client = summary_client
        self._trace_sink = trace_sink or NullContextTraceSink()
        self._budget_planner = budget_planner or ContextBudgetPlanner()

    async def prepare(
        self,
        request: ContextPreparationRequest[MessageT, ToolT],
    ) -> ContextPreparationResult[MessageT]:
        """Return the effective provider messages for one logical step."""

        if request.policy.mode == ContextManagementMode.OFF:
            return ContextPreparationResult(
                messages=request.original_messages,
                outcome=CompactionOutcome.UNCHANGED,
            )

        if request.policy.mode == ContextManagementMode.SHADOW:
            try:
                budget = self._budget_planner.resolve(
                    context_limit=request.context_limit,
                    output_reserve=request.output_reserve,
                    config=request.policy.budget,
                )
                original_estimate = self._estimator.estimate(request.original_messages, request.tools)
            except Exception:
                # Shadow is observational and must never change provider behavior.
                return ContextPreparationResult(
                    messages=request.original_messages,
                    outcome=CompactionOutcome.UNCHANGED,
                )
            outcome = (
                CompactionOutcome.WOULD_COMPACT
                if original_estimate.total_tokens >= budget.soft_trigger_tokens
                else CompactionOutcome.UNCHANGED
            )
            self._record_event(
                request=request,
                trigger=CompactionTrigger.SHADOW,
                outcome=outcome,
                before_tokens=original_estimate.total_tokens,
                after_tokens=original_estimate.total_tokens,
                hard_input_budget=budget.hard_input_budget,
                status="observed",
            )
            return ContextPreparationResult(
                messages=request.original_messages,
                outcome=outcome,
                budget=budget,
                estimate_before=original_estimate,
                estimate_after=original_estimate,
            )

        budget = self._budget_planner.resolve(
            context_limit=request.context_limit,
            output_reserve=request.output_reserve,
            config=request.policy.budget,
        )
        original_estimate = self._estimator.estimate(request.original_messages, request.tools)

        if self._repository is None or self._summary_client is None:
            raise RuntimeError("enforce context management requires repository and summary_client")

        checkpoint = await self._load_valid_checkpoint(request)
        effective_messages = self._assembler.assemble(request.segments, checkpoint)
        effective_estimate = self._estimator.estimate(effective_messages, request.tools)
        if effective_estimate.total_tokens < budget.soft_trigger_tokens:
            outcome = CompactionOutcome.CHECKPOINT_REUSED if checkpoint is not None else CompactionOutcome.UNCHANGED
            if checkpoint is not None:
                self._record_event(
                    request=request,
                    trigger=CompactionTrigger.CHECKPOINT_REUSE,
                    outcome=outcome,
                    checkpoint=checkpoint,
                    before_tokens=original_estimate.total_tokens,
                    after_tokens=effective_estimate.total_tokens,
                    hard_input_budget=budget.hard_input_budget,
                    status="reused",
                )
            return ContextPreparationResult(
                messages=effective_messages,
                outcome=outcome,
                budget=budget,
                estimate_before=original_estimate,
                estimate_after=effective_estimate,
                checkpoint=checkpoint,
            )

        eligible = self._bounded_summary_prefix(self._eligible_segments(request.segments, checkpoint))
        if eligible:
            try:
                async with asyncio.timeout(request.policy.summary.total_deadline_seconds):
                    async with self._repository.lease(request.owner, "context-summary-progressive"):
                        remaining_calls = request.policy.summary.max_calls
                        while eligible and remaining_calls > 0:
                            checkpoint, physical_calls = await self._summarize_and_commit(
                                request=request,
                                checkpoint=checkpoint,
                                eligible=eligible,
                                before_tokens=effective_estimate.total_tokens,
                                hard_input_budget=budget.hard_input_budget,
                                max_calls=remaining_calls,
                                lease_held=True,
                            )
                            remaining_calls -= max(1, physical_calls)
                            effective_messages = self._assembler.assemble(request.segments, checkpoint)
                            effective_estimate = self._estimator.estimate(effective_messages, request.tools)
                            if effective_estimate.total_tokens <= budget.target_tokens:
                                return ContextPreparationResult(
                                    messages=effective_messages,
                                    outcome=CompactionOutcome.CHECKPOINT_COMMITTED,
                                    budget=budget,
                                    estimate_before=original_estimate,
                                    estimate_after=effective_estimate,
                                    checkpoint=checkpoint,
                                )
                            eligible = self._bounded_summary_prefix(
                                self._eligible_segments(request.segments, checkpoint)
                            )
            except Exception as exc:
                lifecycle_error = exc if isinstance(exc, ContextSummaryLifecycleError) else None
                self._record_event(
                    request=request,
                    event_id=lifecycle_error.event_id if lifecycle_error else None,
                    trigger=CompactionTrigger.SOFT_BUDGET,
                    outcome=CompactionOutcome.SUMMARY_FAILED,
                    checkpoint=checkpoint,
                    source_digest=lifecycle_error.source_digest if lifecycle_error else "",
                    checkpoint_id=lifecycle_error.checkpoint_id if lifecycle_error else "",
                    before_tokens=effective_estimate.total_tokens,
                    after_tokens=effective_estimate.total_tokens,
                    hard_input_budget=budget.hard_input_budget,
                    status="failed",
                    error=exc,
                )
                if request.policy.failure.soft == FailurePolicy.ERROR:
                    raise
                if effective_estimate.total_tokens <= budget.hard_input_budget:
                    return ContextPreparationResult(
                        messages=effective_messages,
                        outcome=CompactionOutcome.SUMMARY_FAILED,
                        budget=budget,
                        estimate_before=original_estimate,
                        estimate_after=effective_estimate,
                        checkpoint=checkpoint,
                    )

        if effective_estimate.total_tokens <= budget.hard_input_budget:
            return ContextPreparationResult(
                messages=effective_messages,
                outcome=CompactionOutcome.CHECKPOINT_REUSED if checkpoint else CompactionOutcome.UNCHANGED,
                budget=budget,
                estimate_before=original_estimate,
                estimate_after=effective_estimate,
                checkpoint=checkpoint,
            )

        return self._apply_hard_guard(
            request=request,
            checkpoint=checkpoint,
            budget=budget,
            original_estimate=original_estimate,
        )

    async def recover_overflow(
        self,
        request: ContextPreparationRequest[MessageT, ToolT],
    ) -> ContextPreparationResult[MessageT]:
        """Run a single deterministic hard-guard pass after provider overflow."""

        if request.policy.mode != ContextManagementMode.ENFORCE:
            return await self.prepare(request)
        budget = self._budget_planner.resolve(
            context_limit=request.context_limit,
            output_reserve=request.output_reserve,
            config=request.policy.budget,
        )
        checkpoint = await self._load_valid_checkpoint(request)
        return self._apply_hard_guard(
            request=request,
            checkpoint=checkpoint,
            budget=budget,
            original_estimate=self._estimator.estimate(request.original_messages, request.tools),
            trigger=CompactionTrigger.PROVIDER_OVERFLOW,
            trim_input_budget=budget.target_tokens,
        )

    async def _load_valid_checkpoint(
        self,
        request: ContextPreparationRequest[MessageT, ToolT],
    ) -> ContextCheckpoint | None:
        if self._repository is None:
            return None
        checkpoint = await self._repository.load(request.owner)
        return checkpoint if self._is_valid_checkpoint(request, checkpoint) else None

    def _is_valid_checkpoint(
        self,
        request: ContextPreparationRequest[MessageT, ToolT],
        checkpoint: ContextCheckpoint | None,
    ) -> bool:
        if checkpoint is None:
            return False
        if checkpoint.owner != request.owner:
            return False
        if checkpoint.schema_version != request.policy.checkpoint.schema_version:
            return False
        if checkpoint.policy_version != request.policy.policy_version:
            return False
        if checkpoint.policy_fingerprint != request.policy_fingerprint:
            return False
        if checkpoint.prompt_fingerprint != request.prompt_fingerprint:
            return False
        if checkpoint.model_fingerprint != request.model_fingerprint:
            return False
        resumed_checkpoint = next(
            (
                segment
                for segment in request.segments
                if segment.kind.value == "checkpoint" and segment.checkpoint_id == checkpoint.checkpoint_id
            ),
            None,
        )
        if resumed_checkpoint is not None:
            return True
        available = {segment.segment_id for segment in request.segments}
        covered = frozenset(checkpoint.covered_segment_ids)
        if not covered or not covered.issubset(available):
            return False
        if checkpoint.covered_sources:
            available_by_id = {segment.segment_id: segment for segment in request.segments}
            if any(
                entry.segment_id not in available_by_id
                or available_by_id[entry.segment_id].stable_digest != entry.stable_digest
                for entry in checkpoint.covered_sources
            ):
                return False
            return coverage_entries_digest(checkpoint.covered_sources) == checkpoint.source_digest
        return self._coverage_digest(request.segments, covered) == checkpoint.source_digest

    @staticmethod
    def _eligible_segments(
        segments: tuple[ContextSegment[MessageT], ...],
        checkpoint: ContextCheckpoint | None,
    ) -> tuple[ContextSegment[MessageT], ...]:
        covered = set(checkpoint.covered_segment_ids if checkpoint else ())
        return tuple(
            segment
            for segment in segments
            if segment.retention == RetentionClass.COMPACTABLE and segment.segment_id not in covered
        )

    def _bounded_summary_prefix(
        self,
        segments: tuple[ContextSegment[MessageT], ...],
    ) -> tuple[ContextSegment[MessageT], ...]:
        """Select the oldest complete prefix one summary invocation can hold."""

        if self._summary_client is None:
            return segments
        capacity = getattr(self._summary_client, "source_capacity_bytes", None)
        use_bytes = capacity is not None
        if capacity is None:
            capacity = getattr(self._summary_client, "source_capacity_chars", None)
        if capacity is None or capacity <= 0:
            return segments
        selected: list[ContextSegment[MessageT]] = []
        used = 0
        for segment in segments:
            size = len(segment.summary_text.encode("utf-8")) if use_bytes else len(segment.summary_text)
            if selected and used + size > capacity:
                break
            if size > capacity:
                return ()
            selected.append(segment)
            used += size
        return tuple(selected)

    async def _summarize_and_commit(
        self,
        *,
        request: ContextPreparationRequest[MessageT, ToolT],
        checkpoint: ContextCheckpoint | None,
        eligible: tuple[ContextSegment[MessageT], ...],
        before_tokens: int,
        hard_input_budget: int,
        max_calls: int,
        lease_held: bool = False,
    ) -> tuple[ContextCheckpoint, int]:
        assert self._repository is not None
        assert self._summary_client is not None
        requested_coverage = {
            *(checkpoint.covered_segment_ids if checkpoint else ()),
            *(segment.segment_id for segment in eligible),
        }
        lease = (
            _noop_summary_lease()
            if lease_held
            else self._repository.lease(request.owner, "context-summary-transaction")
        )
        async with lease:
            stored = await self._repository.load(request.owner)
            # A stale request can legitimately contain only an older prefix, so
            # full checkpoint validation (which requires covered raw segments)
            # would reject the newer durable winner. Identity + monotonic
            # coverage are sufficient to prevent an A+B checkpoint regressing
            # to A when requests complete out of order.
            if (
                stored is not None
                and self._has_matching_checkpoint_identity(request, stored)
                and requested_coverage.issubset(set(stored.covered_segment_ids))
            ):
                if self._is_valid_checkpoint(request, stored):
                    return stored, 0
                raise ContextStaleRequestError(
                    "request predates a newer durable checkpoint; preserving repository without injecting future context"
                )
            winner = stored if self._is_valid_checkpoint(request, stored) else None
            if winner is not None and requested_coverage.issubset(set(winner.covered_segment_ids)):
                return winner, 0
            checkpoint = winner
            eligible = self._bounded_summary_prefix(self._eligible_segments(request.segments, checkpoint))
            if not eligible:
                if checkpoint is not None:
                    return checkpoint, 0
                raise ContextSummaryLifecycleError(
                    "no eligible context segment fits the configured summary capacity",
                    event_id=str(uuid4()),
                    checkpoint_id="",
                    source_digest="",
                )
            previous_entries = list(checkpoint.covered_sources if checkpoint else ())
            if checkpoint is not None and not previous_entries:
                covered_set = set(checkpoint.covered_segment_ids)
                previous_entries.extend(
                    ContextCoverageEntry(segment_id=segment.segment_id, stable_digest=segment.stable_digest)
                    for segment in request.segments
                    if segment.segment_id in covered_set
                )
            known_ids = {entry.segment_id for entry in previous_entries}
            covered_entries = tuple(
                [
                    *previous_entries,
                    *(
                        ContextCoverageEntry(segment_id=segment.segment_id, stable_digest=segment.stable_digest)
                        for segment in eligible
                        if segment.segment_id not in known_ids
                    ),
                ]
            )
            covered_ids = tuple(entry.segment_id for entry in covered_entries)
            source_digest = coverage_entries_digest(covered_entries)
            expected_revision = stored.revision if stored is not None else 0
            next_epoch = stored.epoch + 1 if stored is not None else 1

            event_id = str(uuid4())
            checkpoint_id = str(uuid4())
            invocation_id = str(uuid4())
            invocation = SummaryInvocationRequest(
                invocation_id=invocation_id,
                compaction_event_id=event_id,
                checkpoint_id=checkpoint_id,
                owner=request.owner,
                invocation_kind=SummaryInvocationKind.CONTEXT_SUMMARY,
                policy_profile=request.policy.policy_profile,
                prompt_fingerprint=request.prompt_fingerprint,
                source_digest=source_digest,
                previous_summary=checkpoint.summary if checkpoint else None,
                segments=tuple(
                    SummarySourceSegment(
                        segment_id=segment.segment_id,
                        kind=segment.kind,
                        text=segment.summary_text,
                        source_refs=segment.source_refs,
                    )
                    for segment in eligible
                ),
                max_output_tokens=request.policy.summary.max_output_tokens,
                max_calls=max_calls,
            )
            self._record_event(
                request=request,
                event_id=event_id,
                trigger=CompactionTrigger.SOFT_BUDGET,
                outcome=CompactionOutcome.WOULD_COMPACT,
                checkpoint=checkpoint,
                source_digest=source_digest,
                checkpoint_id=checkpoint_id,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                hard_input_budget=hard_input_budget,
                covered_segment_count=len(covered_ids),
                status="summary_started",
            )
            try:
                async with asyncio.timeout(request.policy.summary.total_deadline_seconds):
                    result = await self._summary_client.summarize(invocation)
            except asyncio.CancelledError as exc:
                self._record_failed_summary_trace(request, invocation, exc)
                self._record_event(
                    request=request,
                    event_id=event_id,
                    trigger=CompactionTrigger.SOFT_BUDGET,
                    outcome=CompactionOutcome.SUMMARY_FAILED,
                    checkpoint=checkpoint,
                    source_digest=source_digest,
                    checkpoint_id=checkpoint_id,
                    before_tokens=before_tokens,
                    after_tokens=before_tokens,
                    hard_input_budget=hard_input_budget,
                    covered_segment_count=len(covered_ids),
                    status="summary_cancelled",
                    error=exc,
                )
                raise
            except Exception as exc:
                self._record_failed_summary_trace(request, invocation, exc)
                raise ContextSummaryLifecycleError(
                    str(exc),
                    event_id=event_id,
                    checkpoint_id=checkpoint_id,
                    source_digest=source_digest,
                ) from exc

            try:
                # A durable checkpoint must never point at a trace that failed to persist.
                self._record_summary_trace(request, invocation, result)
            except Exception as exc:
                raise ContextSummaryLifecycleError(
                    f"summary trace persistence failed: {exc}",
                    event_id=event_id,
                    checkpoint_id=checkpoint_id,
                    source_digest=source_digest,
                ) from exc
            next_checkpoint = ContextCheckpoint(
                checkpoint_id=checkpoint_id,
                owner=request.owner,
                schema_version=request.policy.checkpoint.schema_version,
                policy_version=request.policy.policy_version,
                policy_fingerprint=request.policy_fingerprint,
                prompt_fingerprint=request.prompt_fingerprint,
                model_fingerprint=request.model_fingerprint,
                revision=expected_revision + 1,
                epoch=next_epoch,
                covered_segment_ids=covered_ids,
                covered_sources=covered_entries,
                source_digest=source_digest,
                summary=result.document,
                rendered_token_count=max(1, len(result.document.canonical_prompt_text().encode("utf-8")) // 4),
                summary_trace_id=result.invocation_id,
            )
            assembled = self._assembler.assemble(request.segments, next_checkpoint)
            after_tokens = self._estimator.estimate(assembled, request.tools).total_tokens
            if after_tokens >= before_tokens:
                raise ContextSummaryLifecycleError(
                    f"summary did not reduce context tokens ({after_tokens}>={before_tokens})",
                    event_id=event_id,
                    checkpoint_id=checkpoint_id,
                    source_digest=source_digest,
                )
            committed = await self._repository.compare_and_swap(
                request.owner,
                expected_revision=expected_revision,
                checkpoint=next_checkpoint,
            )
            if not committed:
                concurrent = await self._load_valid_checkpoint(request)
                if concurrent is not None and concurrent.source_digest == source_digest:
                    return concurrent, 0
                raise ContextSummaryLifecycleError(
                    "checkpoint compare-and-swap conflict",
                    event_id=event_id,
                    checkpoint_id=checkpoint_id,
                    source_digest=source_digest,
                )
            self._record_event(
                request=request,
                event_id=event_id,
                trigger=CompactionTrigger.SOFT_BUDGET,
                outcome=CompactionOutcome.CHECKPOINT_COMMITTED,
                checkpoint=next_checkpoint,
                source_digest=source_digest,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                hard_input_budget=hard_input_budget,
                covered_segment_count=len(covered_ids),
                status="checkpoint_committed",
            )
            return next_checkpoint, max(1, len(result.call_records))

    @staticmethod
    def _has_matching_checkpoint_identity(
        request: ContextPreparationRequest[MessageT, ToolT],
        checkpoint: ContextCheckpoint,
    ) -> bool:
        return (
            checkpoint.owner == request.owner
            and checkpoint.schema_version == request.policy.checkpoint.schema_version
            and checkpoint.policy_version == request.policy.policy_version
            and checkpoint.policy_fingerprint == request.policy_fingerprint
            and checkpoint.prompt_fingerprint == request.prompt_fingerprint
            and checkpoint.model_fingerprint == request.model_fingerprint
        )

    def _apply_hard_guard(
        self,
        *,
        request: ContextPreparationRequest[MessageT, ToolT],
        checkpoint: ContextCheckpoint | None,
        budget: ContextBudget,
        original_estimate: ContextTokenEstimate,
        trigger: CompactionTrigger = CompactionTrigger.HARD_BUDGET,
        trim_input_budget: int | None = None,
    ) -> ContextPreparationResult[MessageT]:
        already_covered = set(checkpoint.covered_segment_ids if checkpoint else ())
        candidates = (
            [
                segment
                for segment in request.segments
                if segment.segment_id not in already_covered
                and segment.retention in {RetentionClass.COMPACTABLE, RetentionClass.RECENT}
            ]
            if request.policy.failure.hard == FailurePolicy.ATOMIC_TRIM_THEN_ERROR
            else []
        )
        removed: set[str] = set()
        messages = self._assembler.assemble(request.segments, checkpoint)
        estimate = self._estimator.estimate(messages, request.tools)
        trim_budget = trim_input_budget if trim_input_budget is not None else budget.hard_input_budget
        for segment in candidates:
            if estimate.total_tokens <= trim_budget:
                break
            removed.add(segment.segment_id)
            messages = self._assembler.assemble(
                request.segments,
                checkpoint,
                additionally_removed=frozenset(removed),
            )
            estimate = self._estimator.estimate(messages, request.tools)

        if estimate.total_tokens > budget.hard_input_budget:
            error = ContextBudgetExceededError(
                f"pinned/current context exceeds safe input budget ({estimate.total_tokens}>{budget.hard_input_budget})"
            )
            self._record_event(
                request=request,
                trigger=trigger,
                outcome=CompactionOutcome.HARD_BUDGET_REJECTED,
                checkpoint=checkpoint,
                before_tokens=original_estimate.total_tokens,
                after_tokens=estimate.total_tokens,
                hard_input_budget=budget.hard_input_budget,
                status="rejected",
                error=error,
            )
            raise error

        self._record_event(
            request=request,
            trigger=trigger,
            outcome=CompactionOutcome.DETERMINISTIC_TRIM,
            checkpoint=checkpoint,
            before_tokens=original_estimate.total_tokens,
            after_tokens=estimate.total_tokens,
            hard_input_budget=budget.hard_input_budget,
            covered_segment_count=len(removed),
            status="trimmed",
        )
        return ContextPreparationResult(
            messages=messages,
            outcome=CompactionOutcome.DETERMINISTIC_TRIM,
            budget=budget,
            estimate_before=original_estimate,
            estimate_after=estimate,
            checkpoint=checkpoint,
            removed_segment_ids=tuple(segment.segment_id for segment in candidates if segment.segment_id in removed),
        )

    @staticmethod
    def _coverage_digest(
        segments: tuple[ContextSegment[MessageT], ...],
        covered_ids: frozenset[str],
    ) -> str:
        return coverage_digest(cast(tuple[ContextSegment[object], ...], segments), covered_ids)

    def _record_event(
        self,
        *,
        request: ContextPreparationRequest[MessageT, ToolT],
        trigger: CompactionTrigger,
        outcome: CompactionOutcome,
        before_tokens: int,
        after_tokens: int,
        hard_input_budget: int,
        status: str,
        event_id: str | None = None,
        checkpoint: ContextCheckpoint | None = None,
        source_digest: str = "",
        checkpoint_id: str = "",
        covered_segment_count: int = 0,
        error: BaseException | None = None,
    ) -> None:
        event = ContextCompactionEvent(
            event_id=event_id or str(uuid4()),
            logical_step_id=request.logical_step_id,
            owner=request.owner,
            trigger=trigger,
            outcome=outcome,
            policy_fingerprint=request.policy_fingerprint,
            source_digest=source_digest or (checkpoint.source_digest if checkpoint else ""),
            checkpoint_id=checkpoint_id or (checkpoint.checkpoint_id if checkpoint else ""),
            checkpoint_epoch=checkpoint.epoch if checkpoint else 0,
            summary_trace_id=checkpoint.summary_trace_id if checkpoint else "",
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            hard_input_budget=hard_input_budget,
            covered_segment_count=covered_segment_count,
            status=status,
            error_type=type(error).__name__ if error else "",
            error_message=str(error) if error else "",
            metadata=request.metadata or {},
        )
        try:
            self._trace_sink.record_event(event)
        except Exception:
            # Observability must never prevent a safe provider request.
            logger.warning("Unable to persist context compaction event", exc_info=True)

    def _record_summary_trace(
        self,
        request: ContextPreparationRequest[MessageT, ToolT],
        invocation: SummaryInvocationRequest,
        result: object,
    ) -> None:
        from northcua.agentkernel.context.domain.models import SummaryInvocationResult

        summary_result = cast(SummaryInvocationResult, result)
        self._record_physical_call_traces(
            request=request,
            invocation=invocation,
            call_records=summary_result.call_records,
            model=summary_result.model,
            provider=summary_result.provider,
        )
        usage = summary_result.usage
        trace = ModelInvocationTraceEnvelope(
            trace_id=summary_result.invocation_id,
            invocation_kind=invocation.invocation_kind,
            compaction_event_id=invocation.compaction_event_id,
            checkpoint_id=invocation.checkpoint_id,
            parent_main_invocation_id=request.parent_main_invocation_id,
            owner=request.owner,
            source_digest=invocation.source_digest,
            model=summary_result.model,
            provider=summary_result.provider,
            prompt_fingerprint=request.prompt_fingerprint,
            request=summary_result.request_payload,
            response=summary_result.response_payload,
            usage=TokenUsageRecord(
                input_tokens=_usage_int(usage, "input_tokens"),
                output_tokens=_usage_int(usage, "completion_tokens", "output_tokens"),
                reasoning_tokens=_usage_int(usage, "reasoning_tokens"),
                cache_read_tokens=_usage_int(usage, "cache_read_tokens", "cached_tokens"),
                cache_write_tokens=_usage_int(usage, "cache_creation_tokens", "cache_write_tokens"),
                total_tokens=_usage_int(usage, "total_tokens"),
            ),
            latency_ms=summary_result.latency_ms,
            status="succeeded",
        )
        self._trace_sink.record_summary(trace)

    def _record_failed_summary_trace(
        self,
        request: ContextPreparationRequest[MessageT, ToolT],
        invocation: SummaryInvocationRequest,
        error: BaseException,
    ) -> None:
        raw_call_records = _exception_attr(error, "northcua_summary_call_records", ())
        call_records = (
            tuple(record for record in raw_call_records if isinstance(record, SummaryInvocationCallRecord))
            if isinstance(raw_call_records, (list, tuple))
            else ()
        )
        model = str(_exception_attr(error, "northcua_summary_model", ""))
        provider = str(_exception_attr(error, "northcua_summary_provider", ""))
        trace = ModelInvocationTraceEnvelope(
            trace_id=invocation.invocation_id,
            invocation_kind=invocation.invocation_kind,
            compaction_event_id=invocation.compaction_event_id,
            checkpoint_id=invocation.checkpoint_id,
            parent_main_invocation_id=request.parent_main_invocation_id,
            owner=request.owner,
            source_digest=invocation.source_digest,
            model=model,
            provider=provider,
            prompt_fingerprint=request.prompt_fingerprint,
            status="failed",
            error_type=type(error).__name__,
            error_message=str(error),
        )
        try:
            self._record_physical_call_traces(
                request=request,
                invocation=invocation,
                call_records=call_records,
                model=model,
                provider=provider,
            )
            self._trace_sink.record_summary(trace)
        except Exception:
            logger.warning("Unable to persist failed context summary trace", exc_info=True)

    def _record_physical_call_traces(
        self,
        *,
        request: ContextPreparationRequest[MessageT, ToolT],
        invocation: SummaryInvocationRequest,
        call_records: tuple[SummaryInvocationCallRecord, ...],
        model: str,
        provider: str,
    ) -> None:
        for call in call_records:
            kind = _summary_call_kind(call.purpose)
            usage = call.usage
            self._trace_sink.record_summary(
                ModelInvocationTraceEnvelope(
                    trace_id=f"{invocation.invocation_id}:call-{call.call_index}",
                    invocation_kind=kind,
                    compaction_event_id=invocation.compaction_event_id,
                    checkpoint_id=invocation.checkpoint_id,
                    parent_main_invocation_id=request.parent_main_invocation_id,
                    owner=request.owner,
                    source_digest=invocation.source_digest,
                    model=model,
                    provider=provider,
                    prompt_fingerprint=request.prompt_fingerprint,
                    request=call.request,
                    response=call.response,
                    usage=TokenUsageRecord(
                        input_tokens=_usage_int(usage, "input_tokens"),
                        output_tokens=_usage_int(usage, "completion_tokens", "output_tokens"),
                        reasoning_tokens=_usage_int(usage, "reasoning_tokens"),
                        cache_read_tokens=_usage_int(usage, "cache_read_tokens", "cached_tokens"),
                        cache_write_tokens=_usage_int(usage, "cache_creation_tokens", "cache_write_tokens"),
                        total_tokens=_usage_int(usage, "total_tokens"),
                    ),
                    latency_ms=call.latency_ms,
                    status=call.status,
                    error_type=call.error_type,
                    error_message=call.error_message,
                )
            )


def _usage_int(usage: object, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _summary_call_kind(purpose: str) -> SummaryInvocationKind:
    if purpose.endswith("_repair"):
        return SummaryInvocationKind.CONTEXT_SUMMARY_REPAIR
    if purpose == "context_summary_merge":
        return SummaryInvocationKind.CONTEXT_SUMMARY_MERGE
    return SummaryInvocationKind.CONTEXT_SUMMARY


def _exception_attr(error: BaseException, name: str, default: object) -> object:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, name):
            return getattr(current, name)
        current = current.__cause__ or current.__context__
    return default


@asynccontextmanager
async def _noop_summary_lease() -> AsyncIterator[None]:
    yield
