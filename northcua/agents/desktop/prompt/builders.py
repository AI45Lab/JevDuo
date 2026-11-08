"""Typed prompt builders for the desktop computer-using loop."""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from pathlib import Path
from typing import Sequence, cast

from jinja2 import Environment, StrictUndefined
from PIL import Image

from northcua.agentkernel.json_types import ProviderToolChoice, ProviderToolSpecs
from northcua.agentkernel.prompt.image_window import PromptHistoryTurn, PromptImageWindowAllocator
from northcua.core.config import PromptPolicyConfig
from northcua.core.coordinates import CoordinatePolicy, describe_coordinate_instruction
from northcua.core.enums import CoordinateSpace, ObservationArtifactKind
from northcua.core.json_types import JsonObject
from northcua.core.image_payloads import image_data_url_from_path
from northcua.core.models import ObservationArtifact, ObservationContext, WindowState

from .envelope import HistoryTurn, PromptEnvelope


class SystemPromptBuilder:
    """Renders the system prompt template."""

    def __init__(self, template_path: Path) -> None:
        """Initialize the system prompt renderer.

        Args:
            template_path: Path to the system prompt Jinja template.
        """

        self.template_path = template_path
        self.environment = Environment(
            autoescape=False, trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined
        )

    def render(
        self,
        screenshot_width: int | None,
        screenshot_height: int | None,
        *,
        desktop_environment: str,
        home_directory: str,
        coordinate_policy: CoordinatePolicy,
        step_budget: JsonObject | None = None,
    ) -> str:
        """Render the system prompt.

        Args:
            screenshot_width: Current screenshot width when known.
            screenshot_height: Current screenshot height when known.
            desktop_environment: Runtime desktop environment label exposed to
                the model, such as ``linux`` or ``remote``.
            home_directory: User home directory when known for the controlled
                desktop environment.
            coordinate_policy: Active coordinate policy for desktop action
                tools.
            step_budget: Optional current-step budget metadata.

        Returns:
            Rendered system prompt text.
        """

        template = self.environment.from_string(self.template_path.read_text(encoding="utf-8"))
        return template.render(
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            desktop_environment=desktop_environment,
            home_directory=home_directory,
            coordinate_space=coordinate_policy.input_space.value,
            coordinate_instruction=describe_coordinate_instruction(
                coordinate_policy,
                relative_surface="freshest grounding screenshot",
                absolute_surface="desktop",
            ),
            coordinate_is_normalized_1000=coordinate_policy.input_space == CoordinateSpace.NORMALIZED_1000,
            coordinate_is_image_pixels=coordinate_policy.input_space == CoordinateSpace.IMAGE_PIXELS,
            step_budget=self._step_budget_context(step_budget),
        ).strip()

    @staticmethod
    def _positive_int(value: object) -> int | None:
        """Return ``value`` as a positive integer when safe."""

        if isinstance(value, int) and value > 0:
            return value
        return None

    @classmethod
    def _step_budget_context(cls, step_budget: JsonObject | None) -> JsonObject:
        """Normalize runtime step-budget metadata for the prompt template."""

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


class ObservationPromptBuilder:
    """Renders the persistent user and observation prompt sections."""

    def __init__(self, persistent_template: Path, observation_template: Path) -> None:
        """Initialize observation prompt renderers.

        Args:
            persistent_template: Path to the long-lived task template.
            observation_template: Path to the per-observation template.
        """

        self.environment = Environment(
            autoescape=False, trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined
        )
        self.persistent_template = persistent_template
        self.observation_template = observation_template

    def render_persistent(self, task: str) -> str:
        """Render the long-lived task prompt.

        Args:
            task: User task to preserve across the round.

        Returns:
            Rendered persistent user prompt text.
        """

        template = self.environment.from_string(self.persistent_template.read_text(encoding="utf-8"))
        return template.render(task=task).strip()

    def render_observation(
        self,
        *,
        platform_name: str,
        total_image_count: int,
        history_image_count: int,
        has_current_image: bool,
        current_image_role: str,
        observation_metadata: dict[str, object] | None = None,
    ) -> str:
        """Render the observation section.

        Args:
            platform_name: Desktop platform label for the prompt.
            total_image_count: Total images attached to the prompt.
            history_image_count: Images contributed by replay history.
            has_current_image: Whether a current observation image is attached.
            current_image_role: Role label for the current image.
            observation_metadata: Optional structured observation metadata.

        Returns:
            Rendered per-observation user prompt text.
        """

        template = self.environment.from_string(self.observation_template.read_text(encoding="utf-8"))
        return template.render(
            platform_name=platform_name,
            total_image_count=total_image_count,
            history_image_count=history_image_count,
            has_current_image=has_current_image,
            current_image_role=current_image_role,
            observation_metadata=observation_metadata or {},
        ).strip()


