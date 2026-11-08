"""Low-level image selection helpers for prompt image window allocation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from northcua.agentkernel.json_types import JsonObject
from northcua.core.config import PromptPolicyConfig
from northcua.core.image_payloads import image_data_url_from_path

if TYPE_CHECKING:
    from .image_window import PromptHistoryTurn


class PromptImageWindowSelectionMixin:
    """Select and assign prompt-facing screenshot aliases on copied turns."""

    policy: PromptPolicyConfig

    @staticmethod
    def _clear_history_images(history_turns: list[PromptHistoryTurn]) -> None:
        """Clear prompt-facing selected image aliases on copied turns."""

        for turn in history_turns:
            for tool_result in turn.tool_results:
                tool_result["image_path"] = ""
                tool_result["image_url"] = ""

    def _extract_latest_turn_image(self, turn: PromptHistoryTurn, *, variant: str) -> dict[str, str] | None:
        """Return the newest complete image payload for one variant."""

        for tool_result in reversed(turn.tool_results):
            selected = self._select_image_payload(tool_result, variant=variant)
            if selected is not None:
                return selected
        return None

    def _assign_turn_image_payload(self, turn: PromptHistoryTurn, image_payload: dict[str, str]) -> bool:
        """Assign an already-selected image payload to the newest visual result."""

        for tool_result in reversed(turn.tool_results):
            if not self._has_image_payload(tool_result):
                continue
            tool_result["image_path"] = image_payload["path"]
            tool_result["image_url"] = image_payload["url"]
            return True
        return False

    @staticmethod
    def _has_image_payload(tool_result: JsonObject) -> bool:
        """Return whether a tool result has a real prompt-eligible image."""

        if not PromptImageWindowSelectionMixin._is_prompt_image_eligible(tool_result):
            return False
        for path_key, url_key in (
            ("raw_image_path", "raw_image_url"),
            ("overlay_image_path", "overlay_image_url"),
            ("image_path", "image_url"),
        ):
            if tool_result.get(path_key) or tool_result.get(url_key):
                return True
        return False

    @staticmethod
    def _select_image_payload(
        tool_result: JsonObject,
        *,
        variant: str,
    ) -> dict[str, str] | None:
        """Return a complete path/url image payload for the requested variant."""

        if not PromptImageWindowSelectionMixin._is_prompt_image_eligible(tool_result):
            return None

        def _payload(path_key: str, url_key: str) -> dict[str, str] | None:
            path = str(tool_result.get(path_key) or "")
            url = str(tool_result.get(url_key) or "") or image_data_url_from_path(path)
            if url:
                return {"path": path, "url": url}
            return None

        if variant == "raw":
            return _payload("raw_image_path", "raw_image_url")
        if variant == "overlay":
            return _payload("overlay_image_path", "overlay_image_url")
        if variant == "selected":
            return _payload("image_path", "image_url")
        return None

    @staticmethod
    def _is_error_result(tool_result: JsonObject) -> bool:
        return bool(tool_result.get("is_error", False)) or tool_result.get("status") == "error"

    @staticmethod
    def _is_prompt_image_eligible(tool_result: JsonObject) -> bool:
        """Return whether persisted image evidence may enter the prompt window.

        Error results remain ineligible by default. Domain adapters may opt in
        a valid observation attached after the failed action, without changing
        the tool execution's error status.
        """

        if bool(tool_result.get("prompt_image_eligible", False)):
            return True
        return not PromptImageWindowSelectionMixin._is_error_result(tool_result)

    @staticmethod
    def _clear_tool_result_images(tool_result: JsonObject) -> None:
        for key in (
            "raw_image_path",
            "raw_image_url",
            "overlay_image_path",
            "overlay_image_url",
            "image_path",
            "image_url",
        ):
            tool_result[key] = ""
