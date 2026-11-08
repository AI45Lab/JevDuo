"""Shared strict base model for the v1 wire protocol."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class ProtocolModel(BaseModel):
    """Base class for wire contracts that reject unknown fields.

    Open JSON is still allowed through explicit `JsonObject` fields such as
    command parameters, metadata, and result payloads. Everywhere else, extra
    fields are treated as protocol drift and rejected at parse time.

    Attributes:
        model_config: Shared Pydantic config that forbids unknown protocol
            fields.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
