"""Click and drag drawing helpers for history overlays."""

from __future__ import annotations

import math
from typing import List, Tuple

from PIL import ImageDraw

from .models import OverlayPoint


class HistoryActionOverlayClickDrawingHelper:
    """Drawing helpers for click, cursor, and drag overlays."""

    style: object

    def _draw_click_emphasis(
        self,
        draw: ImageDraw.ImageDraw,
        point: OverlayPoint,
        *,
        accent: str = "primary",
        double: bool = False,
    ) -> None:
        primary_color = self.style.accent_secondary_color if accent == "secondary" else self.style.accent_color
        halo_radius = self.style.click_ring_radius + 6
        draw.ellipse(
            [
                point.x - halo_radius,
                point.y - halo_radius,
                point.x + halo_radius,
                point.y + halo_radius,
            ],
            fill=self._with_alpha(primary_color, self.style.click_halo_opacity),
        )
        ring_radii = [self.style.click_ring_radius]
        if double:
            ring_radii.append(self.style.click_ring_radius + self.style.click_ring_spacing)

        for radius in ring_radii:
            bounds = [
                point.x - radius,
                point.y - radius,
                point.x + radius,
                point.y + radius,
            ]
            draw.ellipse(
                bounds,
                outline=self.style.outline_color,
                width=self.style.click_ring_width + 2,
            )
            draw.ellipse(
                bounds,
                outline=primary_color,
                width=self.style.click_ring_width,
            )
            self._draw_click_ticks(draw, point=point, radius=radius, color=primary_color)

        center_radius = self.style.click_center_radius
        draw.ellipse(
            [
                point.x - center_radius,
                point.y - center_radius,
                point.x + center_radius,
                point.y + center_radius,
            ],
            fill=primary_color,
            outline=self.style.outline_color,
            width=2,
        )

    def _draw_cursor(
        self,
        draw: ImageDraw.ImageDraw,
        point: OverlayPoint,
        *,
        opacity: int = 255,
    ) -> None:
        cursor_points = self._cursor_polygon(point)
        shadow_points = [
            (
                x + self.style.cursor_shadow_offset,
                y + self.style.cursor_shadow_offset,
            )
            for x, y in cursor_points
        ]
        shadow_color = self._with_alpha(self.style.cursor_shadow_color, opacity)
        fill_color = self._with_alpha(self.style.cursor_fill_color, opacity)
        inner_fill_color = self._with_alpha(self.style.cursor_inner_fill_color, opacity)
        outline_color = self._with_alpha(self.style.cursor_outline_color, opacity)
        inset_polygon = self._scale_polygon(
            cursor_points,
            anchor=(point.x, point.y),
            scale=0.72,
        )

        draw.polygon(shadow_points, fill=shadow_color)
        draw.polygon(cursor_points, fill=fill_color, outline=outline_color)
        draw.polygon(inset_polygon, fill=inner_fill_color)
        draw.line(
            [cursor_points[1], cursor_points[4]],
            fill=self._with_alpha(self.style.accent_quaternary_color, opacity),
            width=3,
        )
        draw.ellipse(
            [point.x - 3, point.y - 3, point.x + 3, point.y + 3],
            fill=self._with_alpha(self.style.accent_secondary_color, opacity),
            outline=self._with_alpha(self.style.outline_color, opacity),
        )

    def _cursor_polygon(self, point: OverlayPoint) -> List[Tuple[int, int]]:
        scale = self.style.cursor_scale
        return [
            (point.x, point.y),
            (point.x + int(scale * 0.16), point.y + int(scale * 0.72)),
            (point.x + int(scale * 0.30), point.y + int(scale * 0.56)),
            (point.x + int(scale * 0.56), point.y + int(scale * 1.02)),
            (point.x + int(scale * 0.74), point.y + int(scale * 0.92)),
            (point.x + int(scale * 0.47), point.y + int(scale * 0.48)),
            (point.x + int(scale * 0.88), point.y + int(scale * 0.46)),
        ]

    @staticmethod
    def _scale_polygon(
        polygon: List[Tuple[int, int]],
        *,
        anchor: Tuple[int, int],
        scale: float,
    ) -> List[Tuple[int, int]]:
        anchor_x, anchor_y = anchor
        return [
            (
                int(round(anchor_x + (x - anchor_x) * scale)),
                int(round(anchor_y + (y - anchor_y) * scale)),
            )
            for x, y in polygon
        ]

    def _draw_arrow_head(
        self,
        draw: ImageDraw.ImageDraw,
        start: OverlayPoint,
        end: OverlayPoint,
    ) -> None:
        angle = math.atan2(end.y - start.y, end.x - start.x)
        left_angle = angle + math.pi - self.style.arrow_head_angle
        right_angle = angle + math.pi + self.style.arrow_head_angle
        head_length = self.style.arrow_head_length

        left_point = (
            end.x + head_length * math.cos(left_angle),
            end.y + head_length * math.sin(left_angle),
        )
        right_point = (
            end.x + head_length * math.cos(right_angle),
            end.y + head_length * math.sin(right_angle),
        )

        draw.polygon(
            [(end.x, end.y), left_point, right_point],
            fill=self.style.accent_color,
            outline=self.style.outline_color,
        )

    def _draw_click_ticks(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        point: OverlayPoint,
        radius: int,
        color: Tuple[int, int, int, int],
    ) -> None:
        tick_inner = radius + 3
        tick_outer = tick_inner + self.style.click_tick_length
        segments = [
            ((point.x, point.y - tick_inner), (point.x, point.y - tick_outer)),
            ((point.x + tick_inner, point.y), (point.x + tick_outer, point.y)),
            ((point.x, point.y + tick_inner), (point.x, point.y + tick_outer)),
            ((point.x - tick_inner, point.y), (point.x - tick_outer, point.y)),
        ]
        for start, end in segments:
            draw.line([start, end], fill=self.style.outline_color, width=self.style.click_ring_width + 1)
            draw.line([start, end], fill=color, width=self.style.click_ring_width)

    def _draw_solid_drag_polyline(
        self,
        draw: ImageDraw.ImageDraw,
        path: List[Tuple[int, int]],
    ) -> None:
        if len(path) < 2:
            return

        draw.line(
            path,
            fill=self.style.outline_color,
            width=self.style.drag_line_width + 3,
            joint="curve",
        )
        draw.line(
            path,
            fill=self._with_alpha(
                self.style.accent_secondary_color,
                self.style.drag_path_alpha,
            ),
            width=self.style.drag_line_width,
            joint="curve",
        )

    @staticmethod
    def _with_alpha(
        color: Tuple[int, int, int, int],
        opacity: int,
    ) -> Tuple[int, int, int, int]:
        return (color[0], color[1], color[2], int(color[3] * opacity / 255))
