"""Set-of-Mark selection and annotation contracts for web observations."""

from .domain import (
    WebSomCandidate,
    WebSomMark,
    WebSomOmissionReason,
    WebSomSelection,
)
from .selector import InteractionFirstSomSelector, WebSomSelectionConfig

__all__ = [
    "InteractionFirstSomSelector",
    "WebSomCandidate",
    "WebSomMark",
    "WebSomOmissionReason",
    "WebSomSelection",
    "WebSomSelectionConfig",
]
