"""Composite drawing helpers for history overlay rendering."""

from __future__ import annotations

from .clicks import HistoryActionOverlayClickDrawingHelper
from .scroll import HistoryActionOverlayScrollDrawingHelper


class HistoryActionOverlayDrawingHelper(
    HistoryActionOverlayClickDrawingHelper,
    HistoryActionOverlayScrollDrawingHelper,
):
    """Composite helper that groups all overlay drawing primitives."""
