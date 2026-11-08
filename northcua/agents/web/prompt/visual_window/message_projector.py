"""Apply Desktop-style Web visual windows to replayed prompt messages."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Sequence

from nexau.core.messages import ImageBlock, Message, TextBlock, ToolResultBlock, ToolResultContentBlock

from northcua.agentkernel.context.domain.presentation import ContextPresentationNotice
from northcua.agentkernel.prompt.image_window import PromptImageWindowAllocator
from northcua.core.config import PromptPolicyConfig
from northcua.core.image_payloads import image_data_url_from_path

from ...domain.evaluation_artifacts import WebToolResultArtifact
from .domain import WebPromptImageRole, WebPromptVisualHistoryTurn


@dataclass(slots=True)
class WebPromptVisualWindowResult:
    """Prompt messages after applying Web/Desktop image-window selection."""

    history_messages: list[Message]
    include_current_image: bool
    current_image_omitted_reason: str = ""


class WebPromptVisualHistoryFactory:
    """Create prompt-only visual history turns from typed Web artifacts."""

    def build_turn(
        self,
        *,
        session_step: int,
        round_num: int,
        tool_artifacts: Sequence[WebToolResultArtifact],
    ) -> WebPromptVisualHistoryTurn | None:
        """Build one visual history turn for a completed model step."""

        tool_results = [self._tool_result_payload(artifact) for artifact in tool_artifacts]
        tool_results = [payload for payload in tool_results if self._has_any_image(payload)]
        if not tool_results:
            return None
        return WebPromptVisualHistoryTurn(
            session_step=session_step,
            round_num=round_num,
            tool_results=tool_results,
        )

    @staticmethod
    def _tool_result_payload(artifact: WebToolResultArtifact) -> dict[str, object]:
        selected_path = artifact.action_overlay_image_path or artifact.raw_image_path
        observation_recovered = bool(artifact.metadata.get("observation_recovered"))
        return {
            "tool_call_id": artifact.tool_call_id,
            "tool_name": artifact.tool_name,
            "status": artifact.status,
            "is_error": artifact.is_error,
            "observation_recovered": observation_recovered,
            "prompt_image_eligible": observation_recovered,
            "raw_image_path": artifact.raw_image_path,
            "overlay_image_path": artifact.action_overlay_image_path,
            "image_path": selected_path,
            "som_image_path": artifact.som_image_path,
        }

    @staticmethod
    def _has_any_image(payload: dict[str, object]) -> bool:
        return any(
            str(payload.get(key) or "")
            for key in ("raw_image_path", "overlay_image_path", "image_path", "som_image_path")
        )


class WebPromptVisualMessageProjector:
    """Project selected visual-history images into existing prompt messages."""

    def __init__(self, policy: PromptPolicyConfig) -> None:
        self.policy = policy

    def apply(
        self,
        *,
        history_messages: list[Message],
        visual_history_turns: Sequence[WebPromptVisualHistoryTurn],
        current_image_url: str,
        allow_current_image: bool,
        current_image_omitted_reason: str,
        latest_ref_tool_use_ids: set[str] | None = None,
    ) -> WebPromptVisualWindowResult:
        """Return history messages within the total clean/overlay/SoM budget."""

        messages = self._strip_history_images(copy.deepcopy(history_messages))
        if not visual_history_turns:
            return WebPromptVisualWindowResult(
                history_messages=messages,
                include_current_image=allow_current_image
                and bool(current_image_url)
                and self.policy.screenshot_window_images > 0,
                current_image_omitted_reason=current_image_omitted_reason,
            )

        current_clean_url = (
            current_image_url
            if allow_current_image and current_image_url and self.policy.screenshot_window_images > 0
            else ""
        )
        latest_som = self._latest_som_image(visual_history_turns, allowed_tool_use_ids=latest_ref_tool_use_ids)
        desktop_budget = self.policy.screenshot_window_images - int(latest_som is not None)
        desktop_policy = self.policy.model_copy(update={"screenshot_window_images": desktop_budget})
        desktop_plan = PromptImageWindowAllocator(desktop_policy).plan_history_only(
            history_turns=list(visual_history_turns),
            current_clean_screenshot_url=current_clean_url or None,
        )
        role_by_url = self._role_by_url(visual_history_turns)
        if current_clean_url:
            role_by_url[current_clean_url] = WebPromptImageRole.CLEAN
            self._ensure_current_clean_fallback(
                desktop_plan=desktop_plan,
                current_clean_url=current_clean_url,
                role_by_url=role_by_url,
                image_budget=desktop_budget,
            )
        self._insert_observation_images(
            messages=messages,
            image_urls=desktop_plan.observation_images,
            role_by_url=role_by_url,
        )
        selected_by_call_id = self._selected_images_by_call_id(desktop_plan.history_turns, role_by_url=role_by_url)
        if latest_som is not None:
            selected_by_call_id.setdefault(latest_som[0], []).append((WebPromptImageRole.SOM, latest_som[1]))
        self._insert_selected_images(messages, selected_by_call_id)
        return WebPromptVisualWindowResult(
            history_messages=messages,
            include_current_image=False,
            current_image_omitted_reason=current_image_omitted_reason,
        )

    def _ensure_current_clean_fallback(
        self,
        *,
        desktop_plan: object,
        current_clean_url: str,
        role_by_url: dict[str, WebPromptImageRole],
        image_budget: int,
    ) -> None:
        if not current_clean_url or image_budget <= 0:
            return
        if self._has_selected_role(desktop_plan, WebPromptImageRole.CLEAN, role_by_url=role_by_url):
            return
        while self._selected_image_count(desktop_plan) >= image_budget:
            if not self._drop_oldest_selected_image(desktop_plan):
                break
        desktop_plan.observation_images.append(current_clean_url)

    def _has_selected_role(
        self,
        desktop_plan: object,
        role: WebPromptImageRole,
        *,
        role_by_url: dict[str, WebPromptImageRole],
    ) -> bool:
        for image_url in desktop_plan.observation_images:
            if role_by_url.get(image_url, WebPromptImageRole.UNKNOWN) == role:
                return True
        for turn in desktop_plan.history_turns:
            for tool_result in turn.tool_results:
                image_url = str(tool_result.get("image_url") or "")
                if image_url and role_by_url.get(image_url, WebPromptImageRole.UNKNOWN) == role:
                    return True
        return False

    @staticmethod
    def _selected_image_count(desktop_plan: object) -> int:
        count = len(desktop_plan.observation_images)
        for turn in desktop_plan.history_turns:
            for tool_result in turn.tool_results:
                if tool_result.get("image_url") or tool_result.get("image_path"):
                    count += 1
        return count

    @staticmethod
    def _drop_oldest_selected_image(desktop_plan: object) -> bool:
        if desktop_plan.observation_images:
            desktop_plan.observation_images.pop(0)
            return True
        for turn in desktop_plan.history_turns:
            for tool_result in turn.tool_results:
                if tool_result.get("image_url") or tool_result.get("image_path"):
                    tool_result["image_url"] = ""
                    tool_result["image_path"] = ""
                    return True
        return False

    def _insert_observation_images(
        self,
        *,
        messages: list[Message],
        image_urls: list[str],
        role_by_url: dict[str, WebPromptImageRole],
    ) -> None:
        if not image_urls or not messages:
            return
        target = messages[0]
        for image_url in image_urls:
            role = role_by_url.get(image_url, WebPromptImageRole.UNKNOWN)
            target.content.append(TextBlock(text=self._image_role_notice(role)))
            target.content.append(ImageBlock(url=image_url))

    def _selected_images_by_call_id(
        self,
        turns: Sequence[WebPromptVisualHistoryTurn],
        *,
        role_by_url: dict[str, WebPromptImageRole],
    ) -> dict[str, list[tuple[WebPromptImageRole, str]]]:
        selected: dict[str, list[tuple[WebPromptImageRole, str]]] = {}
        for turn in turns:
            for tool_result in turn.tool_results:
                image_url = str(tool_result.get("image_url") or "")
                if not image_url:
                    continue
                tool_call_id = str(tool_result.get("tool_call_id") or "")
                if not tool_call_id:
                    continue
                selected.setdefault(tool_call_id, []).append(
                    (
                        self._selected_tool_result_role(tool_result, image_url=image_url, role_by_url=role_by_url),
                        image_url,
                    )
                )
        return selected

    def _selected_tool_result_role(
        self,
        tool_result: dict[str, object],
        *,
        image_url: str,
        role_by_url: dict[str, WebPromptImageRole],
    ) -> WebPromptImageRole:
        selected_path = str(tool_result.get("image_path") or "")
        if selected_path:
            if selected_path == str(tool_result.get("overlay_image_path") or ""):
                return WebPromptImageRole.ACTION_OVERLAY
            if selected_path == str(tool_result.get("raw_image_path") or ""):
                return WebPromptImageRole.CLEAN
            if selected_path == str(tool_result.get("som_image_path") or ""):
                return WebPromptImageRole.SOM
        return role_by_url.get(image_url, WebPromptImageRole.UNKNOWN)

    def _role_by_url(self, turns: Sequence[WebPromptVisualHistoryTurn]) -> dict[str, WebPromptImageRole]:
        roles: dict[str, WebPromptImageRole] = {}
        for turn in turns:
            for tool_result in turn.tool_results:
                for key, role in (
                    ("raw_image_path", WebPromptImageRole.CLEAN),
                    ("raw_image_url", WebPromptImageRole.CLEAN),
                    ("overlay_image_path", WebPromptImageRole.ACTION_OVERLAY),
                    ("overlay_image_url", WebPromptImageRole.ACTION_OVERLAY),
                    ("som_image_path", WebPromptImageRole.SOM),
                    ("som_image_url", WebPromptImageRole.SOM),
                ):
                    value = str(tool_result.get(key) or "")
                    url = value if value.startswith("data:image/") else image_data_url_from_path(value)
                    if url:
                        roles[url] = role
        return roles

    def _latest_som_image(
        self,
        turns: Sequence[WebPromptVisualHistoryTurn],
        *,
        allowed_tool_use_ids: set[str] | None = None,
    ) -> tuple[str, str] | None:
        if self.policy.screenshot_window_images < 2 or self.policy.ref_snapshot_window_observations <= 0:
            return None
        for turn in reversed(list(turns)):
            for tool_result in reversed(turn.tool_results):
                if self._is_error_tool_result(tool_result) and not bool(tool_result.get("observation_recovered")):
                    continue
                tool_call_id = str(tool_result.get("tool_call_id") or "")
                if allowed_tool_use_ids is not None and tool_call_id not in allowed_tool_use_ids:
                    continue
                som_path = str(tool_result.get("som_image_path") or "")
                som_url = str(tool_result.get("som_image_url") or "") or image_data_url_from_path(som_path)
                if tool_call_id and som_url:
                    return tool_call_id, som_url
        return None

    def _insert_selected_images(
        self,
        messages: list[Message],
        selected_by_call_id: dict[str, list[tuple[WebPromptImageRole, str]]],
    ) -> None:
        for message in messages:
            for index, block in enumerate(message.content):
                if not isinstance(block, ToolResultBlock):
                    continue
                selected = selected_by_call_id.get(block.tool_use_id)
                if not selected:
                    continue
                content = self._tool_result_content_without_images(block.content)
                for role, image_url in selected:
                    content.append(TextBlock(text=self._image_role_notice(role)))
                    content.append(ImageBlock(url=image_url))
                message.content[index] = block.model_copy(update={"content": content})

    def _strip_history_images(self, messages: list[Message]) -> list[Message]:
        for message in messages:
            if any(isinstance(block, ImageBlock) for block in message.content):
                message.content = [block for block in message.content if not isinstance(block, ImageBlock)]
            for index, block in enumerate(message.content):
                if isinstance(block, ToolResultBlock):
                    content = self._tool_result_content_without_images(block.content)
                    message.content[index] = block.model_copy(update={"content": content})
        return messages

    @staticmethod
    def _tool_result_content_without_images(
        content: str | list[ToolResultContentBlock],
    ) -> list[ToolResultContentBlock]:
        if isinstance(content, str):
            return [TextBlock(text=content)] if content else []
        return [block for block in content if not isinstance(block, ImageBlock)]

    @staticmethod
    def _image_role_notice(role: WebPromptImageRole) -> str:
        if role == WebPromptImageRole.ACTION_OVERLAY:
            return ContextPresentationNotice.ACTION_OVERLAY_HISTORY_IMAGE.value
        if role == WebPromptImageRole.CLEAN:
            return ContextPresentationNotice.CLEAN_PAGE_STATE_IMAGE.value
        if role == WebPromptImageRole.SOM:
            return ContextPresentationNotice.LATEST_SET_OF_MARK_IMAGE.value
        return ContextPresentationNotice.VISUAL_HISTORY_IMAGE.value

    @staticmethod
    def _is_error_tool_result(tool_result: dict[str, object]) -> bool:
        return bool(tool_result.get("is_error")) or str(tool_result.get("status") or "") == "error"
