"""Shared badge and glass-chip drawing helpers for history overlays."""

from __future__ import annotations

from typing import Optional, Tuple

from PIL import ImageDraw, ImageFont

from .style import HistoryActionOverlayStyle


class HistoryActionOverlayBadgeDrawingMixin:
    """Draw reusable glass badges used by history overlay helpers."""

    style: HistoryActionOverlayStyle
    _font: ImageFont.ImageFont | ImageFont.FreeTypeFont

    def _draw_glass_chip(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        box: Tuple[int, int, int, int],
        radius: int,
    ) -> None:
        shadow_box = (
            box[0] + self.style.badge_shadow_offset,
            box[1] + self.style.badge_shadow_offset,
            box[2] + self.style.badge_shadow_offset,
            box[3] + self.style.badge_shadow_offset,
        )
        draw.rounded_rectangle(shadow_box, radius=radius, fill=self.style.badge_shadow_color)
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=self.style.glass_fill_color,
            outline=self.style.glass_outline_color,
            width=2,
        )
        draw.rounded_rectangle(
            (box[0] + 2, box[1] + 2, box[2] - 2, box[1] + max(10, radius)),
            radius=max(4, radius - 6),
            fill=self.style.glass_highlight_color,
        )

    @staticmethod
    def _with_alpha(
        color: Tuple[int, int, int, int],
        opacity: int,
    ) -> Tuple[int, int, int, int]:
        return (color[0], color[1], color[2], int(color[3] * opacity / 255))

    def _draw_badge(
        self,
        draw: ImageDraw.ImageDraw,
        image_size: Optional[Tuple[int, int]],
        text: str,
    ) -> None:
        if not text:
            return

        del image_size
        margin = self.style.badge_margin
        padding_x = self.style.badge_padding_x
        padding_y = self.style.badge_padding_y
        text_bbox = draw.textbbox((0, 0), text, font=self._font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]

        box = (
            margin,
            margin,
            margin + text_width + padding_x * 2,
            margin + text_height + padding_y * 2,
        )
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
        draw.text(
            (box[0] + padding_x, box[1] + padding_y),
            text,
            fill=self.style.badge_text_color,
            font=self._font,
        )
