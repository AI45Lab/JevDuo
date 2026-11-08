"""Planning strategies for history action overlays."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from northcua.agentkernel.actions.protocol import ActionCommandInfo

from .models import HistoryActionOverlaySpec

if TYPE_CHECKING:
    from .planner import HistoryActionOverlayPlanner


class BaseHistoryActionOverlayStrategy(ABC):
    """Base planner strategy for one action family."""

    def __init__(self, planner: "HistoryActionOverlayPlanner") -> None:
        """Initialize a strategy with shared planner helpers.

        Args:
            planner: Overlay planner that owns normalization helpers.
        """

        self.planner = planner

    @abstractmethod
    def plan(self, action: ActionCommandInfo) -> HistoryActionOverlaySpec | None:
        """Convert one action into an overlay specification.

        Args:
            action: Captured action command to visualize.

        Returns:
            Overlay specification, or ``None`` when the action cannot render.
        """


class ClickHistoryActionOverlayStrategy(BaseHistoryActionOverlayStrategy):
    """Plan point-based click overlays."""

    def __init__(self, planner: "HistoryActionOverlayPlanner", variant: str) -> None:
        """Initialize click planning.

        Args:
            planner: Overlay planner that owns normalization helpers.
            variant: Click variant to record in the overlay spec.
        """

        super().__init__(planner)
        self.variant = variant

    def plan(self, action: ActionCommandInfo) -> HistoryActionOverlaySpec | None:
        """Plan a click overlay from ``x`` and ``y`` arguments.

        Args:
            action: Captured click-like action.

        Returns:
            Click overlay spec, or ``None`` when coordinates are missing.
        """

        point = self.planner.point_from_arguments(action.arguments, x_key="x", y_key="y")
        if point is None:
            return None
        return HistoryActionOverlaySpec(
            action_function=action.function,
            render_mode="click",
            primary_point=point,
            variant=self.variant,
            metadata={"arguments": dict(action.arguments or {})},
        )


class DragHistoryActionOverlayStrategy(BaseHistoryActionOverlayStrategy):
    """Plan drag overlays from a polyline path."""

    def plan(self, action: ActionCommandInfo) -> HistoryActionOverlaySpec | None:
        """Plan a drag overlay from a path argument.

        Args:
            action: Captured drag action.

        Returns:
            Drag overlay spec, or ``None`` when fewer than two points exist.
        """

        path_points = self.planner.path_from_arguments(action.arguments.get("path"))
        if len(path_points) < 2:
            return None
        return HistoryActionOverlaySpec(
            action_function=action.function,
            render_mode="drag",
            primary_point=path_points[0],
            secondary_point=path_points[-1],
            path_points=path_points,
            metadata={"arguments": dict(action.arguments or {})},
        )


class ScrollHistoryActionOverlayStrategy(BaseHistoryActionOverlayStrategy):
    """Plan scroll overlays as either point-based arrows or a corner badge."""

    def plan(self, action: ActionCommandInfo) -> HistoryActionOverlaySpec | None:
        """Plan a scroll overlay from delta and optional coordinate arguments.

        Args:
            action: Captured scroll action.

        Returns:
            Point-based scroll spec, badge spec, or ``None`` when empty.
        """

        arguments = action.arguments or {}
        delta_x = self.planner.integer_value(arguments.get("delta_x"), default=0)
        delta_y = self.planner.integer_value(arguments.get("delta_y"), default=0)
        point = self.planner.point_from_arguments(arguments, x_key="x", y_key="y")
        if point is not None:
            return HistoryActionOverlaySpec(
                action_function=action.function,
                render_mode="scroll",
                primary_point=point,
                delta_x=delta_x,
                delta_y=delta_y,
                metadata={"arguments": dict(arguments)},
            )
        badge_text = self.planner.formatter.scroll_badge_text(delta_x=delta_x, delta_y=delta_y)
        if not badge_text:
            return None
        return HistoryActionOverlaySpec(
            action_function=action.function,
            render_mode="scroll_badge",
            badge_text=badge_text,
            metadata={"arguments": dict(arguments)},
        )


class BadgeHistoryActionOverlayStrategy(BaseHistoryActionOverlayStrategy):
    """Plan badge-only overlays for non-spatial actions."""

    def __init__(self, planner: "HistoryActionOverlayPlanner", badge_builder) -> None:
        """Initialize badge-only planning.

        Args:
            planner: Overlay planner that owns normalization helpers.
            badge_builder: Callable that builds raw badge text for an action.
        """

        super().__init__(planner)
        self.badge_builder = badge_builder

    def plan(self, action: ActionCommandInfo) -> HistoryActionOverlaySpec | None:
        """Plan a badge-only overlay.

        Args:
            action: Captured non-spatial action.

        Returns:
            Badge overlay spec, or ``None`` when badge text is empty.
        """

        badge_text = self.planner.formatter.badge_text(self.badge_builder(action))
        if not badge_text:
            return None
        return HistoryActionOverlaySpec(
            action_function=action.function,
            render_mode="badge",
            badge_text=badge_text,
            metadata={"arguments": dict(action.arguments or {})},
        )
