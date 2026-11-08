"""Desktop-aligned Web prompt visual-window planning."""

from .domain import (
    WebPromptImageRole,
    WebPromptVisualHistoryTurn,
)
from .message_projector import (
    WebPromptVisualHistoryFactory,
    WebPromptVisualMessageProjector,
    WebPromptVisualWindowResult,
)

__all__ = [
    "WebPromptImageRole",
    "WebPromptVisualHistoryFactory",
    "WebPromptVisualHistoryTurn",
    "WebPromptVisualMessageProjector",
    "WebPromptVisualWindowResult",
]
