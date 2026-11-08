"""Rendering style constants for history overlays."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryActionOverlayStyle:
    """Rendering constants for replay overlays.

    Attributes:
        accent_color: Primary action accent color.
        accent_secondary_color: Secondary action accent color.
        accent_tertiary_color: Tertiary action accent color.
        accent_quaternary_color: Quaternary action accent color.
        neutral_color: Neutral drawing color.
        outline_color: High-contrast outline color.
        glass_fill_color: Fill color for glass-style badges and chips.
        glass_highlight_color: Highlight color for glass-style surfaces.
        glass_outline_color: Outline color for glass-style surfaces.
        cursor_fill_color: Cursor body fill color.
        cursor_inner_fill_color: Cursor accent fill color.
        cursor_outline_color: Cursor outline color.
        cursor_shadow_color: Cursor shadow color.
        badge_background: Badge background color.
        badge_highlight_color: Badge highlight color.
        badge_text_color: Badge text color.
        badge_shadow_color: Badge shadow color.
        line_width: Width for primary overlay lines.
        drag_line_width: Width for drag path lines.
        badge_margin: Badge margin from screenshot edges.
        badge_padding_x: Horizontal badge padding.
        badge_padding_y: Vertical badge padding.
        badge_corner_radius: Badge corner radius.
        badge_shadow_offset: Badge shadow offset in pixels.
        arrow_head_length: Arrow head length in pixels.
        arrow_head_angle: Arrow head angle in radians.
        click_ring_radius: Radius of click emphasis rings.
        click_ring_spacing: Spacing between click rings.
        click_ring_width: Width of click rings.
        click_tick_length: Tick length around click emphasis.
        click_center_radius: Radius of click center dot.
        click_halo_opacity: Opacity for click halo.
        cursor_scale: Cursor glyph scale.
        cursor_shadow_offset: Cursor shadow offset in pixels.
        scroll_pill_padding_x: Horizontal scroll badge padding.
        scroll_pill_padding_y: Vertical scroll badge padding.
        scroll_pill_radius: Scroll pill corner radius.
        scroll_icon_size: Scroll icon size.
        scroll_glyph_gap: Gap between scroll glyph elements.
        scroll_chip_radius: Scroll chip corner radius.
        scroll_chip_arrow_stroke: Width of scroll chip arrow stroke.
        drag_start_opacity: Opacity for drag start marker.
        drag_path_alpha: Alpha for drag path.
        drag_shadow_alpha: Alpha for drag shadow.
        drag_shadow_offset: Drag shadow offset in pixels.
    """

    accent_color: tuple[int, int, int, int] = (240, 68, 56, 255)
    accent_secondary_color: tuple[int, int, int, int] = (255, 183, 77, 255)
    accent_tertiary_color: tuple[int, int, int, int] = (56, 189, 248, 255)
    accent_quaternary_color: tuple[int, int, int, int] = (96, 165, 250, 255)
    neutral_color: tuple[int, int, int, int] = (31, 41, 55, 255)
    outline_color: tuple[int, int, int, int] = (255, 255, 255, 255)
    glass_fill_color: tuple[int, int, int, int] = (15, 23, 42, 210)
    glass_highlight_color: tuple[int, int, int, int] = (255, 255, 255, 52)
    glass_outline_color: tuple[int, int, int, int] = (255, 255, 255, 182)
    cursor_fill_color: tuple[int, int, int, int] = (15, 23, 42, 248)
    cursor_inner_fill_color: tuple[int, int, int, int] = (56, 189, 248, 236)
    cursor_outline_color: tuple[int, int, int, int] = (255, 255, 255, 245)
    cursor_shadow_color: tuple[int, int, int, int] = (15, 23, 42, 42)
    badge_background: tuple[int, int, int, int] = (17, 24, 39, 220)
    badge_highlight_color: tuple[int, int, int, int] = (255, 255, 255, 36)
    badge_text_color: tuple[int, int, int, int] = (255, 255, 255, 255)
    badge_shadow_color: tuple[int, int, int, int] = (0, 0, 0, 64)
    line_width: int = 6
    drag_line_width: int = 5
    badge_margin: int = 18
    badge_padding_x: int = 12
    badge_padding_y: int = 8
    badge_corner_radius: int = 12
    badge_shadow_offset: int = 3
    arrow_head_length: int = 18
    arrow_head_angle: float = math.pi / 7
    click_ring_radius: int = 10
    click_ring_spacing: int = 7
    click_ring_width: int = 3
    click_tick_length: int = 7
    click_center_radius: int = 4
    click_halo_opacity: int = 64
    cursor_scale: int = 42
    cursor_shadow_offset: int = 2
    scroll_pill_padding_x: int = 12
    scroll_pill_padding_y: int = 8
    scroll_pill_radius: int = 14
    scroll_icon_size: int = 22
    scroll_glyph_gap: int = 8
    scroll_chip_radius: int = 10
    scroll_chip_arrow_stroke: int = 3
    drag_start_opacity: int = 120
    drag_path_alpha: int = 235
    drag_shadow_alpha: int = 82
    drag_shadow_offset: int = 5