class ToolSpecBuilder:
    """Small adapter that keeps tool specs/tool_choice attached to the envelope."""

    def build(
        self, tools: ProviderToolSpecs, tool_choice: ProviderToolChoice
    ) -> tuple[ProviderToolSpecs, ProviderToolChoice]:
        """Return tools and tool choice unchanged.

        Args:
            tools: Provider tool specifications.
            tool_choice: Provider tool-choice setting.

        Returns:
            The original tools and tool-choice setting.
        """

        return tools, tool_choice


class PromptEnvelopeBuilder:
    """Main typed prompt-envelope builder."""

    def __init__(
        self,
        template_root: Path,
        policy: PromptPolicyConfig,
        *,
        coordinate_policy: CoordinatePolicy | None = None,
    ) -> None:
        """Initialize prompt-envelope builders and image-window allocator.

        Args:
            template_root: Directory containing prompt templates.
            policy: Prompt policy controlling projection and image windows.
            coordinate_policy: Desktop action coordinate policy. Defaults to
                screenshot pixel coordinates for backward compatibility.
        """

        self.policy = policy
        self.coordinate_policy = coordinate_policy or CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS)
        self.template_root = template_root
        self.system_builder = SystemPromptBuilder(template_root / "computer_using_agent_system.j2")
        self.observation_builder = ObservationPromptBuilder(
            template_root / "computer_using_agent_persistent_user.j2",
            template_root / "computer_using_agent_observation_user.j2",
        )
        self.image_window_allocator = PromptImageWindowAllocator(policy)
        self.tool_builder = ToolSpecBuilder()

    def build(
        self,
        *,
        task: str,
        platform_name: str,
        current_window: WindowState,
        history_turns: Sequence[HistoryTurn],
        observation_context: ObservationContext | None = None,
        current_clean_screenshot_url: str | None = None,
        step_budget: JsonObject | None = None,
        tools: ProviderToolSpecs,
        tool_choice: ProviderToolChoice,
    ) -> PromptEnvelope:
        """Build the current prompt envelope.

        Args:
            task: User task that remains in the persistent prompt section.
            platform_name: Human-readable platform name used when no observation
                context overrides it.
            current_window: Current desktop window state for legacy callers.
            history_turns: Prior turns eligible for prompt replay.
            observation_context: Structured observation payload for the active
                round, if available.
            current_clean_screenshot_url: Legacy current screenshot data URL.
            step_budget: Optional current-step budget metadata.
            tools: Provider tool specifications to attach to the envelope.
            tool_choice: Provider tool-choice policy to attach unchanged.

        Returns:
            A provider-agnostic prompt envelope with rendered text, selected
            images, replay history, and tool metadata.
        """

        if observation_context is None:
            observation_context = ObservationContext(
                platform_name=platform_name,
                current_window=current_window,
                current_clean_screenshot_url=current_clean_screenshot_url or "",
                current_artifacts=self._legacy_artifacts_from_url(current_clean_screenshot_url),
            )
        else:
            platform_name = observation_context.platform_name or platform_name
            current_window = observation_context.current_window or current_window

        image_plan = self.image_window_allocator.plan_history_only(
            history_turns=list(history_turns or []),
            current_clean_screenshot_url=observation_context.current_clean_screenshot_url
            or image_data_url_from_path(observation_context.current_clean_screenshot_path),
        )
        history_messages = []
        replay_turns = cast(list[HistoryTurn], image_plan.history_turns)
        for turn in replay_turns:
            history_messages.extend(turn.to_messages())

        observation_images = image_plan.observation_images
        grounding_image_url = self._latest_grounding_image_url(observation_images, image_plan.history_turns)
        screenshot_resolution = self._detect_resolution(grounding_image_url)
        history_image_count = self.image_window_allocator.count_history_images(image_plan.history_turns)
        total_image_count = len(observation_images) + history_image_count
        desktop_environment = self._desktop_environment(observation_context, platform_name)
        home_directory = self._home_directory(observation_context)
        system_prompt = self.system_builder.render(
            screenshot_width=screenshot_resolution[0] if screenshot_resolution else None,
            screenshot_height=screenshot_resolution[1] if screenshot_resolution else None,
            desktop_environment=desktop_environment,
            home_directory=home_directory,
            coordinate_policy=self.coordinate_policy,
            step_budget=step_budget,
        )
        persistent_user_text = self.observation_builder.render_persistent(task=task)
        prompt_observation_metadata = dict(observation_context.observation_metadata)
        observation_user_text = self.observation_builder.render_observation(
            platform_name=platform_name,
            total_image_count=total_image_count,
            history_image_count=history_image_count,
            has_current_image=bool(observation_images),
            current_image_role=image_plan.current_image_role,
            observation_metadata=prompt_observation_metadata,
        )
        observation_notes = self._observation_notes(observation_context)
        if observation_notes:
            observation_user_text = "\n\n".join([observation_user_text, *observation_notes])
        envelope_tools, envelope_choice = self.tool_builder.build(tools, tool_choice)
        return PromptEnvelope(
            system_prompt=system_prompt,
            persistent_user_text=persistent_user_text,
            observation_user_text=observation_user_text,
            observation_images=observation_images,
            history_messages=history_messages,
            tools=envelope_tools,
            tool_choice=envelope_choice,
        )

    @staticmethod
    def _legacy_artifacts_from_url(screenshot_url: str | None) -> list[ObservationArtifact]:
        """Adapt a legacy screenshot URL into the structured artifact shape.

        Args:
            screenshot_url: Optional current screenshot data URL from older
                prompt-builder callers.

        Returns:
            A single image artifact when a URL is present, otherwise an empty
            list.
        """

        if not screenshot_url:
            return []
        return [
            ObservationArtifact(
                kind=ObservationArtifactKind.IMAGE,
                role="current_clean",
                source="legacy_prompt_builder",
                url=screenshot_url,
            )
        ]

    @staticmethod
    def _desktop_environment(observation_context: ObservationContext, fallback: str) -> str:
        """Return the single desktop environment label shown in prompts.

        Args:
            observation_context: Current observation context.
            fallback: Caller-provided platform label.

        Returns:
            Non-empty desktop environment label.
        """

        value = (
            observation_context.observation_metadata.get("desktop_environment")
            or observation_context.platform_name
            or fallback
        )
        return str(value or "unknown")

    @staticmethod
    def _home_directory(observation_context: ObservationContext) -> str:
        """Return the prompt-facing home directory.

        Args:
            observation_context: Current observation context whose metadata may
                include a sandbox/device home directory.

        Returns:
            Prompt-facing home directory string. Remote desktops use
            ``unavailable`` when the device home cannot be known without
            querying the remote device; generic local prompts omit the line.
        """

        value = observation_context.observation_metadata.get("home_directory")
        if isinstance(value, str) and value.strip():
            return value.strip()
        platform_name = str(observation_context.platform_name or "").lower()
        if platform_name.startswith("remote"):
            return "unavailable"
        return ""

    @staticmethod
    def _observation_notes(observation_context: ObservationContext) -> list[str]:
        """Collect text artifacts that should be appended to the user prompt.

        Args:
            observation_context: Structured observation state for the current
                prompt.

        Returns:
            Render-ready note lines. Empty text artifacts are intentionally
            ignored.
        """

        notes: list[str] = []
        text_notes = [
            artifact.text.strip()
            for artifact in observation_context.current_artifacts
            if artifact.kind == ObservationArtifactKind.TEXT and artifact.text.strip()
        ]
        if text_notes:
            notes.append("Additional observation notes:")
            notes.extend(f"- {text}" for text in text_notes)
        return notes

    @staticmethod
    def _detect_resolution(screenshot_url: str | None) -> tuple[int, int] | None:
        """Resolve image size from a data URL."""

        if not screenshot_url or not screenshot_url.startswith("data:image/"):
            return None
        try:
            base64_payload = screenshot_url.split(",", 1)[1]
            with Image.open(BytesIO(base64.b64decode(base64_payload))) as image:
                return image.size
        except (IndexError, ValueError, binascii.Error, OSError):
            return None

    @staticmethod
    def _latest_grounding_image_url(
        observation_images: Sequence[str],
        history_turns: Sequence[PromptHistoryTurn],
    ) -> str | None:
        """Return the newest prompt image used for coordinate grounding.

        Current observation images take precedence. In history-only projection,
        the freshest image is usually the latest tool-result screenshot selected
        into replay history, so resolution metadata must come from history
        rather than showing ``unavailable``.
        """

        if observation_images:
            return observation_images[-1]

        for turn in reversed(history_turns):
            for result in reversed(turn.tool_results):
                image_url = result.get("image_url")
                if isinstance(image_url, str) and image_url:
                    return image_url
        return None
