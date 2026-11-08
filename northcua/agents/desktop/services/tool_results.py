"""Build prompt-facing tool result artifacts from raw execution traces."""

from __future__ import annotations

import json
import logging
import os
import shutil

from northcua.agentkernel.actions.protocol import ActionCommandInfo
from northcua.agentkernel.json_types import JsonObject
from northcua.app.logging import log_runtime_event
from northcua.core.config import PromptPolicyConfig
from northcua.core.enums import ResultStatus, ScreenshotSelectionKind
from northcua.core.image_payloads import image_data_url_from_path
from northcua.core.models import ToolExecutionRecord, ToolResultArtifact
from northcua.runtime.artifacts import (
    ActionScreenshotOverlayProcessor,
    NorthCUAArtifactStore,
    ScreenshotOverlayService,
    persist_data_url_image,
)

logger = logging.getLogger(__name__)
_NO_SCREENSHOT_POLICY = "none"


class ToolResultService:
    """Select raw/overlay images and build persisted tool result artifacts.

    Each tool result can expose three image concepts:
    - ``raw_image_*`` is the clean post-tool screenshot returned by execution.
    - ``overlay_image_*`` is the pre-action screenshot annotated with the
      action trace when overlay capture is enabled.
    - ``image_*`` is the prompt-selected alias, preferring overlay when present
      and otherwise raw. The alias is for prompt/history replay only; raw and
      overlay fields remain serialized separately.
    """

    def __init__(self, policy: PromptPolicyConfig, artifact_store: NorthCUAArtifactStore) -> None:
        """Initialize tool-result artifact building.

        Args:
            policy: Prompt policy controlling overlay generation.
            artifact_store: Store used to allocate screenshot artifact paths.
        """

        self.policy = policy
        self.artifact_store = artifact_store
        self.overlay_service = ScreenshotOverlayService(
            processors=[
                ActionScreenshotOverlayProcessor(
                    badges_enabled=policy.history_action_trace_badges,
                    max_badge_text=policy.history_action_trace_max_badge_text,
                )
            ]
        )

    def build(
        self,
        *,
        executions: list[ToolExecutionRecord],
        session_step: int,
        pre_action_screenshot_path: str,
        is_initial_turn: bool,
    ) -> list[ToolResultArtifact]:
        """Build artifacts from captured tool executions.

        Args:
            executions: Raw tool execution records for one CUA step.
            session_step: Current session step number.
            pre_action_screenshot_path: Clean screenshot captured before the
                action batch, used as the overlay source.
            is_initial_turn: Whether this is the first turn of the session.

        Returns:
            Persisted tool result artifacts in execution order.
        """

        artifacts: list[ToolResultArtifact] = []
        for index, execution in enumerate(executions):
            output = self._normalized_output(execution.tool_output)
            suppress_prompt_images = execution.is_error or self._suppresses_prompt_images(output)
            raw_image_path = (
                ""
                if suppress_prompt_images
                else self._persist_raw_image(
                    screenshot_path=str(output.get("screenshot_path") or ""),
                    screenshot_url=str(output.get("screenshot_url") or ""),
                    session_step=session_step,
                    tool_index=index,
                )
            )
            raw_image_url = self._encode_path(raw_image_path)
            action = ActionCommandInfo(
                function=execution.tool_name,
                arguments=execution.tool_input_pixels,
            )
            overlay_image_path, overlay_spec = (
                ("", None)
                if suppress_prompt_images
                else self._build_overlay(
                    action=action,
                    session_step=session_step,
                    tool_index=index,
                    pre_action_screenshot_path=pre_action_screenshot_path,
                    is_initial_turn=is_initial_turn,
                )
            )
            overlay_image_url = self._encode_path(overlay_image_path)
            selected_kind = (
                ScreenshotSelectionKind.OVERLAY
                if overlay_image_path
                else (ScreenshotSelectionKind.RAW if raw_image_path else ScreenshotSelectionKind.NONE)
            )
            selected_path = overlay_image_path or raw_image_path
            selected_url = overlay_image_url or raw_image_url
            artifacts.append(
                ToolResultArtifact(
                    tool_call_id=execution.tool_call_id,
                    name=execution.tool_name,
                    content=self._tool_content(execution),
                    status=ResultStatus.FAILURE.value if execution.is_error else ResultStatus.SUCCESS.value,
                    is_error=execution.is_error,
                    raw_image_path=raw_image_path,
                    raw_image_url=raw_image_url,
                    overlay_image_path=overlay_image_path,
                    overlay_image_url=overlay_image_url,
                    image_path=selected_path,
                    image_url=selected_url,
                    selected_image_kind=selected_kind,
                    source_image_path="" if suppress_prompt_images else pre_action_screenshot_path,
                    overlay_spec=overlay_spec,
                    action_function=execution.tool_name,
                )
            )
            log_runtime_event(
                logger,
                "tool_result_artifact_built",
                level=logging.DEBUG,
                session_step=session_step,
                tool_index=index + 1,
                tool_name=execution.tool_name,
                tool_call_id=execution.tool_call_id,
                selected_image_kind=selected_kind,
                raw_image_path=raw_image_path,
                overlay_image_path=overlay_image_path,
                selected_image_path=selected_path,
                is_error=execution.is_error,
                suppress_prompt_images=suppress_prompt_images,
            )
        log_runtime_event(
            logger,
            "tool_result_artifacts_complete",
            level=logging.DEBUG,
            session_step=session_step,
            artifact_count=len(artifacts),
            selected_image_kinds=[artifact.selected_image_kind for artifact in artifacts],
        )
        return artifacts

    @staticmethod
    def _suppresses_prompt_images(output: JsonObject) -> bool:
        """Return whether a tool result opts out of screenshot/overlay history.

        Text-only tools such as ``note`` must not consume screenshot history or
        synthesize an action overlay from the pre-action frame. They can set
        ``screenshot_policy=none`` at the MCP boundary to stay purely textual.
        """

        return str(output.get("screenshot_policy") or "").lower() == _NO_SCREENSHOT_POLICY

    def _build_overlay(
        self,
        *,
        action: ActionCommandInfo,
        session_step: int,
        tool_index: int,
        pre_action_screenshot_path: str,
        is_initial_turn: bool,
    ) -> tuple[str, JsonObject | None]:
        """Render an optional action overlay for one tool result.

        Args:
            action: Tool command info used by overlay renderers.
            session_step: Current session step number.
            tool_index: Zero-based tool index within the step.
            pre_action_screenshot_path: Clean screenshot captured before the action.
            is_initial_turn: Whether this is the first turn of the session.

        Returns:
            Overlay image path and overlay metadata when rendering succeeded.
        """

        if (
            not self.policy.history_action_trace_enabled
            or not pre_action_screenshot_path
            or not os.path.exists(pre_action_screenshot_path)
            or (is_initial_turn and not self.policy.history_action_trace_include_initial)
        ):
            return "", None
        overlay_metadata: JsonObject = {"tool_index": tool_index + 1}
        artifact = self.overlay_service.render_action_overlay(
            source_image_path=pre_action_screenshot_path,
            output_path=str(self.artifact_store.overlay_path(session_step, tool_index + 1)),
            action=action,
            metadata=overlay_metadata,
        )
        if artifact is None:
            return "", None
        raw_overlay_spec = artifact.metadata.get("overlay_spec")
        overlay_spec = (
            {str(key): value for key, value in raw_overlay_spec.items()} if isinstance(raw_overlay_spec, dict) else None
        )
        return artifact.output_path, overlay_spec

    @staticmethod
    def _tool_content(execution: ToolExecutionRecord) -> str:
        """Return prompt-facing textual content for one tool execution.

        Args:
            execution: Tool execution record with LLM and raw tool outputs.

        Returns:
            Text content shown in the tool-result block.
        """

        llm_output = ToolResultService._normalized_output(execution.llm_tool_output)
        error = llm_output.get("error")
        if error is not None:
            return f"{execution.tool_name} failed: {error}"
        model_space_echo = ToolResultService._model_space_action_echo(execution)
        if model_space_echo:
            return model_space_echo
        text = llm_output.get("text")
        if text is not None:
            return str(text)

        tool_output = ToolResultService._normalized_output(execution.tool_output)
        if isinstance(tool_output, dict):
            error = tool_output.get("error")
            if error is not None:
                return f"{execution.tool_name} failed: {error}"
            text = tool_output.get("text")
            if text is not None:
                return str(text)
            if tool_output.get("screenshot_url") or tool_output.get("screenshot_path"):
                return "Screenshot captured."
        return "" if tool_output is None else str(tool_output)

    @staticmethod
    def _model_space_action_echo(execution: ToolExecutionRecord) -> str:
        """Echo mapped spatial actions in the coordinate space seen by the model."""

        if execution.tool_name not in {"click", "double_click", "drag", "move"}:
            return ""
        if not execution.tool_input_model or not execution.tool_input_pixels:
            return ""
        if execution.tool_input_model == execution.tool_input_pixels:
            return ""
        payload = json.dumps(
            execution.tool_input_model,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{execution.tool_name} succeeded with model-space input {payload}."

    @staticmethod
    def _normalized_output(tool_output: object) -> JsonObject:
        """Normalize nested MCP/result payloads into a flat dict.

        NorthCUA traces may carry action outputs as:
        - {"text": "...", "screenshot_path": "..."}
        - {"result": {"text": "...", "screenshot_path": "..."}}
        - {"result": "{\"text\":\"...\",\"screenshot_path\":\"...\"}"}
        """

        if isinstance(tool_output, dict):
            if set(tool_output.keys()) == {"result"}:
                return ToolResultService._normalized_output(tool_output.get("result"))
            return tool_output
        if isinstance(tool_output, str):
            stripped = tool_output.strip()
            if stripped.startswith("data:image/"):
                return {"screenshot_url": stripped}
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    return {"text": tool_output}
                if isinstance(parsed, dict):
                    return ToolResultService._normalized_output(parsed)
            return {"text": tool_output}
        return {}

    def _persist_raw_image(
        self,
        *,
        screenshot_path: str,
        screenshot_url: str,
        session_step: int,
        tool_index: int,
    ) -> str:
        """Persist the clean post-tool screenshot for one tool result.

        Args:
            screenshot_path: Existing local screenshot path from execution.
            screenshot_url: Data URL screenshot from execution.
            session_step: Current session step number.
            tool_index: Zero-based tool index within the step.

        Returns:
            Local artifact path for the persisted raw screenshot, or an empty
            string when no screenshot was available.
        """

        destination = self.artifact_store.tool_screenshot_path(session_step, tool_index)
        if screenshot_path and os.path.exists(screenshot_path):
            if os.path.abspath(screenshot_path) != os.path.abspath(str(destination)):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(screenshot_path, destination)
            return str(destination)
        if screenshot_url:
            destination.parent.mkdir(parents=True, exist_ok=True)
            persist_data_url_image(screenshot_url, destination)
            return str(destination)
        return ""

    @staticmethod
    def _encode_path(path: str) -> str:
        """Encode an existing image path as a prompt-ready data URL.

        Args:
            path: Local image path.

        Returns:
            PNG data URL when the path exists, otherwise an empty string.
        """

        return image_data_url_from_path(path)
