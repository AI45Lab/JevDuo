"""Artifact storage helpers.

The package keeps overlay/store exports lazy so lightweight runtimes can import
``northcua.runtime.artifacts`` without immediately pulling in agent-infra
dependencies they do not use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .screenshot_overlay import (
        ActionScreenshotOverlayProcessor,
        BaseScreenshotOverlayProcessor,
        ScreenshotOverlayArtifact,
        ScreenshotOverlayKind,
        ScreenshotOverlayRequest,
        ScreenshotOverlayService,
        SetOfMarkScreenshotOverlayProcessor,
        persist_data_url_image,
    )
    from .store import NorthCUAArtifactStore

__all__ = [
    "ActionScreenshotOverlayProcessor",
    "BaseScreenshotOverlayProcessor",
    "NorthCUAArtifactStore",
    "ScreenshotOverlayArtifact",
    "ScreenshotOverlayKind",
    "ScreenshotOverlayRequest",
    "ScreenshotOverlayService",
    "SetOfMarkScreenshotOverlayProcessor",
    "persist_data_url_image",
]


def __getattr__(name: str) -> object:
    """Lazily expose artifact helpers with heavier runtime dependencies."""

    if name == "NorthCUAArtifactStore":
        from .store import NorthCUAArtifactStore

        return NorthCUAArtifactStore
    if name in {
        "ActionScreenshotOverlayProcessor",
        "BaseScreenshotOverlayProcessor",
        "ScreenshotOverlayArtifact",
        "ScreenshotOverlayKind",
        "ScreenshotOverlayRequest",
        "ScreenshotOverlayService",
        "SetOfMarkScreenshotOverlayProcessor",
        "persist_data_url_image",
    }:
        from . import screenshot_overlay as _overlay

        return getattr(_overlay, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
