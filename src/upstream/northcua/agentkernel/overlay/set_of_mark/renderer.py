"""Renderer for Set-of-Mark screenshot overlays."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import SetOfMarkBox, SetOfMarkOverlayArtifact, SetOfMarkOverlayMark, SetOfMarkOverlaySpec
from .style import Color, SetOfMarkOverlayStyle

_DANGER_WORDS = frozenset({"delete", "remove", "destroy", "danger", "discard", "cancel subscription"})


class SetOfMarkOverlayRenderer:
    """Paint ref labels and bboxes for current-viewport web screenshots."""

    def __init__(self, style: SetOfMarkOverlayStyle | None = None) -> None:
        self.style = style or SetOfMarkOverlayStyle()
        self._font = self._load_font(self.style.font_size)

    def _load_font(self, size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
        except OSError:
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                return ImageFont.load_default()

    def render(
        self,
        *,
        source_image_path: str,
        spec: SetOfMarkOverlaySpec,
        output_path: str,
    ) -> SetOfMarkOverlayArtifact:
        """Render one Set-of-Mark overlay onto a screenshot."""

        source_path = Path(source_image_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Set-of-Mark source image does not exist: {source_image_path}")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        image = Image.open(source_path).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")
        occupied_labels: list[tuple[int, int, int, int]] = []
        image_size = image.size
        target_rects = [
            rect for mark in spec.marks if (rect := self._rect_from_box(mark.bbox_image, image_size)) is not None
        ]
        for mark in spec.marks:
            color = self._mark_color(mark)
            self._draw_box(draw, mark.bbox_image, color, image_size)
            label_rect = self._draw_label(draw, mark, color, image_size, occupied_labels, target_rects)
            if label_rect is not None:
                occupied_labels.append(label_rect)

        image.save(output, format="PNG")
        return SetOfMarkOverlayArtifact(output_path=str(output), spec=spec)

    def _mark_color(self, mark: SetOfMarkOverlayMark) -> Color:
        text = f"{mark.role} {mark.name}".lower()
        if any(word in text for word in _DANGER_WORDS):
            return (220, 38, 38)
        return self.style.color_for_role(mark.role)

    def _draw_box(
        self,
        draw: ImageDraw.ImageDraw,
        box: SetOfMarkBox,
        color: Color,
        image_size: tuple[int, int],
    ) -> None:
        rect = self._rect_from_box(box, image_size)
        if rect is None:
            return
        stroke = (*color, self.style.stroke_alpha)
        halo = (*self.style.halo_color, self.style.halo_alpha)
        shadow = (*self.style.shadow_color, self.style.shadow_alpha)
        x1, y1, x2, y2 = rect
        draw.rectangle((x1 + 1, y1 + 1, x2 + 1, y2 + 1), outline=shadow, width=self.style.halo_width)
        draw.rectangle(rect, outline=halo, width=self.style.halo_width)
        if self.style.fill_alpha > 0:
            self._alpha_fill(draw, rect, color)
        draw.rectangle(rect, outline=stroke, width=self.style.stroke_width)
        if (x2 - x1) < self.style.min_target_halo or (y2 - y1) < self.style.min_target_halo:
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            radius = self.style.min_target_halo // 2
            draw.ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                outline=stroke,
                width=self.style.stroke_width,
            )

    def _alpha_fill(
        self,
        draw: ImageDraw.ImageDraw,
        rect: tuple[int, int, int, int],
        color: Color,
    ) -> None:
        draw.rectangle(rect, fill=(*color, self.style.fill_alpha))

    def _draw_label(
        self,
        draw: ImageDraw.ImageDraw,
        mark: SetOfMarkOverlayMark,
        color: Color,
        image_size: tuple[int, int],
        occupied_labels: list[tuple[int, int, int, int]],
        target_rects: list[tuple[int, int, int, int]],
    ) -> tuple[int, int, int, int] | None:
        box_rect = self._rect_from_box(mark.bbox_image, image_size)
        if box_rect is None:
            return None
        label = mark.label or mark.ref
        text_box = draw.textbbox((0, 0), label, font=self._font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        chip_width = text_width + self.style.chip_padding_x * 2
        chip_height = text_height + self.style.chip_padding_y * 2
        chip_rect = self._choose_label_rect(
            box_rect=box_rect,
            chip_size=(chip_width, chip_height),
            image_size=image_size,
            occupied_labels=occupied_labels,
            target_rects=target_rects,
        )
        if chip_rect is None:
            return None
        self._draw_leader_if_needed(draw, box_rect, chip_rect, color)
        self._draw_chip(draw, chip_rect, color)
        draw.text(
            (
                chip_rect[0] + self.style.chip_padding_x,
                chip_rect[1] + self.style.chip_padding_y - text_box[1],
            ),
            label,
            font=self._font,
            fill=(*self.style.text_color, 255),
        )
        return chip_rect

    def _draw_chip(self, draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], color: Color) -> None:
        shadow_rect = (rect[0] + 1, rect[1] + 1, rect[2] + 1, rect[3] + 1)
        draw.rounded_rectangle(
            shadow_rect,
            radius=self.style.chip_radius,
            fill=(*self.style.shadow_color, self.style.shadow_alpha),
        )
        draw.rounded_rectangle(
            rect,
            radius=self.style.chip_radius,
            fill=(*color, 245),
            outline=(*self.style.halo_color, 230),
            width=1,
        )

    def _draw_leader_if_needed(
        self,
        draw: ImageDraw.ImageDraw,
        box_rect: tuple[int, int, int, int],
        chip_rect: tuple[int, int, int, int],
        color: Color,
    ) -> None:
        gap = self.style.chip_gap
        touches = not (
            chip_rect[2] < box_rect[0] - gap
            or chip_rect[0] > box_rect[2] + gap
            or chip_rect[3] < box_rect[1] - gap
            or chip_rect[1] > box_rect[3] + gap
        )
        if touches:
            return
        chip_center = ((chip_rect[0] + chip_rect[2]) // 2, (chip_rect[1] + chip_rect[3]) // 2)
        box_center = ((box_rect[0] + box_rect[2]) // 2, (box_rect[1] + box_rect[3]) // 2)
        draw.line((chip_center, box_center), fill=(*self.style.halo_color, 220), width=3)
        draw.line((chip_center, box_center), fill=(*color, 220), width=1)

    def _choose_label_rect(
        self,
        *,
        box_rect: tuple[int, int, int, int],
        chip_size: tuple[int, int],
        image_size: tuple[int, int],
        occupied_labels: list[tuple[int, int, int, int]],
        target_rects: list[tuple[int, int, int, int]],
    ) -> tuple[int, int, int, int] | None:
        x1, y1, x2, y2 = box_rect
        chip_width, chip_height = chip_size
        gap = self.style.chip_gap
        if (y2 - y1) <= chip_height + gap:
            candidates = [
                (x2 + gap, y1),
                (x1 - chip_width - gap, y1),
                (x2 + gap, y2 - chip_height),
                (x1 - chip_width - gap, y2 - chip_height),
                (x1, y1 - chip_height - gap),
                (x2 - chip_width, y1 - chip_height - gap),
                (x1, y2 + gap),
                (x2 - chip_width, y2 + gap),
            ]
        else:
            candidates = [
                (x1, y1 - chip_height - gap),
                (x2 - chip_width, y1 - chip_height - gap),
                (x1, y2 + gap),
                (x2 - chip_width, y2 + gap),
                (x2 + gap, y1),
                (x1 - chip_width - gap, y1),
                (x2 + gap, y2 - chip_height),
                (x1 - chip_width - gap, y2 - chip_height),
            ]
        candidates = list(dict.fromkeys(candidates))
        allow_inside_target = self._allow_inside_target_label(box_rect, chip_size, image_size)
        scored: list[tuple[float, tuple[int, int, int, int]]] = []
        for raw_x, raw_y in candidates:
            rect = self._clamp_rect(raw_x, raw_y, chip_width, chip_height, image_size)
            label_overlap = sum(self._rect_intersection_area(rect, occupied) for occupied in occupied_labels)
            if label_overlap > 0:
                continue
            overlap_area = sum(self._rect_intersection_area(rect, target) for target in target_rects)
            own_overlap_area = self._rect_intersection_area(rect, box_rect)
            if own_overlap_area > 0 and not allow_inside_target:
                continue
            moved = abs(rect[0] - int(round(raw_x))) + abs(rect[1] - int(round(raw_y)))
            distance = self._rect_center_distance(rect, box_rect)
            scored.append((overlap_area * 1000 + own_overlap_area * 5000 + moved * 10 + distance, rect))
        if not scored:
            for raw_x, raw_y in candidates:
                rect = self._clamp_rect(raw_x, raw_y, chip_width, chip_height, image_size)
                label_overlap = sum(self._rect_intersection_area(rect, occupied) for occupied in occupied_labels)
                overlap_area = sum(self._rect_intersection_area(rect, target) for target in target_rects)
                own_overlap_area = self._rect_intersection_area(rect, box_rect)
                if own_overlap_area > 0 and not allow_inside_target:
                    continue
                moved = abs(rect[0] - int(round(raw_x))) + abs(rect[1] - int(round(raw_y)))
                distance = self._rect_center_distance(rect, box_rect)
                scored.append(
                    (
                        label_overlap * 100_000 + overlap_area * 1000 + own_overlap_area * 5000 + moved * 10 + distance,
                        rect,
                    )
                )
        if not scored:
            return None
        _score, best = min(scored, key=lambda item: item[0])
        return best

    def _allow_inside_target_label(
        self,
        box_rect: tuple[int, int, int, int],
        chip_size: tuple[int, int],
        image_size: tuple[int, int],
    ) -> bool:
        box_area = self._rect_area(box_rect)
        image_area = max(1, image_size[0] * image_size[1])
        chip_area = max(1, chip_size[0] * chip_size[1])
        return box_area >= image_area * 0.18 or box_area >= chip_area * 18

    def _rect_from_box(self, box: SetOfMarkBox, image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
        image_width, image_height = image_size
        x1 = max(0, min(image_width - 1, int(round(box.x))))
        y1 = max(0, min(image_height - 1, int(round(box.y))))
        x2 = max(0, min(image_width - 1, int(round(box.right))))
        y2 = max(0, min(image_height - 1, int(round(box.bottom))))
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1, y1, x2, y2)

    def _clamp_rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        image_size: tuple[int, int],
    ) -> tuple[int, int, int, int]:
        image_width, image_height = image_size
        effective_width = min(width, max(1, image_width))
        effective_height = min(height, max(1, image_height))
        max_x = max(0, image_width - effective_width)
        max_y = max(0, image_height - effective_height)
        clamped_x = max(0, min(max_x, int(round(x))))
        clamped_y = max(0, min(max_y, int(round(y))))
        return (clamped_x, clamped_y, clamped_x + effective_width, clamped_y + effective_height)

    def _rects_overlap(self, first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
        padding = 2
        return not (
            first[2] + padding <= second[0]
            or second[2] + padding <= first[0]
            or first[3] + padding <= second[1]
            or second[3] + padding <= first[1]
        )

    def _rect_intersection_area(
        self,
        first: tuple[int, int, int, int],
        second: tuple[int, int, int, int],
    ) -> int:
        x1 = max(first[0], second[0])
        y1 = max(first[1], second[1])
        x2 = min(first[2], second[2])
        y2 = min(first[3], second[3])
        if x2 <= x1 or y2 <= y1:
            return 0
        return (x2 - x1) * (y2 - y1)

    def _rect_area(self, rect: tuple[int, int, int, int]) -> int:
        return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])

    def _rect_center_distance(
        self,
        first: tuple[int, int, int, int],
        second: tuple[int, int, int, int],
    ) -> float:
        first_x = (first[0] + first[2]) / 2
        first_y = (first[1] + first[3]) / 2
        second_x = (second[0] + second[2]) / 2
        second_y = (second[1] + second[3]) / 2
        return abs(first_x - second_x) + abs(first_y - second_y)
