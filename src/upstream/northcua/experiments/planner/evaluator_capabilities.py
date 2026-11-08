"""Capability planning for evaluator live client tools."""

from __future__ import annotations

from pathlib import Path

from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import EvaluationToolMode, EvaluationToolPlacement
from northcua_protocol.capabilities import Capability

_STANDARD_EVALUATOR_CLIENT_TOOLS: dict[str, tuple[str, EvaluationToolPlacement]] = {
    "client_screenshot": ("capture_desktop_screenshot", EvaluationToolPlacement.CLIENT_PROBE),
    "gui_inspect": ("get_desktop_app_target_info", EvaluationToolPlacement.CLIENT_PROBE),
    "client_bash_readonly": (Capability.SHELL_EXEC.value, EvaluationToolPlacement.CLIENT_PROBE),
    "client_gui_screenshot": ("screenshot", EvaluationToolPlacement.CLIENT_MUTATING),
    "client_gui_click": ("click", EvaluationToolPlacement.CLIENT_MUTATING),
    "client_gui_type": ("type", EvaluationToolPlacement.CLIENT_MUTATING),
    "client_gui_keypress": ("keypress", EvaluationToolPlacement.CLIENT_MUTATING),
    "client_gui_wait": ("wait", EvaluationToolPlacement.CLIENT_MUTATING),
}


class EvaluatorCapabilityPlanner:
    """Infer device capabilities required by configured evaluator live tools."""

    def from_app_config_path(self, app_config_path: Path) -> set[Capability]:
        """Load app config and infer evaluator-required device capabilities.

        Args:
            app_config_path: YAML app config path referenced by the suite.

        Returns:
            Device capabilities required by configured evaluator tools.
        """

        path = app_config_path.expanduser()
        if not path.exists():
            return set()
        return self.from_app_config(NorthCUAAppConfig.from_yaml(path))

    def from_app_config(self, app_config: NorthCUAAppConfig) -> set[Capability]:
        """Infer evaluator-required device capabilities from an app config.

        Args:
            app_config: Loaded NorthCUA application config.

        Returns:
            Device capabilities needed for selected evaluator live tools.
        """

        tool_config = app_config.evaluation.tools
        if not tool_config.allow_client_live_probe:
            return set()
        configured_names = set(tool_config.client_probe_tools) | set(tool_config.client_gui_tools)
        if not configured_names and tool_config.mode == EvaluationToolMode.ALLOW_LLM_SELECTED_TOOLS:
            configured_names = set(_STANDARD_EVALUATOR_CLIENT_TOOLS)
        if not configured_names:
            return set()
        allowed_placements = set(tool_config.allowed_placements)
        capabilities: set[Capability] = set()
        for name in configured_names:
            tool = _STANDARD_EVALUATOR_CLIENT_TOOLS.get(name)
            if tool is None:
                continue
            dispatch_name, placement = tool
            if placement not in allowed_placements:
                continue
            capabilities.update(self._capabilities_for_dispatch_name(dispatch_name))
        return capabilities

    @staticmethod
    def _capabilities_for_dispatch_name(dispatch_name: str) -> set[Capability]:
        if dispatch_name == Capability.SHELL_EXEC.value:
            return {Capability.SHELL_EXEC}
        if dispatch_name in {"capture_desktop_screenshot", "screenshot"}:
            return {Capability.DESKTOP_SCREENSHOT}
        if dispatch_name == "get_desktop_app_target_info":
            return {Capability.DESKTOP_WINDOW}
        if dispatch_name in {"click", "type", "keypress", "wait"}:
            return {Capability.DESKTOP_INPUT}
        return set()


__all__ = ["EvaluatorCapabilityPlanner"]
