"""Scroll and badge drawing helpers for history overlays."""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PIL import ImageDraw, ImageFont

from .badge_drawing import HistoryActionOverlayBadgeDrawingMixin
from .models import HistoryActionOverlaySpec, OverlayPoint
from .style import HistoryActionOverlayStyle


class HistoryActionOverlayScrollDrawingHelper(HistoryActionOverlayBadgeDrawingMixin):
    """Drawing helpers for scroll and badge overlays."""

    style: HistoryActionOverlayStyle
    _font: ImageFont.ImageFont | ImageFont.FreeTypeFont

    def _draw_scroll_point_overlay(
        self,
        draw: ImageDraw.ImageDraw,
        image_size: Tuple[int, int],
        spec: HistoryActionOverlaySpec,
    ) -> None:
        if spec.primary_point is None:
            return

        wheel_box = self._scroll_wheel_bounds(spec.primary_point)
        self._draw_glass_chip(
            draw,
            box=wheel_box,
            radius=(wheel_box[2] - wheel_box[0]) // 2,
        )
        self._draw_scroll_wheel(draw, wheel_box=wheel_box)
        self._draw_big_scroll_arrow(
            draw,
            image_size=image_size,
            wheel_box=wheel_box,
            delta_x=spec.delta_x,
            delta_y=spec.delta_y,
        )

    def _draw_scroll_badge(
        self,
        draw: ImageDraw.ImageDraw,
        image_size: Optional[Tuple[int, int]],
        text: str,
    ) -> None:
        if not text:
            return

        margin = self.style.badge_margin
        padding_x = self.style.badge_padding_x
        padding_y = self.style.badge_padding_y
        size = self.style.scroll_icon_size
        text_bbox = draw.textbbox((0, 0), text, font=self._font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        badge_width = padding_x * 2 + size + self.style.scroll_glyph_gap + text_width
        badge_height = max(size + padding_y * 2, text_height + padding_y * 2)

        del image_size
        box = (margin, margin, margin + badge_width, margin + badge_height)
        shadow_box = (
            box[0] + self.style.badge_shadow_offset,
            box[1] + self.style.badge_shadow_offset,
            box[2] + self.style.badge_shadow_offset,
            box[3] + self.style.badge_shadow_offset,
        )

        draw.rounded_rectangle(shadow_box, radius=self.style.badge_corner_radius, fill=self.style.badge_shadow_color)
        draw.rounded_rectangle(
            box,
            radius=self.style.badge_corner_radius,
            fill=self.style.badge_background,
            outline=self.style.glass_outline_color,
            width=2,
        )
        draw.rounded_rectangle(
            (box[0] + 2, box[1] + 2, box[2] - 2, box[1] + 10),
            radius=max(2, self.style.badge_corner_radius - 6),
            fill=self.style.badge_highlight_color,
        )

        icon_left = box[0] + padding_x
        icon_top = int(box[1] + (badge_height - size) // 2)
        self._draw_scroll_wheel(
            draw,
            wheel_box=(icon_left, icon_top, icon_left + size, icon_top + size),
        )
        draw.text(
            (
                icon_left + size + self.style.scroll_glyph_gap,
                box[1] + (badge_height - text_height) // 2 - text_bbox[1],
            ),
            text,
            fill=self.style.badge_text_color,
            font=self._font,
        )

    def _draw_scroll_wheel(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        wheel_box: Tuple[int, int, int, int],
    ) -> None:
        inner_box = (
            wheel_box[0] + 3,
            wheel_box[1] + 3,
            wheel_box[2] - 3,
            wheel_box[3] - 3,
        )
        draw.ellipse(
            inner_box,
            fill=self._with_alpha(self.style.accent_tertiary_color, 56),
            outline=self.style.outline_color,
            width=2,
        )
        center_x = (wheel_box[0] + wheel_box[2]) // 2
        center_y = (wheel_box[1] + wheel_box[3]) // 2
        draw.line([(center_x, wheel_box[1] + 4), (center_x, wheel_box[3] - 4)], fill=self.style.neutral_color, width=2)
        draw.line([(wheel_box[0] + 4, center_y), (wheel_box[2] - 4, center_y)], fill=self.style.neutral_color, width=2)

    def _draw_big_scroll_arrow(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        image_size: Tuple[int, int],
        wheel_box: Tuple[int, int, int, int],
        delta_x: int,
        delta_y: int,
    ) -> None:
        directions = self._scroll_directions(delta_x, delta_y)
        if not directions:
            return

        direction = directions[0]
        color = self._scroll_primary_color(delta_x, delta_y)
        center_x = (wheel_box[0] + wheel_box[2]) // 2
        center_y = (wheel_box[1] + wheel_box[3]) // 2
        arrow_length = max(self.style.scroll_icon_size * 2 + 8, 52)
        gap = 18

        if direction == "up":
            start = (center_x, min(image_size[1] - 24, wheel_box[1] - gap + arrow_length // 2))
            end = (center_x, max(24, wheel_box[1] - gap - arrow_length // 2))
            pill_box = (center_x - 16, end[1] - 14, center_x + 16, start[1] + 14)
        elif direction == "down":
            start = (center_x, max(24, wheel_box[3] + gap - arrow_length // 2))
            end = (center_x, min(image_size[1] - 24, wheel_box[3] + gap + arrow_length // 2))
            pill_box = (center_x - 16, start[1] - 14, center_x + 16, end[1] + 14)
        elif direction == "left":
            start = (min(image_size[0] - 24, wheel_box[0] - gap + arrow_length // 2), center_y)
            end = (max(24, wheel_box[0] - gap - arrow_length // 2), center_y)
            pill_box = (end[0] - 14, center_y - 16, start[0] + 14, center_y + 16)
        else:
            start = (max(24, wheel_box[2] + gap - arrow_length // 2), center_y)
            end = (min(image_size[0] - 24, wheel_box[2] + gap + arrow_length // 2), center_y)
            pill_box = (start[0] - 14, center_y - 16, end[0] + 14, center_y + 16)

        self._draw_glass_chip(draw, box=pill_box, radius=18)
        draw.line([start, end], fill=self.style.outline_color, width=self.style.scroll_chip_arrow_stroke + 4)
        draw.line([start, end], fill=color, width=self.style.scroll_chip_arrow_stroke + 1)
        self._draw_arrow_head_from_points(draw, start=start, end=end, color=color)

    def _scroll_wheel_bounds(self, point: OverlayPoint) -> Tuple[int, int, int, int]:
        size = self.style.scroll_icon_size + 8
        half = size // 2
        return (point.x - half, point.y - half, point.x + half, point.y + half)

    def _draw_arrow_head_from_points(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        start: Tuple[int, int],
        end: Tuple[int, int],
        color: Tuple[int, int, int, int],
    ) -> None:
        start_point = OverlayPoint(*start)
        end_point = OverlayPoint(*end)
        angle = math.atan2(end_point.y - start_point.y, end_point.x - start_point.x)
        left_angle = angle + math.pi - self.style.arrow_head_angle
        right_angle = angle + math.pi + self.style.arrow_head_angle
        head_length = self.style.arrow_head_length
        left_point = (
            end_point.x + head_length * math.cos(left_angle),
            end_point.y + head_length * math.sin(left_angle),
        )
        right_point = (
            end_point.x + head_length * math.cos(right_angle),
            end_point.y + head_length * math.sin(right_angle),
        )
        draw.polygon(
            [(end_point.x, end_point.y), left_point, right_point],
            fill=color,
            outline=self.style.outline_color,
        )

    def _scroll_primary_color(
        self,
        delta_x: int,
        delta_y: int,
    ) -> Tuple[int, int, int, int]:
        if delta_y > 0 or delta_x > 0:
            return self.style.accent_color
        if delta_y < 0 or delta_x < 0:
            return self.style.accent_secondary_color
        return self.style.neutral_color

    @staticmethod
    def _scroll_directions(delta_x: int, delta_y: int) -> List[str]:
        directions: List[str] = []
        if delta_y > 0:
            directions.append("up")
        elif delta_y < 0:
            directions.append("down")
        if delta_x > 0:
            directions.append("right")
        elif delta_x < 0:
            directions.append("left")
        return directions
