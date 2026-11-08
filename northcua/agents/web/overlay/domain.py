"""Typed IR for Web Agent action overlays."""

from __future__ import annotations

from pydantic import BaseModel, Field

from northcua.agentkernel.actions import ActionCommandInfo
from northcua.core.json_types import JsonObject


class WebActionOverlayPlan(BaseModel):
    """One desktop-compatible action overlay render plan.

    The plan intentionally uses the shared ``ActionCommandInfo`` contract so Web
    can reuse the same overlay planner/renderer as Desktop without leaking Web
    action-specific details into the shared layer.
    """

    command: ActionCommandInfo
    source_image_path: str = Field(default="", description="Clean pre-action screenshot path.")
    source_observation_id: str = ""
    metadata: JsonObject = Field(default_factory=dict)
