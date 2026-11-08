"""Renderer for history action overlays."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .drawing import HistoryActionOverlayDrawingHelper
from .models import HistoryActionOverlayArtifact, HistoryActionOverlaySpec
from .style import HistoryActionOverlayStyle


class HistoryActionOverlayRenderer(HistoryActionOverlayDrawingHelper):
    """Renderer that paints one overlay specification on a source screenshot."""

    def __init__(self, style: Optional[HistoryActionOverlayStyle] = None) -> None:
        """Initialize the overlay renderer.

        Args:
            style: Optional drawing style override.
        """

        self.style = style or HistoryActionOverlayStyle()
        self._font = ImageFont.load_default()

    def render(
        self,
        source_image_path: str,
        spec: HistoryActionOverlaySpec,
        output_path: str,
    ) -> HistoryActionOverlayArtifact:
        """Render one history overlay onto a screenshot.

        Args:
            source_image_path: Existing screenshot path to annotate.
            spec: Normalized overlay specification from the planner.
            output_path: PNG path where the overlay artifact should be written.

        Returns:
            Rendered overlay artifact metadata.

        Raises:
            FileNotFoundError: If the source screenshot is missing.
            ValueError: If the overlay render mode is unsupported.
        """

        source_path = Path(source_image_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Overlay source image does not exist: {source_image_path}")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        image = Image.open(source_path).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")

        if spec.render_mode == "click":
            self._draw_click(draw, spec)
        elif spec.render_mode == "drag":
            self._draw_drag(draw, spec)
        elif spec.render_mode == "scroll":
            self._draw_scroll(draw, image.size, spec)
        elif spec.render_mode == "scroll_badge":
            self._draw_scroll_badge(draw, image.size, spec.badge_text)
        elif spec.render_mode == "badge":
            self._draw_badge(draw, image.size, spec.badge_text)
        else:
            raise ValueError(f"Unsupported overlay render mode: {spec.render_mode}")

        image.save(output, format="PNG")
        return HistoryActionOverlayArtifact(output_path=str(output), spec=spec)

    def _draw_click(
        self,
        draw: ImageDraw.ImageDraw,
        spec: HistoryActionOverlaySpec,
    ) -> None:
        """Draw click emphasis and cursor for a click-like action."""

        if spec.primary_point is None:
            return

        self._draw_click_emphasis(
            draw,
            spec.primary_point,
            double=spec.variant == "double",
        )
        self._draw_cursor(draw, spec.primary_point)

    def _draw_drag(
        self,
        draw: ImageDraw.ImageDraw,
        spec: HistoryActionOverlaySpec,
    ) -> None:
        """Draw drag path, endpoints, cursor, and arrow head."""

        if len(spec.path_points) < 2:
            return

        # Path points are already normalized by the planner; do not rescale here
        # or replay overlays will drift from recorded screenshot coordinates.
        path = [(point.x, point.y) for point in spec.path_points]
        shadow_path = [
            (
                x + self.style.drag_shadow_offset,
                y + self.style.drag_shadow_offset,
            )
            for x, y in path
        ]
        draw.line(
            shadow_path,
            fill=self._with_alpha(
                self.style.cursor_shadow_color,
                self.style.drag_shadow_alpha,
            ),
            width=self.style.drag_line_width + 6,
            joint="curve",
        )
        self._draw_solid_drag_polyline(draw, path)
        self._draw_click_emphasis(
            draw,
            spec.path_points[0],
            double=False,
            accent="secondary",
        )
        self._draw_cursor(
            draw,
            spec.path_points[0],
            opacity=self.style.drag_start_opacity,
        )
        self._draw_click_emphasis(
            draw,
            spec.path_points[-1],
            double=False,
            accent="primary",
        )
        self._draw_cursor(draw, spec.path_points[-1])
        self._draw_arrow_head(draw, spec.path_points[-2], spec.path_points[-1])

    def _draw_scroll(
        self,
        draw: ImageDraw.ImageDraw,
        image_size: Tuple[int, int],
        spec: HistoryActionOverlaySpec,
    ) -> None:
        """Draw scroll overlay at a point or fall back to a badge."""

        if spec.primary_point is None:
            if spec.badge_text:
                self._draw_scroll_badge(draw, image_size, spec.badge_text)
            return

        self._draw_scroll_point_overlay(draw, image_size, spec)
