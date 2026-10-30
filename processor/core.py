"""处理器管道核心：注册表、上下文、执行引擎。"""
import functools
import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from itertools import chain
from typing import Any, Dict, Iterator, List, MutableMapping, Optional, Type

from PIL import Image, ImageColor, ImageOps

from core.logger import logger

from processor.types import Alignment, Direction  # noqa: F401  (对外兼容导出)


class PipelineContext(MutableMapping):
    """管道上下文：承载单个处理器的配置与图像缓冲区。"""

    def __init__(self, config: Dict[str, Any]):
        self._config = config

    def get(self, key: str, default: Any = None) -> Any:
        value = self._config.get(key)
        return value if value is not None else default

    def get_exif(self) -> Dict[str, Any]:
        return self.get("exif") or {}

    def getcolor(self, key: str, default: Any = None) -> Any:
        return _parse_color(self._config.get(key, default))

    def getint(self, key: str, default: int = 0) -> int:
        try:
            return int(float(str(self.get(key, default)).strip()))
        except (TypeError, ValueError):
            return default

    def getfloat(self, key: str, default: float = 0.0) -> float:
        try:
            return float(str(self.get(key, default)).strip())
        except (TypeError, ValueError):
            return default

    def getbool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def getenum(self, key: str, default: Any = None, enum: Type[Enum] = None) -> Any:
        value = self.get(key, default)
        if enum is None:
            return value
        if isinstance(value, enum):
            return value
        if isinstance(value, str):
            for candidate in (value, value.upper(), value.lower()):
                try:
                    return enum[candidate]
                except KeyError:
                    continue
        try:
            return enum(value)
        except (ValueError, KeyError):
            return default

    def get_processor_name(self):
        return self.get("processor_name")

    def get_buffer(self) -> List[Image.Image]:
        if not self.get("buffer_loaded", False) and self.get("buffer_path"):
            buffer = []
            for path in self.get("buffer_path"):
                # with 块内完成像素加载，避免长期占用原图文件句柄
                with Image.open(path) as im:
                    buffer.append(ImageOps.exif_transpose(im))
            self.set("buffer", buffer)
            self.set("buffer_loaded", True)
        return self.get("buffer") or []

    def set(self, key: str, value: Any):
        self._config[key] = value
        return self

    def save_buffer(self, processor_name: str, force_save: bool = False):
        """调试用：把当前缓冲区落盘到 output 目录。"""
        if not (force_save or self.get("save_buffer", False)):
            return self
        directory = self.get("output", "./tmp")
        os.makedirs(directory, exist_ok=True)
        buffer_path = []
        for img in self.get_buffer():
            if img.mode == "RGB":
                ext = "jpg"
            elif img.mode == "RGBA":
                ext = "png"
            else:
                img = img.convert("RGBA")
                ext = "png"
            filename = f"{processor_name}_{int(time.time())}_{uuid.uuid4().hex}.{ext}"
            path = os.path.join(directory, filename)
            img.save(path)
            buffer_path.append(path)
            logger.debug(f"Saved buffer: {path}")
        self.set("buffer_path", buffer_path)
        return self

    def update_buffer(self, buffer: List[Image.Image]):
        self.set("buffer", buffer)
        self.set("buffer_loaded", True)
        return self

    def success(self, success: bool = True):
        self.set("success", success)
        return self

    # ---- MutableMapping ----
    def __getitem__(self, key: str) -> Any:
        return self._config[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._config[key] = value

    def __delitem__(self, key: str) -> None:
        del self._config[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._config)

    def __len__(self) -> int:
        return len(self._config)


class ImageProcessor(ABC):
    @abstractmethod
    def process(self, ctx: PipelineContext):
        ...

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def category(self) -> str:
        ...

    def __init_subclass__(cls, **kwargs):
        """给每个子类的 process 方法套一层耗时监控（注册逻辑见 processor/__init__.py）。"""
        super().__init_subclass__(**kwargs)
        original_process = cls.process

        @functools.wraps(original_process)
        def wrapper(self, ctx: PipelineContext):
            start = time.perf_counter()
            try:
                return original_process(self, ctx)
            finally:
                cost = (time.perf_counter() - start) * 1000
                logger.debug(f"[monitor]processor#{self.name()} cost {cost:.2f}ms")

        cls.process = wrapper


def _parse_color(color) -> tuple:
    """解析颜色为 RGBA 元组，支持元组 / '255,255,255,0' / '#RRGGBB' / 颜色名。"""
    if color is None:
        return (0, 0, 0, 0)
    if isinstance(color, (tuple, list)):
        if len(color) == 3:
            return (*color, 255)
        return tuple(color)

    if isinstance(color, str):
        text = color.strip()
        cleaned = text.strip("()")
        if "," in cleaned:
            try:
                parts = [int(float(x.strip())) for x in cleaned.split(",")]
                if len(parts) == 3:
                    return (*parts, 255)
                if len(parts) == 4:
                    return tuple(parts)
            except ValueError:
                pass
        try:
            rgba = ImageColor.getrgb(text)
            return (*rgba, 255) if len(rgba) == 3 else rgba
        except ValueError:
            pass

    raise ValueError(f"无法解析颜色: {color}")


_processor_registry: Dict[str, Type[ImageProcessor]] = {}


def get_all_processors() -> Dict[str, Type[ImageProcessor]]:
    return _processor_registry.copy()


def get_processor(key: str) -> Optional[Type[ImageProcessor]]:
    return _processor_registry.get(key)


def register_processor(key: str, processor_cls: Type[ImageProcessor]):
    if key in _processor_registry:
        return
    _processor_registry[key] = processor_cls
    logger.debug(f"Registered processor: {key} -> {processor_cls.__name__}")


# ---------------------------------------------------------------- 照片位置追踪
#
# 痛点：模板里的「画框 / 留白 / 投影」都会把画布撑大，所以成片和原图的尺寸
# 天生不一样（实测 标准水印2 输出 2000×1615，原图只有 1400×934）。界面上的
# 「对比」模式要把原图和成片沿分割线左右分布，如果不知道「原图内容落在成片
# 的哪个矩形里」，两张图就只能各自居中缩放，看起来就是一张盖住另一张。
#
# 下面这组函数沿着管道累积这个矩形。它们只写 `_photo_rect` 这一个上下文键，
# 完全不碰像素，因此不会影响任何既有输出。

def _photo_rect_get(ctx) -> Optional[tuple]:
    rect = ctx.get("_photo_rect")
    if not rect:
        return None
    try:
        x, y, w, h = (float(v) for v in rect)
    except (TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _track_rect(rect, buffer) -> Optional[tuple]:
    """校验追踪结果：必须仍落在当前画布内，否则判为不可信。

    任何一个会改变画布的处理器只要漏了同步，矩形就会越界；这里统一兜住，
    让预览退回「居中」的保守排布，而不是把原图摆到一个错误的位置。
    """
    if not rect or not buffer:
        return None
    x, y, w, h = rect
    img = buffer[0]
    eps = 1.0
    if x < -eps or y < -eps or w <= 0 or h <= 0:
        return None
    if x + w > img.width + eps or y + h > img.height + eps:
        return None
    return (x, y, w, h)


def intersect_rect(rect, left: float, top: float, width: float, height: float):
    """求矩形与裁切窗 (left, top, width, height) 的交集（坐标平移到裁切后的画布）。"""
    if not rect:
        return None
    x0, y0, w, h = rect
    nx0 = max(x0, left) - left
    ny0 = max(y0, top) - top
    nx1 = min(x0 + w, left + width) - left
    ny1 = min(y0 + h, top + height) - top
    if nx1 <= nx0 or ny1 <= ny0:
        return None
    return (nx0, ny0, nx1 - nx0, ny1 - ny0)


def scale_rect(rect, factor: float):
    """等比缩放矩形。"""
    if not rect:
        return None
    return tuple(v * factor for v in rect)


def shift_photo_rect(ctx, dx: float, dy: float) -> None:
    """画布向左上被撑大时，把照片矩形整体平移。"""
    rect = _photo_rect_get(ctx)
    if not rect:
        ctx.set("_photo_rect", None)
        return
    ctx.set("_photo_rect", (rect[0] + dx, rect[1] + dy, rect[2], rect[3]))


def scale_photo_rect(ctx, factor: float) -> None:
    """整张画布被等比缩放时，照片矩形同步缩放。"""
    ctx.set("_photo_rect", scale_rect(_photo_rect_get(ctx), factor))


def crop_photo_rect(ctx, left: float, top: float, width: float, height: float) -> None:
    """画布被裁切到 (left, top, width, height) 时，求照片矩形与裁切窗的交集。"""
    ctx.set("_photo_rect", intersect_rect(_photo_rect_get(ctx), left, top, width, height))


def clear_photo_rect(ctx) -> None:
    """标记「照片位置已无法按简单平移/缩放追踪」（旋转、自动裁边、拼接等）。"""
    ctx.set("_photo_rect", None)


def start_process(data: List[dict], input_path: str = None, output_path: str = None,
                  initial_buffer: List = None, exif: dict = None,
                  save_options: dict = None, return_meta: bool = False):
    """执行处理管道。

    Args:
        data: 处理器配置列表（模板渲染后的 JSON）
        input_path: 输入文件路径
        output_path: 输出文件路径（为 None 时只返回结果不落盘）
        initial_buffer: 初始图像缓冲区
        exif: 预先读取好的 EXIF（避免重复调用 exiftool）
        save_options: 落盘选项 {quality, subsampling, preserve_exif, long_edge, output_format}
        return_meta: 为 True 时返回 (结果图, 元信息)；元信息里带 photo_rect
            （原始照片内容在最终画布中的 (x, y, w, h)），供界面「对比」模式对齐。
            默认 False，保持既有调用方的返回值不变。
    """
    if not data:
        raise RuntimeError("处理管道为空，请检查模板内容")

    nodes = [PipelineContext(dict(datum)) for datum in data]

    if initial_buffer is not None:
        nodes[0].set("buffer", initial_buffer)
        nodes[0].set("buffer_loaded", True)
    elif input_path is not None:
        nodes[0].set("buffer_path", [input_path])

    if input_path is not None:
        if exif is None:
            from core.util import get_exif
            exif = get_exif(input_path)
        for node in nodes:
            if "exif" not in node:
                node["exif"] = exif

    output = nodes[0].get_buffer()
    all_buffer = [output]
    last_merger_idx = -1

    # 初始时「照片」就是整张画布；后续由各处理器按自己的几何变更同步
    photo_rect = None
    if output:
        photo_rect = (0.0, 0.0, float(output[0].width), float(output[0].height))

    for idx, node in enumerate(nodes):
        processor = get_processor(node.get_processor_name())
        if processor is None:
            raise RuntimeError(f"Processor '{node.get_processor_name()}' not found")

        instance: ImageProcessor = processor()
        node.set("_photo_rect", photo_rect)
        if "select" in node:
            indexes = json.loads(node["select"])
            node.update_buffer(list(chain.from_iterable(all_buffer[i] for i in indexes)))
        elif instance.category() != "merger":
            node.update_buffer(output)
        else:
            buffers = all_buffer[last_merger_idx + 1: idx + 1]
            node.update_buffer(list(chain.from_iterable(buffers)))
            last_merger_idx = idx
            # 拼接会重排画布，位置无法用平移/缩放表达，直接标记为未知
            node.set("_photo_rect", None)

        instance.process(node)
        output = node.get_buffer()
        all_buffer.append(output)
        photo_rect = _track_rect(_photo_rect_get(node), output)

    nodes[-1].save_buffer("final").success()
    result = output[0]

    if output_path is not None:
        from core.imaging import save_output
        opts = save_options or {}
        save_output(result, output_path, source_path=input_path, **opts)

    if return_meta:
        return result, {"photo_rect": photo_rect,
                        "width": result.width,
                        "height": result.height}
    return result
