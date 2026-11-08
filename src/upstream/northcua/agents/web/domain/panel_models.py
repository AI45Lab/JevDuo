"""Terminal/UI panel projections for web-agent steps."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from northcua.core.models import AssistantToolCallRecord

from .results import WebStepStatus, WebToolRecord


class WebStepPanelRecord(BaseModel):
    """Compact display model emitted after each web-agent step.

    Attributes:
        step: One-based step number.
        status: Step status shown in the terminal badge.
        summary: Short observation/action summary.
        url: Page URL at the time of rendering.
        tool_summary: Summary derived from the executed Web tools.
        tools: All executed web tools for this step.
        assistant_text: User-visible assistant text from the model.
        reasoning: Provider reasoning summary when available.
        assistant_tool_calls: Raw model tool-call records.
        observation_summary: Compact page title/URL summary.
        success: Whether the step completed without tool errors.
    """

    model_config = ConfigDict(extra="forbid")

    step: int
    status: WebStepStatus
    summary: str = ""
    url: str = ""
    tool_summary: str = ""
    tools: list[WebToolRecord] = Field(default_factory=list)
    assistant_text: str = ""
    reasoning: str = ""
    assistant_tool_calls: list[AssistantToolCallRecord] = Field(default_factory=list)
    observation_summary: str = ""
    success: bool = True
