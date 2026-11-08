"""Web-specific final report model for AgentKit round evaluation."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from northcua.agentkit.evaluator.protocol import RoundEvaluationReport


class WebBlockerKind(str, Enum):
    """Known Web blockers the evaluator may identify from evidence."""

    NONE = "none"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA = "captcha"
    NETWORK = "network"
    PERMISSION = "permission"
    PAGE_ERROR = "page_error"
    UNSUPPORTED_BROWSER_STATE = "unsupported_browser_state"
    UNKNOWN = "unknown"


class WebRoundEvaluationReport(RoundEvaluationReport):
    """Round report extension with Web page-state verification fields."""

    final_url: str = Field(default="", description="Final verified browser URL from terminal evidence or probe.")
    final_title: str = Field(default="", description="Final verified page title from terminal evidence or probe.")
    page_state_verified: bool = Field(
        default=False,
        description="Whether URL/title/tab/page-state evidence was inspected and supports the verdict.",
    )
    dom_state_verified: bool = Field(
        default=False,
        description="Whether DOM snapshot evidence was inspected and supports the verdict.",
    )
    visual_state_verified: bool = Field(
        default=False,
        description="Whether screenshot or SoM visual evidence was inspected and supports the verdict.",
    )
    web_blocker: WebBlockerKind = Field(
        default=WebBlockerKind.NONE,
        description="Primary Web blocker when the task is not completed due to page/environment state.",
    )
    critical_web_evidence_refs: list[str] = Field(
        default_factory=list,
        description="Evidence refs that are critical to the Web-specific verdict.",
    )
