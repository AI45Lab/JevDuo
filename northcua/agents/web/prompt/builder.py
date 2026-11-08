"""Prompt construction for the generic NorthCUA web agent."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jinja2 import Environment, StrictUndefined
from nexau.core.messages import (
    DiscriminatedBlock,
    ImageBlock,
    Message,
    Role,
    TextBlock,
)

from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.agentkit.prompt_history import PromptHistoryReplayer, StepHistoryTrimmer
from northcua.core.config import PromptPolicyConfig
from northcua.core.image_payloads import image_data_url_from_path
from northcua.core.models import ToolExecutionRecord

from ..domain.observations import WebObservationContext
from ..observation.projector import WebObservationProjector
from .history import WebHistoryToolResultProjector, WebHistoryToolUseBlockFactory
from .image_window import WebPromptImageWindowBudgeter, WebPromptImageWindowResult
from .ref_window import WebRefSnapshotWindowBudgeter
from .visual_window import WebPromptVisualHistoryTurn, WebPromptVisualMessageProjector, WebPromptVisualWindowResult


class WebTemplateRenderer:
    """Small Jinja-backed prompt section renderer."""

    def __init__(self, template_path: Path) -> None:
        self.template_path = template_path
        self.environment = Environment(
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )

    def render(self, **context: object) -> str:
        """Render the template with the provided context.

        Args:
            **context: Jinja variables required by the selected template.

        Returns:
            Whitespace-trimmed prompt section ready to place in a text block.
        """

        template = self.environment.from_string(self.template_path.read_text(encoding="utf-8"))
        return template.render(**context).strip()


@dataclass(slots=True)
class WebAgentPromptEnvelope:
    """Provider-agnostic prompt payload for one web-agent step.

    Attributes:
        messages: Ordered provider messages for the current model step.
        tools: Contract-projected provider tool payload exposed for this step.
        tool_choice: Optional provider-specific tool-choice directive.
    """

    messages: list[Message] = field(default_factory=list)
    tools: list[dict[str, object]] = field(default_factory=list)
    tool_choice: dict[str, object] | str | None = None


WebPromptHistoryStrategy = Literal["desktop", "append"]


class WebAgentPromptBuilder:
    """Build DOM-first prompt envelopes for web-agent steps."""

    def __init__(
        self,
        *,
        template_root: Path | None = None,
        history_window_rounds: int = 8,
        history_strategy: WebPromptHistoryStrategy = "desktop",
        screenshot_window_images: int = 3,
        ref_snapshot_window_observations: int = 1,
        include_initial_observation_image: bool = True,
        include_subsequent_observation_images: bool = True,
    ) -> None:
        self.template_root = template_root or (Path(__file__).resolve().parent / "templates")
        self.observation_renderer = WebTemplateRenderer(self.template_root / "web_agent_observation_user.j2")
        self.history_window_rounds = max(0, history_window_rounds)
        self.history_strategy = history_strategy
        self.history_replayer = PromptHistoryReplayer(
            tool_use_factory=WebHistoryToolUseBlockFactory(),
            tool_result_projector=WebHistoryToolResultProjector(),
        )
        self.observation_projector = WebObservationProjector()
        self.screenshot_window_images = max(0, int(screenshot_window_images))
        self.ref_snapshot_window_observations = max(0, int(ref_snapshot_window_observations))
        self.include_initial_observation_image = include_initial_observation_image
        self.include_subsequent_observation_images = include_subsequent_observation_images
        self.prompt_policy = PromptPolicyConfig(
            history_window_rounds=self.history_window_rounds,
            screenshot_window_images=self.screenshot_window_images,
            ref_snapshot_window_observations=self.ref_snapshot_window_observations,
            include_initial_observation_image=include_initial_observation_image,
            include_subsequent_observation_images=include_subsequent_observation_images,
        )
        self.image_window_budgeter = WebPromptImageWindowBudgeter(
            screenshot_window_images=self.screenshot_window_images,
            include_initial_observation_image=include_initial_observation_image,
            include_subsequent_observation_images=include_subsequent_observation_images,
        )
        self.visual_message_projector = WebPromptVisualMessageProjector(self.prompt_policy)
        self.ref_snapshot_window_budgeter = WebRefSnapshotWindowBudgeter(
            max_history_snapshots=self.ref_snapshot_window_observations,
        )

    def build_system_prompt_context(self, *, step_budget: dict[str, object] | None = None) -> dict[str, object]:
        """Return context for the agent-level Jinja system prompt.

        Args:
            step_budget: Optional shared runtime step-budget metadata for the
                current model step.

        Returns:
            Template variables used by the static web-agent system prompt.
        """

        return {"step_budget": self._step_budget_context(step_budget)}

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if isinstance(value, int) and value > 0:
            return value
        return None

    @classmethod
    def _step_budget_context(cls, step_budget: dict[str, object] | None) -> dict[str, object]:
        if not isinstance(step_budget, dict):
            return {
                "available": False,
                "round_limit": None,
            }
        round_limit = cls._positive_int(step_budget.get("round_limit"))
        return {
            "available": bool(round_limit),
            "round_limit": round_limit,
        }

    def build(
        self,
        *,
        user_query: str,
        observation: WebObservationContext,
        prior_messages: list[Message],
        tools: list[dict[str, object]],
        tool_choice: dict[str, object] | str | None,
        allow_current_observation_image: bool = True,
        visual_history_turns: list[WebPromptVisualHistoryTurn] | None = None,
    ) -> WebAgentPromptEnvelope:
        """Build one provider-facing prompt envelope.

        Args:
            user_query: Original user task for the current round.
            observation: Latest browser snapshot and optional screenshot.
            prior_messages: Conversation/tool history from previous steps.
            tools: Model-visible tool descriptors for this step.
            tool_choice: Provider-specific tool routing instruction.
            allow_current_observation_image: Whether the current observation
                may attach its screenshot image. Web mirrors desktop semantics:
                the initial observation may attach an image, while later
                visual images are carried by tool results.

        Returns:
            Prompt envelope containing ordered messages plus tool metadata.
        """

        history = self._trim_history(prior_messages)
        effective_visual_history_turns = list(visual_history_turns or [])
        latest_ref_tool_use_ids: set[str] = set()
        if effective_visual_history_turns:
            history = self.ref_snapshot_window_budgeter.apply(history)
            latest_ref_tool_use_ids = self.ref_snapshot_window_budgeter.latest_retained_tool_result_ids(history)
        render_current_observation = self.history_strategy == "append" or not history
        observation_image_url = self._observation_image_url(observation)
        current_image_url = (
            observation_image_url if render_current_observation or effective_visual_history_turns else ""
        )
        image_plan = self._apply_image_window(
            history_messages=history,
            current_image_url=current_image_url,
            allow_current_image=allow_current_observation_image
            and (render_current_observation or bool(effective_visual_history_turns)),
            current_image_count=sum(
                isinstance(block, ImageBlock)
                for block in self._observation_image_blocks(observation, include_image=True)
            ),
            visual_history_turns=effective_visual_history_turns,
            latest_ref_tool_use_ids=latest_ref_tool_use_ids,
        )
        history = image_plan.history_messages
        if not effective_visual_history_turns:
            history = self.ref_snapshot_window_budgeter.apply(history)
        if self.history_strategy == "append":
            task_message = Message(role=Role.USER, content=[TextBlock(text=f"Task:\n{user_query.strip()}")])
            observation_message = Message(
                role=Role.USER,
                content=self._observation_blocks(
                    observation,
                    include_image=image_plan.include_current_image,
                    image_omitted_reason=image_plan.current_image_omitted_reason,
                    observation_heading="Latest page observation",
                ),
            )
            messages = [*history, observation_message] if history else [task_message, observation_message]
        else:
            if history:
                messages = (
                    history
                    if self._has_desktop_task_prefix(history)
                    else [
                        Message(role=Role.USER, content=[TextBlock(text=f"Task:\n{user_query.strip()}")]),
                        *history,
                    ]
                )
            else:
                task_message = self._task_observation_message(
                    user_query=user_query,
                    observation=observation,
                    include_image=image_plan.include_current_image,
                    image_omitted_reason=image_plan.current_image_omitted_reason,
                )
                messages = [task_message]
        return WebAgentPromptEnvelope(messages=messages, tools=tools, tool_choice=tool_choice)

    @staticmethod
    def _observation_image_url(observation: WebObservationContext) -> str:
        """Return the best prompt-safe image URL for the latest observation."""

        return WebAgentPromptBuilder._clean_observation_image_url(observation)

    @staticmethod
    def _clean_observation_image_url(observation: WebObservationContext) -> str:
        """Return the clean screenshot URL for a prompt observation."""

        screenshot = observation.screenshot
        if screenshot is None:
            return ""
        annotation = screenshot.annotation
        if annotation is not None:
            source_url = image_data_url_from_path(annotation.source_screenshot_path)
            if source_url:
                return source_url
        if screenshot.data_url:
            return screenshot.data_url
        return image_data_url_from_path(screenshot.path)

    @staticmethod
    def _som_observation_image_url(observation: WebObservationContext) -> str:
        """Return the SoM screenshot URL for a prompt observation when present."""

        screenshot = observation.screenshot
        if screenshot is None or screenshot.annotation is None:
            return ""
        annotation = screenshot.annotation
        overlay_url = image_data_url_from_path(annotation.overlay_screenshot_path)
        if overlay_url:
            return overlay_url
        if screenshot.data_url:
            return screenshot.data_url
        return image_data_url_from_path(screenshot.path)

    def _apply_image_window(
        self,
        *,
        history_messages: list[Message],
        current_image_url: str,
        allow_current_image: bool,
        current_image_count: int,
        visual_history_turns: list[WebPromptVisualHistoryTurn],
        latest_ref_tool_use_ids: set[str],
    ) -> WebPromptImageWindowResult | WebPromptVisualWindowResult:
        if visual_history_turns:
            return self.visual_message_projector.apply(
                history_messages=history_messages,
                visual_history_turns=visual_history_turns,
                current_image_url=current_image_url,
                allow_current_image=allow_current_image,
                current_image_omitted_reason="visual history carries latest tool-result images",
                latest_ref_tool_use_ids=latest_ref_tool_use_ids,
            )
        return self.image_window_budgeter.apply(
            history_messages=history_messages,
            current_image_url=current_image_url,
            allow_current_image=allow_current_image,
            current_image_count=current_image_count,
        )

    def record_assistant_step(self, *, assistant_text: str) -> Message:
        """Return an assistant history message for the next step.

        Args:
            assistant_text: User-visible assistant text emitted by the model.

        Returns:
            Assistant message that can be appended to prompt history.
        """

        return Message(role=Role.ASSISTANT, content=[TextBlock(text=assistant_text.strip())])

    def record_tool_result_step(self, *, tool_name: str, text: str) -> Message:
        """Return a compact tool-result summary as user-visible history.

        Args:
            tool_name: Name of the executed tool.
            text: Human-readable result summary returned by the tool layer.

        Returns:
            User-role history message that preserves the tool result for the
            next model step.
        """

        return Message(role=Role.USER, content=[TextBlock(text=f"Tool result from {tool_name}:\n{text.strip()}")])

    def append_step_history(
        self,
        *,
        envelope: WebAgentPromptEnvelope | None,
        prior_messages: list[Message],
        trace_state: TraceCaptureState,
        tool_executions: list[ToolExecutionRecord] | None = None,
    ) -> list[Message]:
        """Append structured assistant tool-use and tool-result messages.

        Args:
            envelope: Prompt envelope used for the completed step when the
                strategy wants to retain the full sent message list.
            prior_messages: Existing prompt history before this step.
            trace_state: Captured model text, reasoning summary, and tool calls.
            tool_executions: Tool execution records to replay as tool results.

        Returns:
            Updated prompt history for the next step.
        """

        base_messages = prior_messages
        if envelope is not None:
            if self.history_strategy == "append":
                base_messages = envelope.messages
            elif not prior_messages:
                base_messages = self._desktop_initial_history_prefix(envelope.messages)
        return self.history_replayer.append_tool_step_history(
            messages=base_messages,
            trace_state=trace_state,
            tool_executions=tool_executions,
        )

    def _trim_history(self, messages: list[Message]) -> list[Message]:
        fixed_prefix_count = 0
        if self.history_strategy == "append" and messages:
            fixed_prefix_count = 1
        elif self.history_strategy == "desktop" and messages and self._is_desktop_initial_history_message(messages[0]):
            fixed_prefix_count = 1
        return StepHistoryTrimmer(
            history_window_steps=self.history_window_rounds,
            fixed_prefix_count=fixed_prefix_count,
        ).trim(messages)

    def _desktop_initial_history_prefix(self, messages: list[Message]) -> list[Message]:
        """Retain the Step 1 user observation as desktop-style fixed history."""

        if not messages:
            return []
        if not self._is_desktop_initial_history_message(messages[0]):
            return []
        return [copy.deepcopy(messages[0])]

    def _has_desktop_task_prefix(self, messages: list[Message]) -> bool:
        if not messages:
            return False
        message = messages[0]
        if message.role != Role.USER:
            return False
        return any(isinstance(block, TextBlock) and "Task:\n" in block.text for block in message.content)

    def _is_desktop_initial_history_message(self, message: Message) -> bool:
        if message.role != Role.USER:
            return False
        return any(
            isinstance(block, TextBlock)
            and "Task:\n" in block.text
            and ("Initial page observation:" in block.text or "Current page:" in block.text)
            for block in message.content
        )

    def _observation_blocks(
        self,
        observation: WebObservationContext,
        *,
        include_image: bool,
        image_omitted_reason: str,
        observation_heading: str = "Page observation",
    ) -> list[DiscriminatedBlock]:
        snapshot = observation.snapshot
        annotation = observation.screenshot.annotation if observation.screenshot else None
        refs = self.observation_projector.snapshot_refs(snapshot, annotation=annotation)
        image_blocks = self._observation_image_blocks(observation, include_image=include_image)
        observation_text = self.observation_renderer.render(
            observation_heading=observation_heading,
            page_title=snapshot.title or "(untitled)",
            page_url=snapshot.url or snapshot.origin or "(blank)",
            refs=refs,
            ref_count=len(refs),
            observation_id=observation.observation_id,
            settled=observation.settled,
            settle_reason=observation.settle_reason,
            screenshot_kind=(
                observation.screenshot.annotation.kind
                if observation.screenshot and observation.screenshot.annotation
                else ("viewport" if observation.screenshot else "none")
            ),
            screenshot_image_available=bool(observation.screenshot and observation.screenshot.data_url),
            screenshot_image_attached=bool(image_blocks),
            screenshot_image_omitted_reason=self._current_image_omitted_reason(
                observation=observation,
                include_image=include_image,
                image_omitted_reason=image_omitted_reason,
                image_attached=bool(image_blocks),
            ),
            screenshot_marks=self._som_marks_for_prompt(observation),
            screenshot_marks_omitted_count=(
                observation.screenshot.annotation.omitted_count
                if observation.screenshot and observation.screenshot.annotation
                else 0
            ),
            screenshot_marks_truncated=(
                observation.screenshot.annotation.truncated
                if observation.screenshot and observation.screenshot.annotation
                else False
            ),
        )
        return [TextBlock(text=observation_text), *image_blocks]

    def _observation_image_blocks(
        self,
        observation: WebObservationContext,
        *,
        include_image: bool,
    ) -> list[DiscriminatedBlock]:
        """Return role-labeled observation images aligned with Desktop/Web semantics."""

        if not include_image or self.screenshot_window_images <= 0 or observation.screenshot is None:
            return []
        clean_url = self._clean_observation_image_url(observation)
        if not clean_url:
            return []
        blocks: list[DiscriminatedBlock] = [
            TextBlock(text="Current clean page screenshot."),
            ImageBlock(url=clean_url),
        ]
        som_url = self._som_observation_image_url(observation)
        if (
            som_url
            and self.screenshot_window_images >= 2
            and som_url != clean_url
            and self.ref_snapshot_window_observations > 0
            and observation.screenshot.annotation is not None
        ):
            blocks.extend(
                [
                    TextBlock(text="Current Set-of-Mark image for actionable refs in this observation."),
                    ImageBlock(url=som_url),
                ]
            )
        return blocks

    def _task_observation_message(
        self,
        *,
        user_query: str,
        observation: WebObservationContext,
        include_image: bool,
        image_omitted_reason: str,
    ) -> Message:
        """Return the desktop-like leading user message for one prompt step."""

        observation_blocks = self._observation_blocks(
            observation,
            include_image=include_image,
            image_omitted_reason=image_omitted_reason,
            observation_heading="Initial page observation",
        )
        observation_text = ""
        image_blocks: list[ImageBlock] = []
        for block in observation_blocks:
            if isinstance(block, TextBlock):
                observation_text = "\n\n".join(part for part in [observation_text, block.text] if part)
            elif isinstance(block, ImageBlock):
                image_blocks.append(block)
        combined_text = "\n\n".join(
            part
            for part in [
                f"Task:\n{user_query.strip()}",
                observation_text,
            ]
            if part
        )
        return Message(role=Role.USER, content=[TextBlock(text=combined_text), *image_blocks])

    def _som_marks_for_prompt(self, observation: WebObservationContext) -> list[dict[str, str]]:
        screenshot = observation.screenshot
        annotation = screenshot.annotation if screenshot else None
        if annotation is None:
            return []
        marks: list[dict[str, str]] = []
        for mark in annotation.marks:
            marks.append(
                {
                    "label": mark.label,
                    "ref": mark.ref,
                    "role": mark.role,
                    "name": mark.name,
                    "bbox": self._format_bbox(
                        mark.bbox_css.x, mark.bbox_css.y, mark.bbox_css.width, mark.bbox_css.height
                    ),
                }
            )
        return marks

    def _format_bbox(self, x: float, y: float, width: float, height: float) -> str:
        return f"{self._format_number(x)},{self._format_number(y)},{self._format_number(width)},{self._format_number(height)}"

    def _format_number(self, value: float) -> str:
        rounded = round(float(value), 1)
        return str(int(rounded)) if rounded.is_integer() else f"{rounded:g}"

    def _current_image_omitted_reason(
        self,
        *,
        observation: WebObservationContext,
        include_image: bool,
        image_omitted_reason: str,
        image_attached: bool,
    ) -> str:
        if not observation.screenshot:
            return "no screenshot captured"
        if include_image and image_attached:
            return ""
        return image_omitted_reason
