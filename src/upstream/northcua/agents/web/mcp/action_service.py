"""Shared runtime service for web-agent MCP action tools."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from time import time_ns

from PIL import Image
from pydantic import ValidationError

from northcua.agentkernel.overlay.set_of_mark import SetOfMarkBox, SetOfMarkOverlayMark
from northcua.app.logging import log_runtime_event
from northcua.core.image_payloads import image_data_url_from_path
from northcua.core.json_types import JsonObject
from northcua.runtime.artifacts.screenshot_overlay import ScreenshotOverlayService
from northcua.runtime.mcp.invocation_context import current_mcp_invocation_context

from ..backend import WebActionBackendError, WebActionCommandResult
from ..domain.actions import WebAgentActionType, WebWaitParams, action_tool_name_for_action
from ..domain.evaluation_artifacts import WebToolArtifactAttributionSource, WebToolArtifactImageKind
from ..domain.observations import (
    WebBoundingBox,
    WebObservationContext,
    WebScreenshotAnnotation,
    WebScreenshotMark,
    WebScreenshotObservation,
    WebSnapshotObservation,
)
from ..observation.collector import WebObservationCollector
from ..observation.projector import WebObservationProjector
from ..observation.ref_alias import WebRefAliasService
from ..observation.settle import WebPostActionSettleService, WebSettleResult
from ..observation.som import InteractionFirstSomSelector, WebSomSelectionConfig
from ..overlay import WebActionOverlayAdapter
from ..security import WebNavigationPolicy
from ..session.models import RegisteredWebAgentSession
from ..session.registry import WebAgentSessionRegistry
from .error_classification import build_web_tool_error_payload

logger = logging.getLogger(__name__)
ToolCall = Callable[[str, RegisteredWebAgentSession], Awaitable[dict[str, object]]]


class WebAgentMCPActionService:
    """Handle session lookup, error conversion, waits, and shared payload shape."""

    _PNG_DATA_URL_PREFIX = "data:image/png;base64,"

    def __init__(self, registry: WebAgentSessionRegistry | None = None) -> None:
        """Initialize the action service around a session registry.

        Args:
            registry: Optional registry used by tests or alternate runtimes.
        """

        self.registry = registry or WebAgentSessionRegistry()
        self.settle_service = WebPostActionSettleService()
        self.observation_projector = WebObservationProjector()
        self.ref_alias_service = WebRefAliasService()
        self.action_overlay_adapter = WebActionOverlayAdapter()
        self.screenshot_overlay_service = ScreenshotOverlayService()
        self.observation_collector = WebObservationCollector(
            settle_service=self.settle_service,
            persist_screenshot=self.persist_screenshot_if_needed,
            annotate_screenshot=self.annotate_screenshot_with_marks,
        )

    async def run_with_session(self, action: WebAgentActionType, call: ToolCall) -> dict[str, object]:
        """Execute one tool handler under the current MCP invocation session.

        Args:
            action: Web action type used for stable logging and error payloads.
            call: Coroutine that receives the session id and registered session.

        Returns:
            Stable ok/error payload for the MCP action tool.
        """

        session_id = current_mcp_invocation_context().session_id
        if not session_id:
            return self.error_payload(
                action,
                RuntimeError(f"{action_tool_name_for_action(action)} requires a session_id invocation context."),
            )
        try:
            registered = self.registry.get_registered_session(session_id)
            previous_artifact_context = registered.begin_tool_artifact_invocation(action.value)
            try:
                return await call(session_id, registered)
            finally:
                registered.restore_tool_artifact_invocation(previous_artifact_context)
        except (WebActionBackendError, LookupError, ValidationError, ValueError, binascii.Error) as exc:
            log_runtime_event(
                logger,
                "web_agent_tool_error",
                level=logging.WARNING,
                session_id=session_id,
                action=action.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self.error_payload(action, exc)

    def ok_payload(
        self,
        *,
        action: WebAgentActionType,
        text: str,
        result: object,
        params: JsonObject | None = None,
    ) -> dict[str, object]:
        """Build the stable successful tool result payload.

        Args:
            action: Web action that produced the result.
            text: Short model-facing summary of the result.
            result: Typed observation, raw primitive, or JSON-compatible payload.
            params: Optional action parameters to echo for traceability.

        Returns:
            JSON-compatible success payload shared by all web action tools.
        """

        if isinstance(result, (WebSnapshotObservation, WebScreenshotObservation)):
            payload: object = result.model_dump(mode="json")
        else:
            payload = result
        return {
            "status": "ok",
            "text": text,
            "action": action.value,
            "params": dict(params or {}),
            "result": payload,
        }

    def command_result_data(self, result: WebActionCommandResult) -> object | None:
        """Return command data after enforcing success semantics.

        Args:
            result: Browser substrate command result from local, remote, or
                device-owned web action backends.

        Returns:
            Command payload data when the substrate reported success.

        Raises:
            WebActionCommandError: If the command result represents a failed
                browser action.
        """

        return result.require_success().data

    def error_payload(self, action: WebAgentActionType, exc: Exception) -> dict[str, object]:
        """Build the stable failed tool result payload.

        Args:
            action: Web action that failed.
            exc: Exception that should be surfaced to model and trace logs.

        Returns:
            JSON-compatible error payload shared by all web action tools.
        """

        return build_web_tool_error_payload(action, exc)

    def record_preflight_tool_invocation(self, action: WebAgentActionType) -> None:
        """Record a tool call that failed before ``run_with_session``.

        Contract/schema errors still appear as tool executions in the trace,
        but they never materialize images. Recording a placeholder keeps the
        step-local invocation ledger aligned with resolved tool-call order, so
        later same-action screenshots cannot be attributed to the failed call.
        """

        session_id = current_mcp_invocation_context().session_id
        if not session_id:
            return
        try:
            registered = self.registry.get_registered_session(session_id)
        except LookupError:
            return
        previous = registered.begin_tool_artifact_invocation(action.value)
        registered.restore_tool_artifact_invocation(previous)

    async def post_action_wait(self, registered: RegisteredWebAgentSession) -> None:
        """Run the configured post-action wait for navigation/mutation tools.

        Args:
            registered: Active web session containing settings and client.
        """

        wait_ms = max(0, int(registered.settings.post_action_wait_ms))
        if wait_ms <= 0:
            return
        self.command_result_data(await registered.client.wait(WebWaitParams(duration_ms=wait_ms)))

    async def capture_pre_action_observation(
        self,
        registered: RegisteredWebAgentSession,
        *,
        action: WebAgentActionType,
        params: Mapping[str, object] | None = None,
    ) -> str:
        """Render a best-effort pre-action overlay from the actionable observation.

        The overlay must use the exact observation whose refs the model acted
        on. Capturing another DOM snapshot here would rewrite Playwright's
        ephemeral source refs before the action executes and could redirect a
        valid numeric alias to a different element. The cached observation is
        therefore authoritative; if it is unavailable, overlay generation is
        skipped without changing action success semantics.

        Returns:
            Path to the rendered action-overlay image, when available. Local
            runs also record this through session artifact events; remote runs
            attach the image data URL to the tool result so the controller can
            materialize the same visual role.
        """

        if not registered.settings.persist_pre_action_observation:
            return ""
        observation = registered.current_actionable_observation
        if observation is None:
            return ""
        if observation.observation_id != registered.current_ref_aliases.observation_id:
            log_runtime_event(
                logger,
                "web_agent_pre_action_observation_skipped",
                level=logging.DEBUG,
                session_id=registered.session_id,
                action=action.value,
                reason="observation_alias_mismatch",
                observation_id=observation.observation_id,
                alias_observation_id=registered.current_ref_aliases.observation_id,
            )
            return ""
        return self.render_pre_action_overlay(
            registered=registered,
            action=action,
            params=params,
            observation=observation,
        )

    def render_pre_action_overlay(
        self,
        *,
        registered: RegisteredWebAgentSession,
        action: WebAgentActionType,
        params: Mapping[str, object] | None,
        observation: WebObservationContext,
    ) -> str:
        """Render a Desktop-style action overlay for a pre-action Web frame."""

        plan = self.action_overlay_adapter.build_plan(
            action=action,
            params=params,
            observation=observation,
        )
        if plan is None:
            return ""
        output_path = registered.artifact_store.screenshots_dir / f"tool_{time_ns()}_action_overlay.png"
        try:
            artifact = self.screenshot_overlay_service.render_action_overlay(
                source_image_path=plan.source_image_path,
                output_path=str(output_path),
                action=plan.command,
                metadata=plan.metadata,
            )
        except Exception as exc:
            log_runtime_event(
                logger,
                "web_agent_action_overlay_render_failed",
                level=logging.DEBUG,
                session_id=registered.session_id,
                action=action.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ""
        if artifact is None:
            return ""
        registered.record_tool_image_artifact(
            raw_image_path=plan.source_image_path,
            action_overlay_image_path=artifact.output_path,
            selected_image_path=artifact.output_path,
            selected_image_kind=WebToolArtifactImageKind.ACTION_OVERLAY,
            source=WebToolArtifactAttributionSource.EXPLICIT_PATH,
            metadata={
                "stage": "pre_action_overlay",
                "overlay_spec": artifact.metadata.get("overlay_spec", {}),
                **plan.metadata,
            },
        )
        registered.artifact_store.write_json(
            Path(artifact.output_path).with_suffix(".overlay.json"),
            {
                "kind": artifact.kind.value,
                "source_image_path": plan.source_image_path,
                "output_path": artifact.output_path,
                "metadata": artifact.metadata,
            },
        )
        log_runtime_event(
            logger,
            "web_agent_action_overlay_rendered",
            level=logging.DEBUG,
            session_id=registered.session_id,
            action=action.value,
            has_source_image=True,
            has_action_overlay_image=True,
            overlay_kind=artifact.kind.value,
        )
        return artifact.output_path

    async def collect_current_observation(
        self,
        registered: RegisteredWebAgentSession,
        *,
        mode: str,
        observation_id: str | None = None,
        settle_result: WebSettleResult | None = None,
        validate_url: bool = True,
        publish_ref_aliases: bool = True,
    ) -> WebObservationContext:
        """Collect and validate a fresh current-page observation.

        Recovery callers can defer alias publication until the projected
        observation has been attached to the model-visible tool result.
        """

        observation = await self.observation_collector.collect(
            registered=registered,
            mode=mode,
            observation_id=observation_id,
            settle_result=settle_result,
        )
        if validate_url:
            self.validate_observed_url(registered, observation.page_url)
        if publish_ref_aliases:
            self.update_ref_aliases_from_observation(registered, observation)
        return observation

    def update_ref_aliases_from_observation(
        self,
        registered: RegisteredWebAgentSession,
        observation: WebObservationContext,
    ) -> None:
        """Make one observation's numeric refs the active action aliases."""

        annotation = observation.screenshot.annotation if observation.screenshot else None
        registered.update_ref_aliases(
            self.ref_alias_service.aliases_for_observation(
                observation.snapshot,
                annotation=annotation,
                observation_id=observation.observation_id,
            ),
            observation=observation,
        )

    def _snapshot_observation_context(
        self,
        snapshot: WebSnapshotObservation,
        *,
        observation_id: str,
    ) -> WebObservationContext:
        """Wrap a raw snapshot in an observation context for projection."""

        return WebObservationContext(snapshot=snapshot, observation_id=observation_id)

    def resolve_model_ref(self, registered: RegisteredWebAgentSession, ref: object) -> str:
        """Resolve a model-visible numeric ref into the backend source ref."""

        return registered.resolve_model_ref(ref)

    def action_overlay_params(
        self,
        registered: RegisteredWebAgentSession,
        *,
        action: WebAgentActionType,
        params: Mapping[str, object],
    ) -> JsonObject:
        """Return action params enriched with source refs for overlay rendering.

        The browser action executes against the currently active alias map. A
        best-effort pre-action observation must not replace that active map, so
        overlays carry the resolved source refs alongside model-visible refs and
        match marks by source ref when possible.
        """

        overlay_params: JsonObject = {str(key): value for key, value in params.items()}
        if (
            action
            in {
                WebAgentActionType.CLICK_REF,
                WebAgentActionType.HOVER_REF,
                WebAgentActionType.FILL_REF,
                WebAgentActionType.SELECT_OPTION_REF,
                WebAgentActionType.CHECK_REF,
                WebAgentActionType.UNCHECK_REF,
            }
            and "ref" in overlay_params
        ):
            overlay_params["source_ref"] = self.resolve_model_ref(registered, overlay_params.get("ref"))
        if action == WebAgentActionType.DRAG_REF:
            if "from_ref" in overlay_params:
                overlay_params["source_from_ref"] = self.resolve_model_ref(registered, overlay_params.get("from_ref"))
            if "to_ref" in overlay_params:
                overlay_params["source_to_ref"] = self.resolve_model_ref(registered, overlay_params.get("to_ref"))
        return overlay_params

    def project_command_payload_refs(self, registered: RegisteredWebAgentSession, payload: object) -> object:
        """Replace source refs in a backend command payload with model refs."""

        if isinstance(payload, str):
            return registered.project_source_ref(payload)
        if isinstance(payload, list):
            return [self.project_command_payload_refs(registered, item) for item in payload]
        if isinstance(payload, dict):
            return {str(key): self.project_command_payload_refs(registered, value) for key, value in payload.items()}
        return payload

    async def post_action_observation_payload(
        self,
        registered: RegisteredWebAgentSession,
        *,
        action: WebAgentActionType,
    ) -> tuple[JsonObject | None, JsonObject]:
        """Settle and collect the model-visible post-action observation bundle."""

        mode = registered.settings.post_action_observation_mode
        settle_result = await self.settle_service.settle_after_action(registered=registered, action=action)
        await self.validate_current_url(registered)
        if mode == "none":
            registered.clear_ref_aliases(observation_id=f"post-{action.value}-none")
            return None, ""
        observation = await self.collect_current_observation(
            registered,
            mode=mode,
            observation_id=f"post-{action.value}-{time_ns()}",
            settle_result=settle_result,
        )
        return self.project_observation_payload(observation)

    async def best_effort_post_action_observation_payload(
        self,
        registered: RegisteredWebAgentSession,
        *,
        action: WebAgentActionType,
    ) -> tuple[JsonObject | None, JsonObject, JsonObject | None]:
        """Return post-action observation data without converting action success into failure.

        URL/security validation remains strict; failures there still propagate
        because the browser may have left the allowed policy boundary. Snapshot,
        screenshot, bbox, or overlay failures are reported as retryable
        observation errors attached to the successful action payload.
        """

        mode = registered.settings.post_action_observation_mode
        timeout_seconds = registered.settings.post_action_observation_timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                settle_result = await self.settle_service.settle_after_action(registered=registered, action=action)
                if mode == "none":
                    await self.validate_current_url(registered)
                    registered.clear_ref_aliases(observation_id=f"post-{action.value}-none")
                    return None, {}, None
                observation = await self.observation_collector.collect(
                    registered=registered,
                    mode=mode,
                    observation_id=f"post-{action.value}-{time_ns()}",
                    settle_result=settle_result,
                )
        except TimeoutError:
            await self.validate_current_url(registered)
            registered.clear_ref_aliases(observation_id=f"post-{action.value}-failed")
            timeout_error = TimeoutError(f"post-action observation exceeded {timeout_seconds:g} seconds")
            return None, {}, self.observation_error_payload(timeout_error)
        except Exception as exc:
            await self.validate_current_url(registered)
            registered.clear_ref_aliases(observation_id=f"post-{action.value}-failed")
            return None, {}, self.observation_error_payload(exc)
        self.validate_observed_url(registered, observation.page_url)
        self.update_ref_aliases_from_observation(registered, observation)
        bundle, image_urls = self.project_observation_payload(observation)
        return bundle, image_urls, None

    def project_observation_payload(
        self,
        observation: WebObservationContext,
    ) -> tuple[JsonObject, JsonObject]:
        """Project one collected observation for a real model-visible tool result.

        The same projection is used by ordinary post-action collection and by
        runner recovery, keeping local and remote result envelopes identical.
        """

        return (
            self.observation_projector.bundle_dict(observation),
            self._post_action_image_urls(observation),
        )

    def observation_error_payload(self, exc: Exception) -> JsonObject:
        """Return a prompt-visible retryable post-observation failure payload."""

        return {
            "code": "post_action_observation_failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "retryable": True,
            "hint": "The browser action succeeded, but the fresh post-action observation failed. Call web_snapshot or web_screenshot before using old refs.",
        }

    def attach_observation_payload(
        self,
        payload: dict[str, object],
        *,
        observation: JsonObject | None,
        screenshot_url: str = "",
        image_urls: JsonObject | None = None,
        observation_error: JsonObject | None = None,
    ) -> dict[str, object]:
        """Attach optional observation and prompt image side-channel to a tool payload."""

        if observation is not None:
            payload["observation"] = observation
        merged_image_urls = dict(image_urls or {})
        if screenshot_url:
            merged_image_urls.setdefault("screenshot_url", screenshot_url)
        for key in ("screenshot_url", "raw_image_url", "action_overlay_image_url", "som_image_url"):
            value = merged_image_urls.get(key)
            if isinstance(value, str) and value:
                payload[key] = value
        if observation_error is not None:
            payload["observation_error"] = observation_error
        return payload

    def _post_action_image_urls(self, observation: WebObservationContext) -> JsonObject:
        """Return role-specific image URLs for the fresh post-action observation.

        ``screenshot_url`` is kept as the legacy selected image channel. Newer
        prompt/window code consumes explicit ``raw_image_url`` for the clean
        page state, ``som_image_url`` for the Set-of-Mark image, and
        ``action_overlay_image_url`` when the action handler captured a
        pre-action Desktop-style overlay. All values are inline data URLs so
        remote/device paths never enter model-visible payloads.
        """

        screenshot = observation.screenshot
        if screenshot is None:
            return {}
        image_urls: JsonObject = {}
        selected_url = screenshot.data_url or ""
        if selected_url:
            image_urls["screenshot_url"] = selected_url
        annotation = screenshot.annotation
        if annotation is None:
            if selected_url:
                image_urls["raw_image_url"] = selected_url
            return image_urls
        raw_url = image_data_url_from_path(annotation.source_screenshot_path)
        som_url = selected_url or image_data_url_from_path(annotation.overlay_screenshot_path)
        if raw_url:
            image_urls["raw_image_url"] = raw_url
        if som_url:
            image_urls["som_image_url"] = som_url
        return image_urls

    def screenshot_result_payload(self, screenshot: WebScreenshotObservation) -> JsonObject:
        """Return path-free explicit screenshot result metadata."""

        return self.observation_projector.screenshot_result(screenshot)

    def validate_url(self, registered: RegisteredWebAgentSession, url: str) -> str:
        """Validate a tool-supplied URL against the session policy.

        Args:
            registered: Active web session containing URL policy settings.
            url: Candidate URL provided by the model or caller.

        Returns:
            Normalized URL accepted by the web navigation policy.

        Raises:
            ValueError: If the URL violates scheme, localhost, file, or network policy.
        """

        return WebNavigationPolicy(registered.settings).validate_url(url)

    def validate_observed_url(self, registered: RegisteredWebAgentSession, url: str) -> str:
        """Validate a page URL observed after navigation or mutation.

        Args:
            registered: Active web session containing URL policy settings.
            url: Current page URL observed from the browser.

        Returns:
            Empty string for missing URLs, otherwise a policy-validated URL.
        """

        if not url:
            return ""
        return WebNavigationPolicy(registered.settings).validate_url(url)

    async def validate_current_url(self, registered: RegisteredWebAgentSession) -> str:
        """Read and validate the current page URL after a page-mutating action.

        Args:
            registered: Active web session whose browser client should be queried.

        Returns:
            Empty string or policy-validated current page URL.
        """

        result = await registered.client.get_url()
        payload = self.command_result_data(result)
        if isinstance(payload, dict):
            url = str(payload.get("url") or payload.get("result") or "")
        elif isinstance(payload, str):
            url = payload
        else:
            url = ""
        return self.validate_observed_url(registered, url)

    def persist_screenshot_if_needed(
        self,
        registered: RegisteredWebAgentSession,
        screenshot: WebScreenshotObservation,
    ) -> WebScreenshotObservation:
        """Persist inline screenshot data and enforce the configured byte cap.

        Args:
            registered: Active web session with artifact store and settings.
            screenshot: Screenshot observation returned by the active web
                action backend.

        Returns:
            Screenshot observation with ``path`` populated when data was persisted.

        Raises:
            ValueError: If inline screenshot bytes exceed the configured cap.
            binascii.Error: If a PNG data URL cannot be decoded.
        """

        if screenshot.path and Path(screenshot.path).is_file():
            return screenshot
        if not screenshot.data_url:
            return screenshot
        if not screenshot.data_url.startswith(self._PNG_DATA_URL_PREFIX):
            return screenshot
        decoded = base64.b64decode(screenshot.data_url.removeprefix(self._PNG_DATA_URL_PREFIX), validate=True)
        if len(decoded) > registered.settings.max_screenshot_bytes:
            raise ValueError(
                f"Screenshot exceeds web.max_screenshot_bytes={registered.settings.max_screenshot_bytes}: "
                f"{len(decoded)}"
            )
        path = registered.artifact_store.screenshots_dir / f"tool_{time_ns()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(decoded)
        screenshot.path = str(path)
        registered.record_tool_image_artifact(
            raw_image_path=str(path),
            selected_image_kind=WebToolArtifactImageKind.RAW,
            source=WebToolArtifactAttributionSource.EXPLICIT_PATH,
            metadata={"stage": "persist_screenshot"},
        )
        return screenshot

    async def annotate_screenshot_with_marks(
        self,
        registered: RegisteredWebAgentSession,
        screenshot: WebScreenshotObservation,
        snapshot: WebSnapshotObservation,
        *,
        viewport_payload: JsonObject | None = None,
        box_payloads: Mapping[str, object] | None = None,
        state_payloads: Mapping[str, object] | None = None,
    ) -> WebScreenshotObservation:
        """Render Set-of-Mark labels for the current viewport screenshot."""

        if not screenshot.path:
            return screenshot
        source_path = Path(screenshot.path)
        image_width, image_height = self._image_size(source_path)
        screenshot.width = screenshot.width or image_width
        screenshot.height = screenshot.height or image_height
        viewport = dict(viewport_payload) if viewport_payload is not None else await self._viewport_payload(registered)
        viewport_width = self._positive_float(viewport.get("width"), default=float(image_width))
        viewport_height = self._positive_float(viewport.get("height"), default=float(image_height))
        scale_x = image_width / viewport_width if viewport_width else 1.0
        scale_y = image_height / viewport_height if viewport_height else 1.0

        web_marks: list[WebScreenshotMark] = []
        overlay_marks: list[SetOfMarkOverlayMark] = []
        lookup_refs = [element.model_ref for element in snapshot.elements[: registered.settings.som_max_box_lookup]]
        resolved_box_payloads, resolved_state_payloads = await self._annotation_payloads_for_refs(
            registered,
            lookup_refs,
            box_payloads=box_payloads,
            state_payloads=state_payloads,
        )
        selection = InteractionFirstSomSelector(
            WebSomSelectionConfig(
                max_marks=registered.settings.som_max_marks,
                max_box_lookup=registered.settings.som_max_box_lookup,
                min_bbox_css_px=registered.settings.som_min_bbox_css_px,
            )
        ).select(
            elements=snapshot.elements,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            box_payloads=resolved_box_payloads,
            state_payloads=resolved_state_payloads,
        )
        omitted_reason_counts = {reason.value: count for reason, count in selection.omitted_reason_counts.items()}
        for mark_index, mark in enumerate(selection.marks, start=1):
            element = mark.element
            css_box = mark.bbox_css
            image_box = SetOfMarkBox(
                x=css_box.x * scale_x,
                y=css_box.y * scale_y,
                width=css_box.width * scale_x,
                height=css_box.height * scale_y,
            )
            label = str(mark_index)
            web_mark = WebScreenshotMark(
                ref=label,
                label=label,
                source_ref=element.model_ref,
                role=element.role,
                name=element.name or element.text,
                bbox_css=WebBoundingBox(
                    x=css_box.x,
                    y=css_box.y,
                    width=css_box.width,
                    height=css_box.height,
                ),
                bbox_image=WebBoundingBox(
                    x=image_box.x,
                    y=image_box.y,
                    width=image_box.width,
                    height=image_box.height,
                ),
                visible=mark.visible,
                enabled=mark.enabled,
            )
            web_marks.append(web_mark)
            overlay_marks.append(
                SetOfMarkOverlayMark(
                    ref=web_mark.ref,
                    label=web_mark.label,
                    role=web_mark.role,
                    name=web_mark.name,
                    bbox_css=css_box,
                    bbox_image=image_box,
                    visible=web_mark.visible,
                    enabled=web_mark.enabled,
                )
            )

        output_path = registered.artifact_store.screenshots_dir / f"tool_{time_ns()}_som.png"
        overlay_artifact = None
        if overlay_marks:
            overlay_artifact = ScreenshotOverlayService().render_set_of_mark_overlay(
                source_image_path=str(source_path),
                output_path=str(output_path),
                marks=overlay_marks,
                metadata={
                    "truncated": selection.truncated,
                    "omitted_count": selection.omitted_count,
                    "omitted_reason_counts": omitted_reason_counts,
                    "marked_count": selection.marked_count,
                    "visible_interactive_count": selection.visible_interactive_count,
                    "max_marks": selection.max_marks,
                    "max_box_lookup": selection.max_box_lookup,
                    "coordinate_frame": "viewport_css",
                },
            )
        annotation = WebScreenshotAnnotation(
            marks=web_marks,
            truncated=selection.truncated,
            omitted_count=selection.omitted_count,
            omitted_reason_counts=omitted_reason_counts,
            marked_count=selection.marked_count,
            visible_interactive_count=selection.visible_interactive_count,
            max_marks=selection.max_marks,
            max_box_lookup=selection.max_box_lookup,
            source_screenshot_path=str(source_path),
            overlay_screenshot_path=str(output_path if overlay_artifact is not None else ""),
        )
        registered.artifact_store.write_json(
            (output_path if overlay_artifact is not None else source_path).with_suffix(".annotation.json"),
            annotation.model_dump(mode="json"),
        )
        screenshot.annotation = annotation
        if overlay_artifact is not None:
            screenshot.path = overlay_artifact.output_path
            self._attach_data_url_if_small(registered, screenshot)
            registered.record_tool_image_artifact(
                raw_image_path=str(source_path),
                som_image_path=overlay_artifact.output_path,
                selected_image_path=overlay_artifact.output_path,
                selected_image_kind=WebToolArtifactImageKind.SOM,
                source=WebToolArtifactAttributionSource.EXPLICIT_PATH,
                metadata={"stage": "annotate_screenshot"},
            )
        raw = dict(screenshot.raw)
        raw["annotation"] = annotation.model_dump(mode="json")
        screenshot.raw = raw
        return screenshot

    async def _annotation_payloads_for_refs(
        self,
        registered: RegisteredWebAgentSession,
        refs: list[str],
        *,
        box_payloads: Mapping[str, object] | None,
        state_payloads: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], dict[str, dict[str, bool]]]:
        if box_payloads is None and state_payloads is None:
            fetched_boxes, fetched_states = await asyncio.gather(
                self._box_payloads_for_refs(registered, refs),
                self._state_payloads_for_refs(registered, refs),
            )
            return fetched_boxes, fetched_states

        resolved_box_payloads = self._normalize_box_payload_mapping(box_payloads or {})
        resolved_state_payloads = self._normalize_state_payload_mapping(state_payloads or {})
        missing_box_refs = [ref for ref in refs if ref not in resolved_box_payloads]
        missing_state_refs = [ref for ref in refs if ref not in resolved_state_payloads]

        if missing_box_refs and missing_state_refs:
            fetched_boxes, fetched_states = await asyncio.gather(
                self._box_payloads_for_refs(registered, missing_box_refs),
                self._state_payloads_for_refs(registered, missing_state_refs),
            )
            resolved_box_payloads.update(fetched_boxes)
            resolved_state_payloads.update(fetched_states)
        elif missing_box_refs:
            resolved_box_payloads.update(await self._box_payloads_for_refs(registered, missing_box_refs))
        elif missing_state_refs:
            resolved_state_payloads.update(await self._state_payloads_for_refs(registered, missing_state_refs))

        return resolved_box_payloads, resolved_state_payloads

    def _normalize_box_payload_mapping(self, payloads: Mapping[str, object]) -> dict[str, object]:
        return {str(ref): payload for ref, payload in payloads.items() if not str(ref).startswith("__")}

    def _normalize_state_payload_mapping(self, payloads: Mapping[str, object]) -> dict[str, dict[str, bool]]:
        normalized: dict[str, dict[str, bool]] = {}
        for ref, state in payloads.items():
            if str(ref).startswith("__") or not isinstance(state, Mapping):
                continue
            normalized[str(ref)] = {
                key: bool(value)
                for key, value in state.items()
                if key in {"visible", "enabled"} and isinstance(value, bool)
            }
        return normalized

    async def _box_payloads_for_refs(
        self,
        registered: RegisteredWebAgentSession,
        refs: list[str],
    ) -> dict[str, object]:
        if not refs:
            return {}
        result = await registered.client.get_boxes(refs)
        payload = self.command_result_data(result)
        if not isinstance(payload, dict):
            return {}
        boxes = payload.get("boxes")
        if isinstance(boxes, dict):
            return dict(boxes)
        return dict(payload)

    async def _state_payloads_for_refs(
        self,
        registered: RegisteredWebAgentSession,
        refs: list[str],
    ) -> dict[str, dict[str, bool]]:
        if not refs:
            return {}
        try:
            result = await registered.client.get_ref_states(refs)
            payload = self.command_result_data(result)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        states = payload.get("states")
        if not isinstance(states, dict):
            return {}
        normalized: dict[str, dict[str, bool]] = {}
        for ref, state in states.items():
            if isinstance(ref, str) and isinstance(state, dict):
                normalized[ref] = {
                    key: bool(value)
                    for key, value in state.items()
                    if key in {"visible", "enabled"} and isinstance(value, bool)
                }
        return normalized

    async def _viewport_payload(self, registered: RegisteredWebAgentSession) -> JsonObject:
        result = await registered.client.get_viewport_size()
        payload = self.command_result_data(result)
        if isinstance(payload, dict):
            nested = payload.get("result")
            return dict(nested) if isinstance(nested, dict) else dict(payload)
        return {}

    def _image_size(self, path: Path) -> tuple[int, int]:
        with Image.open(path) as image:
            return image.size

    def _attach_data_url_if_small(
        self,
        registered: RegisteredWebAgentSession,
        screenshot: WebScreenshotObservation,
    ) -> None:
        path = Path(screenshot.path)
        data = path.read_bytes()
        if len(data) > registered.settings.max_screenshot_bytes:
            screenshot.data_url = ""
            return
        screenshot.data_url = f"{self._PNG_DATA_URL_PREFIX}{base64.b64encode(data).decode('ascii')}"

    def _positive_float(self, value: object, *, default: float) -> float:
        parsed = self._optional_float(value)
        return parsed if parsed is not None and parsed > 0 else default

    def _optional_float(self, value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None
