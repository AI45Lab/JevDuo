"""Map Web Agent actions onto shared Desktop action-overlay commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from northcua.agentkernel.actions import ActionCommandInfo
from northcua.core.json_types import JsonObject

from ..domain.actions import WebAgentActionType, WebScrollDirection, canonical_web_ref
from ..domain.observations import WebBoundingBox, WebObservationContext, WebScreenshotMark, WebScreenshotObservation
from .domain import WebActionOverlayPlan


@dataclass(slots=True)
class WebActionOverlayAdapter:
    """Translate Web Agent actions into shared screenshot-overlay actions.

    Web tools operate in viewport CSS pixels and often target numeric refs.
    Desktop's shared overlay renderer expects screenshot-pixel action commands.
    This adapter owns that boundary: it resolves Web refs against the pre-action
    SoM marks, converts coordinates into screenshot pixels, and returns a small
    ``ActionCommandInfo`` consumed by ``ScreenshotOverlayService``.
    """

    wait_duration_ms: int = 250

    def build_plan(
        self,
        *,
        action: WebAgentActionType,
        params: Mapping[str, object] | None,
        observation: WebObservationContext,
    ) -> WebActionOverlayPlan | None:
        """Build an overlay plan for one Web tool call.

        Args:
            action: Internal Web Agent action kind.
            params: Validated model-visible tool parameters.
            observation: Best-effort pre-action observation.

        Returns:
            Render plan, or ``None`` when the action has no meaningful overlay
            or the pre-action screenshot cannot support one.
        """

        screenshot = observation.screenshot
        source_image_path = self._source_image_path(screenshot)
        if not screenshot or not source_image_path:
            return None
        payload = dict(params or {})
        command = self._command_for_action(action=action, params=payload, screenshot=screenshot)
        if command is None:
            return None
        return WebActionOverlayPlan(
            command=command,
            source_image_path=source_image_path,
            source_observation_id=observation.observation_id,
            metadata={
                "web_action": action.value,
                "source_observation_id": observation.observation_id,
            },
        )

    def _command_for_action(
        self,
        *,
        action: WebAgentActionType,
        params: JsonObject,
        screenshot: WebScreenshotObservation,
    ) -> ActionCommandInfo | None:
        if action == WebAgentActionType.FILL_REF:
            text = str(params.get("text") or "")
            return ActionCommandInfo(function="type", arguments={"text": text}) if text else None

        if action in {
            WebAgentActionType.CLICK_REF,
            WebAgentActionType.SELECT_OPTION_REF,
            WebAgentActionType.CHECK_REF,
            WebAgentActionType.UNCHECK_REF,
        }:
            point = self._point_for_ref(params.get("source_ref") or params.get("ref"), screenshot=screenshot)
            if point is None:
                return None
            return ActionCommandInfo(function="click", arguments={**point, "ref": self._ref_text(params.get("ref"))})

        if action == WebAgentActionType.HOVER_REF:
            point = self._point_for_ref(params.get("source_ref") or params.get("ref"), screenshot=screenshot)
            if point is None:
                return None
            return ActionCommandInfo(function="move", arguments={**point, "ref": self._ref_text(params.get("ref"))})

        if action == WebAgentActionType.DRAG_REF:
            start = self._point_for_ref(params.get("source_from_ref") or params.get("from_ref"), screenshot=screenshot)
            end = self._point_for_ref(params.get("source_to_ref") or params.get("to_ref"), screenshot=screenshot)
            if start is None or end is None:
                return None
            return ActionCommandInfo(
                function="drag",
                arguments={
                    "path": [start, end],
                    "from_ref": self._ref_text(params.get("from_ref")),
                    "to_ref": self._ref_text(params.get("to_ref")),
                },
            )

        if action == WebAgentActionType.CLICK_POINT:
            point = self._point_from_css(params.get("x"), params.get("y"), screenshot=screenshot)
            if point is None:
                return None
            return ActionCommandInfo(function="click", arguments=point)

        if action == WebAgentActionType.DOUBLE_CLICK_POINT:
            point = self._point_from_css(params.get("x"), params.get("y"), screenshot=screenshot)
            if point is None:
                return None
            return ActionCommandInfo(function="double_click", arguments=point)

        if action == WebAgentActionType.DRAG_POINT:
            start = self._point_from_css(params.get("from_x"), params.get("from_y"), screenshot=screenshot)
            end = self._point_from_css(params.get("to_x"), params.get("to_y"), screenshot=screenshot)
            if start is None or end is None:
                return None
            return ActionCommandInfo(function="drag", arguments={"path": [start, end]})

        if action == WebAgentActionType.SCROLL:
            return ActionCommandInfo(function="scroll", arguments=self._scroll_arguments(params))

        if action == WebAgentActionType.PRESS:
            key = str(params.get("key") or "").strip()
            return ActionCommandInfo(function="keypress", arguments={"key": key}) if key else None

        if action == WebAgentActionType.TYPE_TEXT:
            return ActionCommandInfo(function="type", arguments={"text": str(params.get("text") or "")})

        if action == WebAgentActionType.WAIT:
            return ActionCommandInfo(function="wait", arguments={"ms": self._wait_ms(params)})

        return None

    def _point_for_ref(self, value: object, *, screenshot: WebScreenshotObservation) -> JsonObject | None:
        ref = self._ref_text(value)
        if not ref:
            return None
        mark = self._marks_by_ref(screenshot).get(ref)
        if mark is None:
            return None
        return self._center_of_box(mark.bbox_image)

    def _point_from_css(
        self,
        x_value: object,
        y_value: object,
        *,
        screenshot: WebScreenshotObservation,
    ) -> JsonObject | None:
        x = self._float_value(x_value)
        y = self._float_value(y_value)
        if x is None or y is None:
            return None
        scale_x, scale_y = self._css_to_image_scale(screenshot)
        return {"x": round(x * scale_x), "y": round(y * scale_y)}

    def _css_to_image_scale(self, screenshot: WebScreenshotObservation) -> tuple[float, float]:
        annotation = screenshot.annotation
        if annotation is not None:
            for mark in annotation.marks:
                if mark.bbox_css.width > 0 and mark.bbox_css.height > 0:
                    return (
                        mark.bbox_image.width / mark.bbox_css.width,
                        mark.bbox_image.height / mark.bbox_css.height,
                    )
        viewport = screenshot.raw.get("viewport") if isinstance(screenshot.raw, dict) else None
        if isinstance(viewport, Mapping):
            viewport_width = self._float_value(viewport.get("width"))
            viewport_height = self._float_value(viewport.get("height"))
            if viewport_width and viewport_height and screenshot.width and screenshot.height:
                return screenshot.width / viewport_width, screenshot.height / viewport_height
        return 1.0, 1.0

    def _marks_by_ref(self, screenshot: WebScreenshotObservation) -> dict[str, WebScreenshotMark]:
        annotation = screenshot.annotation
        if annotation is None:
            return {}
        marks: dict[str, WebScreenshotMark] = {}
        for mark in annotation.marks:
            for key in {mark.ref, mark.label, mark.source_ref}:
                normalized = self._ref_text(key)
                if normalized:
                    marks[normalized] = mark
        return marks

    @staticmethod
    def _source_image_path(screenshot: WebScreenshotObservation | None) -> str:
        if screenshot is None:
            return ""
        annotation = screenshot.annotation
        if annotation is not None and annotation.source_screenshot_path:
            return annotation.source_screenshot_path
        return screenshot.path

    @staticmethod
    def _center_of_box(box: WebBoundingBox) -> JsonObject:
        return {
            "x": round(box.x + box.width / 2.0),
            "y": round(box.y + box.height / 2.0),
        }

    @staticmethod
    def _scroll_arguments(params: Mapping[str, object]) -> JsonObject:
        amount = WebActionOverlayAdapter._int_value(params.get("amount"), default=600)
        direction = str(params.get("direction") or WebScrollDirection.DOWN.value).strip().lower()
        delta_x = 0
        delta_y = 0
        if direction == WebScrollDirection.UP.value:
            delta_y = -amount
        elif direction == WebScrollDirection.DOWN.value:
            delta_y = amount
        elif direction == WebScrollDirection.LEFT.value:
            delta_x = -amount
        elif direction == WebScrollDirection.RIGHT.value:
            delta_x = amount
        return {"delta_x": delta_x, "delta_y": delta_y}

    def _wait_ms(self, params: Mapping[str, object]) -> int:
        duration_ms = self._int_value(params.get("duration_ms"), default=-1)
        return duration_ms if duration_ms >= 0 else self.wait_duration_ms

    @staticmethod
    def _ref_text(value: object) -> str:
        try:
            return canonical_web_ref(value)
        except ValueError:
            return ""

    @staticmethod
    def _float_value(value: object) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _int_value(value: object, *, default: int) -> int:
        parsed = WebActionOverlayAdapter._float_value(value)
        if parsed is None:
            return default
        return int(round(parsed))
