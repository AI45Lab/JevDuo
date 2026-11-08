"""Typed image roles for Web prompt visual-window projection."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from northcua.agentkernel.json_types import JsonObject


class WebPromptImageRole(str, Enum):
    """Prompt-facing image roles used by Web visual-window planning."""

    CLEAN = "clean"
    ACTION_OVERLAY = "action_overlay"
    SOM = "som"
    UNKNOWN = "unknown"


class WebPromptVisualHistoryTurn(BaseModel):
    """Prompt-only visual replay turn for one completed Web agent step."""

    session_step: int = 0
    round_num: int = 1
    tool_results: list[JsonObject] = Field(default_factory=list)
