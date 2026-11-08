"""Abstract boundaries implemented by runtime/provider infrastructure."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, TypeVar

from northcua.agentkernel.context.domain.models import (
    ContextCheckpoint,
    ContextCompactionEvent,
    ContextOwnerKey,
    ContextSummaryDocumentV1,
    ContextTokenEstimate,
    ModelInvocationTraceEnvelope,
    SummaryInvocationRequest,
    SummaryInvocationResult,
)

EstimatedMessageT = TypeVar("EstimatedMessageT", contravariant=True)
EstimatedToolT = TypeVar("EstimatedToolT", contravariant=True)
BuiltMessageT = TypeVar("BuiltMessageT", covariant=True)


class TokenEstimator(Protocol[EstimatedMessageT, EstimatedToolT]):
    """Estimate provider input size including tools and non-text blocks."""

    def estimate(
        self,
        messages: tuple[EstimatedMessageT, ...],
        tools: tuple[EstimatedToolT, ...],
    ) -> ContextTokenEstimate: ...


class SummaryClient(Protocol):
    """Generate one validated typed context summary."""

    async def summarize(self, request: SummaryInvocationRequest) -> SummaryInvocationResult: ...

    async def close(self) -> None: ...

    @property
    def source_capacity_bytes(self) -> int | None: ...


class CheckpointRepository(Protocol):
    """Durable compare-and-swap repository with a summary singleflight lease."""

    async def load(self, owner: ContextOwnerKey) -> ContextCheckpoint | None: ...

    async def compare_and_swap(
        self,
        owner: ContextOwnerKey,
        *,
        expected_revision: int,
        checkpoint: ContextCheckpoint,
    ) -> bool: ...

    def lease(self, owner: ContextOwnerKey, source_digest: str) -> AbstractAsyncContextManager[None]: ...


class SummaryMessageFactory(Protocol[BuiltMessageT]):
    """Render a provider-neutral checkpoint as one runtime message."""

    def build(self, summary: ContextSummaryDocumentV1, checkpoint_id: str) -> BuiltMessageT: ...


class ContextTraceSink(Protocol):
    """Persist context lifecycle and internal model invocations."""

    def record_event(self, event: ContextCompactionEvent) -> None: ...

    def record_summary(self, trace: ModelInvocationTraceEnvelope) -> None: ...


class NullContextTraceSink:
    """No-op sink used by OFF mode and isolated unit tests."""

    def record_event(self, event: ContextCompactionEvent) -> None:
        del event

    def record_summary(self, trace: ModelInvocationTraceEnvelope) -> None:
        del trace
