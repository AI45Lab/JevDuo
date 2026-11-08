"""Cross-platform accessibility capture support for NorthCUA.

This package keeps its heavier artifact-persistence helpers behind a lazy
attribute export so lightweight consumers, including the standalone device
worker distribution, can import capture models/collectors without pulling in
desktop artifact-store dependencies at module import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from northcua.core.accessibility import (
    AccessibilityBounds,
    AccessibilityCapabilities,
    AccessibilityCaptureRequest,
    AccessibilityCaptureStats,
    AccessibilityDiffSummary,
    AccessibilityNode,
    AccessibilitySnapshot,
    AccessibilityStepRecord,
    AccessibilitySummary,
    AccessibilityWindowRef,
)

from .collector import AccessibilityCollector
from .diff import AccessibilityDiffBuilder
from .summary import AccessibilitySummaryBuilder, calculate_capture_stats, iter_nodes

if TYPE_CHECKING:
    from .persist import AccessibilityArtifactPersister

__all__ = [
    "AccessibilityArtifactPersister",
    "AccessibilityBounds",
    "AccessibilityCapabilities",
    "AccessibilityCaptureRequest",
    "AccessibilityCaptureStats",
    "AccessibilityCollector",
    "AccessibilityDiffBuilder",
    "AccessibilityDiffSummary",
    "AccessibilityNode",
    "AccessibilitySnapshot",
    "AccessibilityStepRecord",
    "AccessibilitySummary",
    "AccessibilitySummaryBuilder",
    "AccessibilityWindowRef",
    "calculate_capture_stats",
    "iter_nodes",
]


def __getattr__(name: str) -> object:
    """Lazily expose optional helpers with heavier runtime dependencies.

    Args:
        name: Module attribute requested by the caller.

    Returns:
        Exported module attribute.

    Raises:
        AttributeError: If the name is not exported by this package.
    """

    if name == "AccessibilityArtifactPersister":
        from .persist import AccessibilityArtifactPersister

        return AccessibilityArtifactPersister
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
