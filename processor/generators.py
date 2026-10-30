"""生成器类处理器：纯色、渐变、文字、图片加载。"""
from abc import ABC
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.configs import fonts_dir
from processor.core import Direction, ImageProcessor, PipelineContext, _parse_color

BASE_FONT_SIZE = 512


def load_font(font_path: str):
    """加载字体；相对路径基于用户字体目录，找不到则逐级降级。"""
    try:
        if font_path:
            font_file = Path(font_path)
            if not font_file.is_absolute():
                font_file = fonts_dir / font_path
            if font_file.exists():
                return ImageFont.truetype(str(font_file), BASE_FONT_SIZE)
        for fallback in (fonts_dir / "AlibabaPuHuiTi-2-45-Light.otf", "arial.ttf",
                         "Arial.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(str(fallback), BASE_FONT_SIZE)
            except OSError:
                continue
    except OSError:
        pass
    return ImageFont.load_default()


@dataclass
class TextSegment:
    text: str
    font_path: Optional[str] = None
    height: int = 100
    is_bold: bool = False
    color: str = "black"
    trim: bool = False

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    @staticmethod
    def from_dict(data: dict):
        return TextSegment(
            text=data.get("text"),
            font_path=data.get("font_path", None),
            height=int(data.get("height", 100)),
            color=data.get("color", "black"),
            is_bold=bool(data.get("is_bold", False)),
            trim=bool(data.get("trim", False)),
        )

    @staticmethod
    def from_dicts(data: List[dict]):
        return [TextSegment.from_dict(datum) for datum in (data or [])]


class Generator(ImageProcessor, ABC):
    def category(self) -> str:
        return "generator"


class SolidColorGenerator(Generator):
    def process(self, ctx: PipelineContext):
        image = Image.new("RGBA", (ctx.getint("width"), ctx.getint("height")), ctx.get("color"))
        ctx.update_buffer([image]).success()

    def name(self) -> str:
        return "solid_color"


class InterpolateMethod(Enum):
    LINEAR = "linear"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"
    EASE_IN_OUT = "ease_in_out"


def _easing_linear(t):
    return t


def _easing_ease_in(t):
    return t * t


def _easing_ease_out(t):
    return 1 - (1 - t) ** 2


def _easing_ease_in_out(t):
    return np.where(t < 0.5, 2 * t * t, 1 - (-2 * t + 2) ** 2 / 2)


EASING_FUNCTIONS = {
    InterpolateMethod.LINEAR: _easing_linear,
    InterpolateMethod.EASE_IN: _easing_ease_in,
    InterpolateMethod.EASE_OUT: _easing_ease_out,
    InterpolateMethod.EASE_IN_OUT: _easing_ease_in_out,
}


def _draw_gradient_numpy(width, height, start_rgba, end_rgba, direction, method=InterpolateMethod.LINEAR):
    """NumPy 向量化渐变绘制。"""
    x = np.arange(width)
    y = np.arange(height)
    xx, yy = np.meshgrid(x, y)

    if direction == Direction.HORIZONTAL:
        t = xx / (width - 1) if width > 1 else np.zeros_like(xx, dtype=float)
    elif direction == Direction.VERTICAL:
        t = yy / (height - 1) if height > 1 else np.zeros_like(yy, dtype=float)
    elif direction == Direction.DIAGONAL:
        t = (xx + yy) / (width + height - 2) if (width + height) > 2 else np.zeros_like(xx, dtype=float)
    elif direction == Direction.RADIAL:
        cx, cy = width / 2, height / 2
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        t = np.clip(dist / np.sqrt(cx ** 2 + cy ** 2), 0, 1)
    else:
        t = np.zeros((height, width), dtype=float)

    t = EASING_FUNCTIONS.get(method, _easing_linear)(t.astype(float))
    start = np.array(start_rgba, dtype=float)
    end = np.array(end_rgba, dtype=float)
    pixels = start + (end - start) * t[:, :, np.newaxis]
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), mode="RGBA")


class GradientColorGenerator(Generator):
    def process(self, ctx: PipelineContext):
        image = _draw_gradient_numpy(
            ctx.getint("width"), ctx.getint("height"),
            _parse_color(ctx.get("start_color")), _parse_color(ctx.get("end_color")),
            ctx.getenum("direction", Direction.HORIZONTAL, Direction),
            ctx.getenum("interpolate_method", InterpolateMethod.LINEAR, InterpolateMethod),
        )
        ctx.update_buffer([image]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "gradient_color"


class RichTextGenerator(Generator):
    @staticmethod
    def generate(segment: TextSegment) -> Image.Image:
        font = load_font(segment.font_path)
        metrics = font.getmetrics()
        text = segment.text if segment.text else " "
        bbox = font.getbbox(text)
        image = Image.new("RGBA", (max(1, int(bbox[2] - bbox[0])), metrics[0] + abs(metrics[1])), (0, 0, 0, 0))
        ImageDraw.Draw(image).text((0, 0), text, font=font, fill=_parse_color(segment.color))

        from processor.core import start_process
        pipeline = [
            {"processor_name": "trim", "trim_top": segment.trim,
             "trim_bottom": segment.trim, "save_buffer": False},
            {"processor_name": "resize",
             "height": segment.height * 1.13 if segment.is_bold else segment.height,
             "save_buffer": False},
        ]
        return start_process(pipeline, input_path=None, output_path=None, initial_buffer=[image])

    def process(self, ctx: PipelineContext):
        ctx.update_buffer([RichTextGenerator.generate(TextSegment.from_dict(ctx))]) \
            .save_buffer(self.name()).success()

    def name(self) -> str:
        return "rich_text"


class MultiRichTextGenerator(Generator):
    def process(self, ctx: PipelineContext):
        segments: List[TextSegment] = TextSegment.from_dicts(ctx.get("text_segments"))
        alignment = ctx.get("text_alignment")
        spacing = ctx.getint("text_spacing")
        height = ctx.get("height", 100)

        text_images = []
        for segment in segments:
            segment.height = height
            context = PipelineContext(asdict(segment))
            context.set("save_buffer", False)
            RichTextGenerator().process(context)
            text_images.extend(context.get_buffer())

        if not text_images:
            ctx.update_buffer([]).success()
            return

        from processor.core import start_process
        result = start_process(
            [{"processor_name": "concat", "alignment": alignment,
              "spacing": spacing, "save_buffer": False}],
            initial_buffer=text_images,
        )
        ctx.update_buffer([result]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "multi_rich_text"


class ImageLoader(Generator):
    def process(self, ctx: PipelineContext):
        path = ctx.get("path")
        if isinstance(path, str):
            ctx.update_buffer([Image.open(path)]).success()
        elif isinstance(path, list):
            ctx.update_buffer([Image.open(p) for p in path]).success()

    def name(self) -> str:
        return "image"
