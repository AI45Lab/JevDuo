"""Planner for history action overlays."""

from __future__ import annotations

from collections.abc import Sequence

from northcua.agentkernel.actions.protocol import ActionCommandInfo
from northcua.agentkernel.json_types import JsonObject, JsonValue

from .badges import OverlayBadgeTextFormatter
from .models import HistoryActionOverlaySpec, OverlayPoint
from .strategies import (
    BadgeHistoryActionOverlayStrategy,
    BaseHistoryActionOverlayStrategy,
    ClickHistoryActionOverlayStrategy,
    DragHistoryActionOverlayStrategy,
    ScrollHistoryActionOverlayStrategy,
)


class HistoryActionOverlayPlanner:
    """Planner that maps executed actions into normalized overlay specifications."""

    def __init__(self, *, badges_enabled: bool = True, max_badge_text: int = 32) -> None:
        """Initialize overlay planning strategies.

        Args:
            badges_enabled: Whether badge-only overlays may emit text.
            max_badge_text: Maximum badge text length before truncation.
        """

        self.badges_enabled = badges_enabled
        self.max_badge_text = max(max_badge_text, 4)
        self.formatter = OverlayBadgeTextFormatter(
            badges_enabled=badges_enabled,
            max_badge_text=max_badge_text,
        )
        self._strategies: dict[str, BaseHistoryActionOverlayStrategy] = {
            "click": ClickHistoryActionOverlayStrategy(self, variant="single"),
            "double_click": ClickHistoryActionOverlayStrategy(self, variant="double"),
            "drag": DragHistoryActionOverlayStrategy(self),
            "scroll": ScrollHistoryActionOverlayStrategy(self),
            "keypress": BadgeHistoryActionOverlayStrategy(self, self.formatter.keypress_badge_text),
            "type": BadgeHistoryActionOverlayStrategy(self, self.formatter.type_badge_text),
            "wait": BadgeHistoryActionOverlayStrategy(self, self.formatter.wait_badge_text),
            "move": BadgeHistoryActionOverlayStrategy(self, self.formatter.move_badge_text),
        }

    def plan(self, action: ActionCommandInfo) -> HistoryActionOverlaySpec | None:
        """Convert one action into an overlay specification.

        Args:
            action: Captured action command to visualize.

        Returns:
            Overlay specification, or ``None`` when the action is unsupported
            or lacks required coordinates.
        """

        strategy = self._strategies.get(action.function)
        if strategy is None:
            return None
        return strategy.plan(action)

    @staticmethod
    def integer_value(value: JsonValue, default: int = 0) -> int:
        """Convert one value into an integer when possible.

        Args:
            value: Raw numeric-like value.
            default: Fallback value when conversion fails.

        Returns:
            Rounded integer value or the default.
        """

        try:
            if value is None:
                return default
            return int(round(float(value)))
        except (TypeError, ValueError):
            return default

    def point_from_arguments(
        self,
        arguments: JsonObject,
        *,
        x_key: str,
        y_key: str,
    ) -> OverlayPoint | None:
        """Build one overlay point from action arguments.

        Args:
            arguments: Raw action arguments.
            x_key: Key containing the horizontal coordinate.
            y_key: Key containing the vertical coordinate.

        Returns:
            Overlay point when both coordinates are present, otherwise ``None``.
        """

        if x_key not in arguments or y_key not in arguments:
            return None
        return OverlayPoint(
            x=self.integer_value(arguments.get(x_key)),
            y=self.integer_value(arguments.get(y_key)),
        )

    def path_from_arguments(self, raw_path: JsonValue) -> list[OverlayPoint]:
        """Normalize one drag path payload into overlay points.

        Args:
            raw_path: Raw path payload from action arguments.

        Returns:
            Ordered overlay points with invalid entries skipped.
        """

        if not isinstance(raw_path, Sequence) or isinstance(raw_path, (str, bytes)):
            return []
        normalized_path: list[OverlayPoint] = []
        for point in raw_path:
            if not isinstance(point, dict):
                continue
            if "x" not in point or "y" not in point:
                continue
            normalized_path.append(
                OverlayPoint(
                    x=self.integer_value(point.get("x")),
                    y=self.integer_value(point.get("y")),
                )
            )
        return normalized_path
