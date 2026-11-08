"""Stable digest helpers used for idempotent checkpoints."""

from __future__ import annotations

import hashlib

from northcua.agentkernel.context.domain.models import ContextCoverageEntry
from northcua.agentkernel.context.domain.runtime import ContextSegment


def coverage_digest(segments: tuple[ContextSegment[object], ...], covered_ids: frozenset[str]) -> str:
    return coverage_entries_digest(
        tuple(
            ContextCoverageEntry(segment_id=segment.segment_id, stable_digest=segment.stable_digest)
            for segment in segments
            if segment.segment_id in covered_ids
        )
    )


def extended_coverage_digest(previous_digest: str, new_segments: tuple[ContextSegment[object], ...]) -> str:
    """Extend a durable digest when resume retained only checkpoint plus tail."""

    hasher = hashlib.sha256()
    hasher.update(b"northcua-checkpoint-extension-v1\0")
    hasher.update(previous_digest.encode("ascii"))
    hasher.update(b"\n")
    for segment in new_segments:
        hasher.update(segment.segment_id.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(segment.stable_digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def coverage_entries_digest(entries: tuple[ContextCoverageEntry, ...]) -> str:
    """Hash ordered semantic identities identically for full and resumed history."""

    hasher = hashlib.sha256()
    for entry in entries:
        hasher.update(entry.segment_id.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(entry.stable_digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()
