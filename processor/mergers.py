"""合并器类处理器：多图叠加对齐、顺序拼接。"""
import json
from abc import ABC
from typing import List

from PIL import Image

from processor.core import Direction, ImageProcessor, PipelineContext
from processor.types import Alignment


def _calc_offset(size: int, max_size: int, alignment: Alignment) -> int:
    value = alignment.value
    if value == "start":
        return 0
    if value == "center":
        return (max_size - size) // 2
    if value == "end":
        return max_size - size
    return 0


class Merger(ImageProcessor, ABC):
    def category(self) -> str:
        return "merger"


class AlignmentMerger(Merger):
    """把多张图按对齐方式叠加到同一张画布（常用于把文字压到原图上）。"""

    def process(self, ctx: PipelineContext):
        buffer: List[Image.Image] = ctx.get_buffer()
        if not buffer:
            return
        horizontal = ctx.getenum("horizontal_alignment", Alignment.CENTER, Alignment)
        vertical = ctx.getenum("vertical_alignment", Alignment.CENTER, Alignment)
        background = ctx.getcolor("background", (255, 255, 255, 0))
        try:
            offsets = json.loads(ctx.get("offsets", "[]") or "[]")
            weights = json.loads(ctx.get("weights", "[]") or "[]")
        except (ValueError, TypeError):
            offsets, weights = [], []

        max_width = max(img.width for img in buffer)
        max_height = max(img.height for img in buffer)
        canvas = Image.new("RGBA", (max_width, max_height), background)

        if weights:
            missing = len(buffer) - len(weights)
            weights = list(weights) + [0] * max(0, missing)
            buffer = [item for _, item in sorted(zip(weights, buffer), key=lambda x: x[0])]

        for i, img in enumerate(buffer):
            offset_x, offset_y = offsets[i] if i < len(offsets) else (0, 0)
            img_x = _calc_offset(img.width, max_width, horizontal) - offset_x
            img_y = _calc_offset(img.height, max_height, vertical) - offset_y
            paste_img = img if img.mode == "RGBA" else img.convert("RGBA")
            canvas.paste(paste_img, (img_x, img_y), paste_img)

        ctx.update_buffer([canvas]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "alignment"


class ConcatMerger(Merger):
    """按水平 / 垂直方向顺序拼接多张图。"""

    def process(self, ctx: PipelineContext):
        buffer = ctx.get_buffer()
        if not buffer:
            return
        alignment = ctx.getenum("alignment", Alignment.BOTTOM, Alignment)
        direction = ctx.getenum("direction", Direction.HORIZONTAL, Direction)
        spacing = ctx.getint("spacing", 10)
        background = ctx.getcolor("background", (255, 255, 255, 0))

        images = [img.convert("RGBA") if img.mode != "RGBA" else img for img in buffer]

        if direction == Direction.HORIZONTAL:
            total_width = sum(img.width for img in images) + spacing * (len(images) - 1)
            max_height = max(img.height for img in images)
            canvas_size = (total_width, max_height)
        else:
            max_width = max(img.width for img in images)
            total_height = sum(img.height for img in images) + spacing * (len(images) - 1)
            canvas_size = (max_width, total_height)

        canvas = Image.new("RGBA", canvas_size, background)
        pos = 0
        for img in images:
            if direction == Direction.HORIZONTAL:
                y = _calc_offset(img.height, canvas_size[1], alignment)
                canvas.paste(img, (pos, y), img)
                pos += img.width + spacing
            else:
                x = _calc_offset(img.width, canvas_size[0], alignment)
                canvas.paste(img, (x, pos), img)
                pos += img.height + spacing

        ctx.update_buffer([canvas]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "concat"
