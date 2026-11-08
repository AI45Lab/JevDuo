"""Typed submitter specs for remote AgentKit tasks."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field

from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.capabilities import Capability, JsonObject

_DESKTOP_REMOTE_CAPABILITIES = {
    Capability.DESKTOP_INPUT,
    Capability.DESKTOP_SCREENSHOT,
    Capability.DESKTOP_WINDOW,
}

ToolChoicePolicy = Literal["auto", "required"]


class SubmitTaskPayload(TypedDict):
    """Keyword payload accepted by ``ControllerSubmitterClient.submit_task``."""

    agent_kind: str
    request: str
    target_device_id: str
    required_capabilities: set[Capability]
    input_artifacts: NotRequired[list[ArtifactRef]]
    metadata: dict[str, object]


class DesktopSubmitSpec(BaseModel):
    """Typed spec for one remote desktop task submission.

    Attributes:
        query: User task/query text.
        session_id: Optional session id forwarded to the desktop runner.
        max_steps: Maximum desktop round/query steps before terminal stop.
        max_session_steps: Optional session-wide step cap.
        tool_choice_policy: Per-task model tool-selection policy.
        target_device_id: Optional strict controller device target.
        required_capabilities: Device capabilities required for scheduling.
    """

    query: str
    session_id: str = ""
    max_steps: int = 100
    max_session_steps: int = 0
    tool_choice_policy: ToolChoicePolicy = "auto"
    allowed_tool_names: tuple[str, ...] = ()
    target_device_id: str = ""
    required_capabilities: set[Capability] = Field(default_factory=lambda: set(_DESKTOP_REMOTE_CAPABILITIES))

    def submit_payload(self) -> SubmitTaskPayload:
        """Return controller submit kwargs for the desktop task.

        Returns:
            Keyword payload accepted by `ControllerSubmitterClient.submit_task`.
        """

        return {
            "agent_kind": "desktop",
            "request": self.query,
            "target_device_id": self.target_device_id,
            "required_capabilities": set(self.required_capabilities),
            "metadata": {
                "session_id": self.session_id,
                "max_steps": self.max_steps,
                "max_session_steps": self.max_session_steps,
                "tool_choice_policy": self.tool_choice_policy,
                "allowed_tool_names": list(self.allowed_tool_names),
            },
        }


class WebSubmitSpec(BaseModel):
    """Typed spec for one remote generic web-agent task submission.

    Attributes:
        query: User web task/query text.
        url: Optional initial URL opened by the remote browser runner.
        headed: Optional per-task override for showing the browser window.
        session_id: Optional controller/web-agent session id.
        max_steps: Optional web-agent round/query step cap.
        max_session_steps: Optional session-wide step cap.
        tool_choice_policy: Per-task model tool-selection policy.
        target_device_id: Optional strict controller device target.
        required_capabilities: Browser/device capabilities required for
            scheduling.
    """

    query: str
    url: str = ""
    headed: bool | None = None
    session_id: str = ""
    max_steps: int = 0
    max_session_steps: int = 0
    tool_choice_policy: ToolChoicePolicy = "auto"
    allowed_tool_names: tuple[str, ...] = ()
    target_device_id: str = ""
    required_capabilities: set[Capability] = Field(default_factory=lambda: {Capability.BROWSER_PAGE})

    def submit_payload(self) -> SubmitTaskPayload:
        """Return controller submit kwargs for the web-agent task.

        Returns:
            Keyword payload accepted by ``ControllerSubmitterClient``.
        """

        metadata: dict[str, object] = {
            "session_id": self.session_id,
            "url": self.url,
            "max_steps": self.max_steps,
            "max_session_steps": self.max_session_steps,
            "tool_choice_policy": self.tool_choice_policy,
            "allowed_tool_names": list(self.allowed_tool_names),
        }
        if self.headed is not None:
            metadata["headed"] = self.headed
        return {
            "agent_kind": "web",
            "request": self.query,
            "target_device_id": self.target_device_id,
            "required_capabilities": set(self.required_capabilities),
            "metadata": metadata,
        }
