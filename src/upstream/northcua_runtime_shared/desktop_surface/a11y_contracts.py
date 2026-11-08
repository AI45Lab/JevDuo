"""Tool contracts for desktop-native accessibility tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from northcua_protocol.capabilities import Capability, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import McpBinding, ToolContract, ToolVisibility

DESKTOP_NATIVE_A11Y_SERVER_NAME = "DesktopNativeA11yExecutor"


class DesktopA11yParams(BaseModel):
    """Base parameter model for desktop accessibility tools."""

    model_config = ConfigDict(extra="forbid")


class DesktopA11yCapabilitiesParams(DesktopA11yParams):
    """Parameters for accessibility capability discovery."""


class DesktopAccessibilityTreeParams(DesktopA11yParams):
    """Parameters for normalized accessibility tree capture."""

    scope: Literal["desktop", "active_window"] = Field(
        default="desktop",
        description="Capture scope: desktop for the whole desktop, or active_window for the focused window.",
    )
    max_depth: int = Field(default=50, description="Maximum recursion depth for the normalized tree.")
    max_children_per_node: int = Field(
        default=256,
        description="Maximum number of retained children for any one node.",
    )
    max_nodes: int = Field(default=5000, description="Maximum total number of nodes retained in the normalized tree.")
    include_raw_payload: bool = Field(
        default=False,
        description="Set to true to include an optional raw sidecar payload in the response.",
    )
    capture_timeout_seconds: float = Field(
        default=3.0, description="Maximum time budget for one accessibility capture."
    )


_A11Y_TOOL_MODELS: tuple[tuple[str, type[BaseModel], float], ...] = (
    ("get_accessibility_capabilities", DesktopA11yCapabilitiesParams, 10.0),
    ("capture_accessibility_tree", DesktopAccessibilityTreeParams, 30.0),
)


def desktop_a11y_contracts() -> tuple[ToolContract, ...]:
    """Return source-of-truth contracts for desktop-native accessibility tools."""

    return tuple(
        ToolContract(
            tool_id=tool_id,
            namespace="desktop_native",
            agent_kind="desktop",
            visibility=ToolVisibility.RUNTIME_ONLY,
            capability=Capability.A11Y_TREE,
            params_model=params_model,
            timeout_seconds=timeout_seconds,
            side_effect=False,
            requires_focus=False,
            risk_level=ToolRiskLevel.LOW,
            binding=McpBinding(server_name=DESKTOP_NATIVE_A11Y_SERVER_NAME),
        )
        for tool_id, params_model, timeout_seconds in _A11Y_TOOL_MODELS
    )


__all__ = [
    "DESKTOP_NATIVE_A11Y_SERVER_NAME",
    "DesktopA11yCapabilitiesParams",
    "DesktopAccessibilityTreeParams",
    "desktop_a11y_contracts",
]
