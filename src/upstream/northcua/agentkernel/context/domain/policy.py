"""Agent-owned summary policy contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextSummaryPolicy:
    """Declarative agent policy consumed by the shared summary engine."""

    profile: str
    rules: tuple[str, ...]
