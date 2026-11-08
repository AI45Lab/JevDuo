"""Typed action records used for history overlay planning."""

from __future__ import annotations

from pydantic import BaseModel, Field

from northcua.agentkernel.json_types import JsonObject


class ActionCommandInfo(BaseModel):
    """Minimal action record needed by the overlay planner.

    Attributes:
        function: Action function name, such as ``click`` or ``scroll``.
        arguments: Raw action arguments captured for replay visualization.
    """

    function: str = ""
    arguments: JsonObject = Field(default_factory=dict)
