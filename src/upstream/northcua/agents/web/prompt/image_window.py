"""Desktop-like prompt image-window budgeting for web-agent messages."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass

from nexau.core.messages import ImageBlock, Message, Role, TextBlock, ToolResultBlock

from northcua.agentkernel.context.domain.presentation import ContextPresentationNotice
from northcua.core.config import PromptPolicyConfig


@dataclass(slots=True, frozen=True)
class _HistoryImageBinding:
    """Mapping from allocator turn index back to a prompt message block."""

    message_index: int
    block_index: int
    content_index: int | None
    image_url: str
    binding_index: int


@dataclass(slots=True)
class WebPromptImageWindowResult:
    """Budgeted prompt history and current observation image decision."""

    history_messages: list[Message]
    include_current_image: bool
    current_image_omitted_reason: str = ""


class WebPromptImageWindowBudgeter:
    """Apply legacy generic image-window budgeting to web prompt history.

    The production visual-history path uses
    :class:`WebPromptVisualMessageProjector`, which delegates clean/action-
    overlay selection to the shared Desktop allocator and then appends Web's
    latest Set-of-Mark image. This fallback remains for prompt builds that do
    not have typed visual-history artifacts yet; it simply budgets already
    embedded prompt images in chronological order without inferring image
    roles.
    """

    def __init__(
        self,
        *,
        screenshot_window_images: int,
        include_initial_observation_image: bool,
        include_subsequent_observation_images: bool,
    ) -> None:
        self.policy = PromptPolicyConfig(
            screenshot_window_images=max(0, int(screenshot_window_images)),
            include_initial_observation_image=include_initial_observation_image,
            include_subsequent_observation_images=include_subsequent_observation_images,
        )

    def apply(
        self,
        *,
        history_messages: list[Message],
        current_image_url: str,
        allow_current_image: bool = True,
        current_image_count: int = 1,
    ) -> WebPromptImageWindowResult:
        """Return prompt history and current-image decision within budget."""

        copied_messages = copy.deepcopy(history_messages)
        self._strip_non_initial_top_level_history_images(copied_messages)
        bindings = self._history_image_bindings(copied_messages)
        include_current = self._include_current_image(
            current_image_url=current_image_url,
            allow_current_image=allow_current_image,
            has_history=bool(history_messages),
        )
        remaining_history_budget = max(
            self.policy.screenshot_window_images - (max(1, current_image_count) if include_current else 0), 0
        )
        selected = self._latest_binding_indices(bindings, remaining=remaining_history_budget)
        self._apply_selected_history_images(copied_messages, bindings, selected)
        return WebPromptImageWindowResult(
            history_messages=copied_messages,
            include_current_image=include_current,
            current_image_omitted_reason=self._current_image_omitted_reason(
                current_image_url=current_image_url,
                has_history=bool(history_messages),
                include_current=include_current,
                allow_current_image=allow_current_image,
            ),
        )

    def _history_image_bindings(self, messages: list[Message]) -> list[_HistoryImageBinding]:
        bindings: list[_HistoryImageBinding] = []
        for message_index, message in enumerate(messages):
            for block_index, block in enumerate(message.content):
                if isinstance(block, ImageBlock):
                    if self._is_initial_observation_message(messages, message_index):
                        image_url = str(block.url or "")
                        binding_index = len(bindings)
                        bindings.append(
                            _HistoryImageBinding(
                                message_index=message_index,
                                block_index=block_index,
                                content_index=None,
                                image_url=image_url,
                                binding_index=binding_index,
                            )
                        )
                    continue
                if isinstance(block, ToolResultBlock) and isinstance(block.content, list):
                    for content_index, content_block in enumerate(block.content):
                        if not isinstance(content_block, ImageBlock):
                            continue
                        image_url = str(content_block.url or "")
                        binding_index = len(bindings)
                        bindings.append(
                            _HistoryImageBinding(
                                message_index=message_index,
                                block_index=block_index,
                                content_index=content_index,
                                image_url=image_url,
                                binding_index=binding_index,
                            )
                        )
                    continue
        return bindings

    def _include_current_image(
        self,
        *,
        current_image_url: str,
        allow_current_image: bool,
        has_history: bool,
    ) -> bool:
        if not current_image_url or not allow_current_image or self.policy.screenshot_window_images <= 0:
            return False
        if has_history:
            return bool(self.policy.include_subsequent_observation_images)
        return bool(self.policy.include_initial_observation_image)

    def _strip_non_initial_top_level_history_images(self, messages: list[Message]) -> None:
        for message_index, message in enumerate(messages):
            if not any(isinstance(block, ImageBlock) for block in message.content):
                continue
            if self._is_initial_observation_message(messages, message_index):
                continue
            message.content = [block for block in message.content if not isinstance(block, ImageBlock)]
            if not any(
                isinstance(block, TextBlock)
                and "top-level observation image omitted from this replay turn" in block.text
                for block in message.content
            ):
                message.content.append(TextBlock(text=self.top_level_history_image_omission_notice()))

    def _is_initial_observation_message(self, messages: list[Message], message_index: int) -> bool:
        if message_index != 0:
            return False
        message = messages[message_index]
        if message.role != Role.USER:
            return False
        return any(
            isinstance(block, TextBlock)
            and "Task:\n" in block.text
            and ("Initial page observation:" in block.text or "Current page:" in block.text)
            for block in message.content
        )

    def _latest_binding_indices(self, bindings: Sequence[_HistoryImageBinding], *, remaining: int) -> set[int]:
        if remaining <= 0:
            return set()
        return {binding.binding_index for binding in bindings[-remaining:]}

    def _apply_selected_history_images(
        self,
        messages: list[Message],
        bindings: list[_HistoryImageBinding],
        selected: set[int],
    ) -> None:
        omitted_tool_blocks: set[tuple[int, int]] = set()
        for binding_index in reversed(range(len(bindings))):
            if binding_index in selected:
                continue
            binding = bindings[binding_index]
            message = messages[binding.message_index]
            block = message.content[binding.block_index]
            if binding.content_index is None:
                if binding.block_index < len(message.content) and isinstance(
                    message.content[binding.block_index], ImageBlock
                ):
                    message.content.pop(binding.block_index)
                    self._append_initial_image_omission_notice(message)
                continue
            if not isinstance(block, ToolResultBlock) or not isinstance(block.content, list):
                continue
            content = list(block.content)
            if binding.content_index < len(content) and isinstance(content[binding.content_index], ImageBlock):
                content.pop(binding.content_index)
                omitted_tool_blocks.add((binding.message_index, binding.block_index))
                message.content[binding.block_index] = block.model_copy(update={"content": content})
        for message_index, block_index in omitted_tool_blocks:
            message = messages[message_index]
            block = message.content[block_index]
            if not isinstance(block, ToolResultBlock) or not isinstance(block.content, list):
                continue
            content = list(block.content)
            if not any(
                isinstance(item, TextBlock) and "image omitted from this replay turn" in item.text for item in content
            ):
                content.append(TextBlock(text=self.history_image_omission_notice()))
                message.content[block_index] = block.model_copy(update={"content": content})

    def _append_initial_image_omission_notice(self, message: Message) -> None:
        if any(
            isinstance(block, TextBlock) and "initial observation image omitted from this replay turn" in block.text
            for block in message.content
        ):
            return
        message.content.append(TextBlock(text=self.initial_history_image_omission_notice()))

    def _current_image_omitted_reason(
        self,
        *,
        current_image_url: str,
        has_history: bool,
        include_current: bool,
        allow_current_image: bool,
    ) -> str:
        if not current_image_url:
            return "screenshot image is unavailable or exceeded the prompt image size policy"
        if include_current:
            return ""
        if not allow_current_image:
            return "latest observations are carried by retained tool results after Step 1"
        if self.policy.screenshot_window_images <= 0:
            return "prompt image-window budget is zero"
        if has_history and not self.policy.include_subsequent_observation_images:
            return "current observation image omitted by prompt policy"
        if not has_history and not self.policy.include_initial_observation_image:
            return "initial observation image omitted by prompt policy"
        return "current observation image omitted by prompt image-window allocation"

    def history_image_omission_notice(self) -> str:
        return ContextPresentationNotice.WEB_HISTORY_IMAGE_OMITTED.value

    def top_level_history_image_omission_notice(self) -> str:
        return ContextPresentationNotice.WEB_TOP_LEVEL_IMAGE_OMITTED.value

    def initial_history_image_omission_notice(self) -> str:
        return ContextPresentationNotice.WEB_INITIAL_IMAGE_OMITTED.value
