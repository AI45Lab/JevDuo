"""Checkpoint-aware history assembly without provider-specific imports."""

from __future__ import annotations

from typing import Generic, TypeVar

from northcua.agentkernel.context.domain.models import ContextCheckpoint
from northcua.agentkernel.context.domain.runtime import ContextSegment
from northcua.agentkernel.context.ports import SummaryMessageFactory

MessageT = TypeVar("MessageT")


class CheckpointHistoryAssembler(Generic[MessageT]):
    """Replace checkpoint-covered segments at their original history boundary."""

    def __init__(self, message_factory: SummaryMessageFactory[MessageT]) -> None:
        self._message_factory = message_factory

    def assemble(
        self,
        segments: tuple[ContextSegment[MessageT], ...],
        checkpoint: ContextCheckpoint | None,
        *,
        additionally_removed: frozenset[str] = frozenset(),
    ) -> tuple[MessageT, ...]:
        covered = set(checkpoint.covered_segment_ids if checkpoint is not None else ())
        covered.update(additionally_removed)
        output: list[MessageT] = []
        checkpoint_inserted = False
        for segment in segments:
            if segment.kind.value == "checkpoint":
                if (
                    checkpoint is not None
                    and not checkpoint_inserted
                    and segment.checkpoint_id == checkpoint.checkpoint_id
                ):
                    # Incoming marker content is only a position hint. Durable
                    # repository state remains the sole summary source of truth.
                    output.append(self._message_factory.build(checkpoint.summary, checkpoint.checkpoint_id))
                    checkpoint_inserted = True
                # Stale/unverifiable checkpoint messages are never forwarded.
                continue
            if segment.segment_id in covered:
                if checkpoint is not None and not checkpoint_inserted:
                    output.append(self._message_factory.build(checkpoint.summary, checkpoint.checkpoint_id))
                    checkpoint_inserted = True
                continue
            output.extend(segment.messages)
        if checkpoint is not None and not checkpoint_inserted:
            # A resumed caller may already have dropped raw covered segments.
            # Keep the checkpoint after the immutable prefix instead of losing it.
            insert_at = 0
            for segment in segments:
                if segment.kind.value in {"system", "task", "current_observation"}:
                    insert_at += len(segment.messages)
                else:
                    break
            output.insert(insert_at, self._message_factory.build(checkpoint.summary, checkpoint.checkpoint_id))
        return tuple(output)
