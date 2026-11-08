"""Style constants for Set-of-Mark overlays."""

from __future__ import annotations

from dataclasses import dataclass

Color = tuple[int, int, int]


@dataclass(frozen=True)
class SetOfMarkOverlayStyle:
    """Rendering constants optimized for agent-readable screenshot labels."""

    role_colors: dict[str, Color] | None = None
    default_color: Color = (37, 99, 235)
    text_color: Color = (255, 255, 255)
    halo_color: Color = (255, 255, 255)
    shadow_color: Color = (15, 23, 42)
    fill_alpha: int = 0
    stroke_alpha: int = 230
    halo_alpha: int = 210
    shadow_alpha: int = 145
    font_size: int = 14
    stroke_width: int = 1
    halo_width: int = 2
    chip_padding_x: int = 5
    chip_padding_y: int = 3
    chip_gap: int = 4
    chip_radius: int = 5
    min_target_halo: int = 16

    def color_for_role(self, role: str) -> Color:
        """Return the semantic color for a role."""

        role_key = role.lower().strip()
        palette = self.role_colors or {
            "button": (37, 99, 235),
            "menuitem": (37, 99, 235),
            "tab": (37, 99, 235),
            "textbox": (22, 163, 74),
            "searchbox": (22, 163, 74),
            "combobox": (22, 163, 74),
            "listbox": (22, 163, 74),
            "checkbox": (234, 88, 12),
            "radio": (234, 88, 12),
            "switch": (234, 88, 12),
            "link": (124, 58, 237),
        }
        return palette.get(role_key, self.default_color)
