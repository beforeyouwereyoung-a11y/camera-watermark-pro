"""滤镜类处理器：模糊、缩放、裁切、留白、圆角、阴影、水印等。"""
import json
import re
from abc import ABC
from pathlib import Path
from typing import Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.configs import fonts_dir
from core.logger import logger
from processor.core import (ImageProcessor, PipelineContext, get_processor, start_process,
                            _parse_color, clear_photo_rect, crop_photo_rect,
                            scale_photo_rect, shift_photo_rect)
from processor.types import Alignment


class FilterProcessor(ImageProcessor, ABC):
    def category(self) -> str:
        return "filter"


class BlurFilter(FilterProcessor):
    """高斯模糊（常用于生成背景虚化）。"""

    def process(self, ctx: PipelineContext):
        radius = ctx.getint("blur_radius", 5)
        buffer = []
        for img in ctx.get_buffer():
            if img.mode != "RGB":
                img = img.convert("RGB")
            buffer.append(img.filter(ImageFilter.GaussianBlur(radius=radius)))
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "blur"


class ResizeFilter(FilterProcessor):
    """按宽/高/比例缩放。"""

    def process(self, ctx: PipelineContext):
        width, height = ctx.get("width"), ctx.get("height")
        scale = ctx.get("scale")

        buffer = []
        for img in ctx.get_buffer():
            if width and height:
                target_size = (int(width), int(height))
            else:
                if width:
                    factor = float(width) / img.width
                elif height:
                    factor = float(height) / img.height
                elif scale:
                    factor = float(scale)
                else:
                    ctx.set("success", False)
                    return
                target_size = (max(1, int(img.width * factor)), max(1, int(img.height * factor)))
            buffer.append(img.resize(target_size, resample=Image.Resampling.LANCZOS))
            scale_photo_rect(ctx, target_size[0] / img.width if img.width else 1.0)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "resize"


class TrimFilter(FilterProcessor):
    """自动裁掉与背景同色的边缘（用于清理留白）。"""

    def process(self, ctx: PipelineContext):
        buffer = []
        for image in ctx.get_buffer():
            if image.height * image.width == 0:
                continue
            bbox = self.get_foreground_bbox(
                image,
                trim_left=ctx.get("trim_left", True),
                trim_right=ctx.get("trim_right", True),
                trim_top=ctx.get("trim_top", True),
                trim_bottom=ctx.get("trim_bottom", True),
            )
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                buffer.append(image.crop(bbox))
            else:
                buffer.append(image)
        # 自动裁边的边界由图像内容决定，位置无法预判
        clear_photo_rect(ctx)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "trim"

    @staticmethod
    def _get_background_color(img_array: np.ndarray) -> np.ndarray:
        corners = np.array([
            img_array[0, 0], img_array[0, -1],
            img_array[-1, 0], img_array[-1, -1],
        ])
        return np.mean(corners, axis=0)

    def get_foreground_bbox(self, image: Image.Image, threshold: float = 10.0, padding: int = 0,
                            trim_left: bool = True, trim_right: bool = True,
                            trim_top: bool = True, trim_bottom: bool = True) -> Tuple[int, int, int, int]:
        img_array = np.array(image.convert("RGBA"), dtype=np.float32)
        height, width, _ = img_array.shape
        background = self._get_background_color(img_array)
        diff = np.sqrt(np.sum((img_array - background) ** 2, axis=-1))
        exceeds = diff > threshold
        col_exceeds = np.any(exceeds, axis=0)
        row_exceeds = np.any(exceeds, axis=1)
        if not np.any(col_exceeds):
            return 0, width, 0, height
        left = int(np.argmax(col_exceeds))
        right = int(width - np.argmax(col_exceeds[::-1]))
        top = int(np.argmax(row_exceeds))
        bottom = int(height - np.argmax(row_exceeds[::-1]))
        if not trim_left:
            left = 0
        if not trim_right:
            right = width
        if not trim_top:
            top = 0
        if not trim_bottom:
            bottom = height
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(width, right + padding)
        bottom = min(height, bottom + padding)
        return left, top, right, bottom


class MarginFilter(FilterProcessor):
    """按像素给图片四周加留白。"""

    def process(self, ctx: PipelineContext):
        left = ctx.getint("left_margin", 0)
        right = ctx.getint("right_margin", 0)
        top = ctx.getint("top_margin", 0)
        bottom = ctx.getint("bottom_margin", 0)
        color = ctx.get("margin_color", "white")

        buffer = []
        for img in ctx.get_buffer():
            canvas = Image.new(img.mode, (img.width + left + right, img.height + top + bottom), color)
            canvas.paste(img, (left, top))
            buffer.append(canvas)
            shift_photo_rect(ctx, left, top)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "margin"


class MarginWithRatioFilter(FilterProcessor):
    """按指定画幅比例补留白（如 2.25:1）。"""

    ratio_pattern = re.compile(r"[0-9.]+:[0-9.]+")
    ratio_threshold = 0.01

    def process(self, ctx: PipelineContext):
        buffer = ctx.get_buffer()
        if not buffer:
            return
        exif = ctx.get_exif()
        try:
            real_ratio = int(exif.get("ImageWidth")) / int(exif.get("ImageHeight"))
        except (TypeError, ValueError, ZeroDivisionError):
            real_ratio = buffer[0].width / buffer[0].height
        ratio_value = ctx.get("ratio")
        if ratio_value and self.ratio_pattern.match(str(ratio_value)):
            w, h = str(ratio_value).split(":")
            real_ratio = float(w) / float(h)

        img = buffer[0]
        cur_ratio = img.width / img.height
        if abs(cur_ratio - real_ratio) <= self.ratio_threshold:
            return
        if cur_ratio > real_ratio:
            new_h = int(img.width / real_ratio)
            pad = new_h - img.height
            ctx.set("top_margin", pad / 2)
            ctx.set("bottom_margin", pad - pad / 2)
        else:
            new_w = int(img.height * real_ratio)
            pad = new_w - img.width
            ctx.set("left_margin", pad / 2)
            ctx.set("right_margin", pad - pad / 2)
        MarginFilter().process(ctx)
        ctx.save_buffer(self.name()).success()

    def name(self) -> str:
        return "margin_with_ratio"


class WatermarkFilter(FilterProcessor):
    """经典 EXIF 信息条水印（底栏四象限 + 左右 Logo）。

    底栏原本是「左列两行 + 右列两行 + 中间 Logo」的固定排布，既不测量
    文本宽度也不做碰撞检测 —— 一旦遇到竖构图（画布窄）或过长的 EXIF
    文本（例如完整镜头型号），左右两列与中间 Logo 就会互相堆叠。

    现在改为**先测量、后排版**，逐级降级：
    ① 当前字号下各元素互不重叠 → 直接绘制（与旧行为完全一致）；
    ② 轻微重叠 → 字号小幅等比缩小（1.0 → 0.85）后重排；
    ③ 仍放不下 → 单列左对齐（左列在上、右列在下），底栏按内容自动加高；
    ④ 单行仍超宽 → 继续缩小字号（下限 0.6），最后才裁掉溢出部分。
    """

    # 两列布局尝试的字号比例（依次退让；再小就改用单列，避免同批照片字号不一致）
    TWO_COL_SCALES = (1.0, 0.95, 0.9, 0.85)
    # 单列布局尝试的字号比例
    SINGLE_COL_SCALES = (1.0, 0.9, 0.8, 0.7, 0.6)
    SINGLE_COL_MIN_SCALE = 0.6

    def process(self, ctx: PipelineContext):
        img = ctx.get_buffer()[0]
        color = ctx.get("color", "white")
        delimiter_color = ctx.get("delimiter_color", "black")
        delimiter_width = max(ctx.getint("delimiter_width", int(img.width * 0.003)), 0)
        left_margin = ctx.getint("left_margin", 0)
        right_margin = ctx.getint("right_margin", 0)
        top_margin = ctx.getint("top_margin", 0)
        bottom_margin = ctx.getint("bottom_margin", int(img.height * 0.12))
        middle_spacing = ctx.getint("middle_spacing", int(bottom_margin * 0.05))
        right_alignment = ctx.getenum("right_alignment", Alignment.RIGHT, Alignment)
        opacity = ctx.getfloat("opacity", 1.0)

        # ---- 「装裱排版」新增的两个**默认关闭**的开关（v3.5.0）----------------
        # 都是为了「照片装裱感」这套审美服务的。默认值刻意保持旧行为，
        # 这样另外 8 个内置模板的外观一个像素都不会变。
        #
        #   edge_align       底栏内容的左右参考线直接对齐「照片左右边界」
        #                    关闭时沿用旧的「内缩 2% 画布宽」
        #   right_inline     右侧改成「日期 … 间距 … Logo」单行流式，
        #                    Logo 最右边缘贴照片右边界、与日期垂直居中
        #                    关闭时沿用旧的「右列两行 + Logo 垂直居中占位」
        edge_align = ctx.getbool("edge_align", False)
        right_inline = ctx.getbool("right_inline", False)
        # Logo 渲染边长 = 左侧主标题渲染高度 × right_logo_ratio（仅 inline 模式）
        right_logo_ratio = ctx.getfloat("right_logo_ratio", 1.0)
        # Logo 与日期之间的水平间距；0 表示按照片宽度自动取值
        right_gap = ctx.getint("right_gap", 0) or max(2, int(img.width * 0.0086))

        slot_cfgs = {
            "left_top": ctx.get("left_top"),
            "left_bottom": ctx.get("left_bottom"),
            "right_top": ctx.get("right_top"),
            "right_bottom": ctx.get("right_bottom"),
        }
        for cfg in slot_cfgs.values():
            if cfg and "height" not in cfg:
                cfg["height"] = int(bottom_margin * 0.3)

        left_logo = self._open(ctx.get("left_logo"))
        right_logo = self._open(ctx.get("right_logo"))
        center_logo = self._open(ctx.get("center_logo"))
        center_logo_height = ctx.getint("center_logo_height")

        canvas_width = img.width + left_margin + right_margin
        # 「内部间距」：右列文字与竖分隔线 / Logo 之间。保持既有取值，不随开关变化。
        common_spacing = int(0.02 * canvas_width)
        # 「边缘间距」：底栏内容相对「照片左/右边界」的偏移。
        # edge_align=True 时归零，文字左边界即与照片左边界成一条线。
        align_spacing = 0 if edge_align else common_spacing
        gap = max(2, int(canvas_width * 0.005))

        def render(scale):
            slots = {}
            for key, cfg in slot_cfgs.items():
                if not cfg:
                    slots[key] = None
                    continue
                node = dict(cfg)
                node["height"] = max(1, int(int(node.get("height", 1)) * scale))
                slots[key] = start_process([node])
            return slots

        geometry = dict(
            left_margin=left_margin, right_margin=right_margin, top_margin=top_margin,
            img_height=img.height, bottom_margin=bottom_margin,
            middle_spacing=middle_spacing, common_spacing=common_spacing,
            align_spacing=align_spacing,
            canvas_width=canvas_width, delimiter_color=delimiter_color,
            right_inline=right_inline, right_logo_ratio=right_logo_ratio,
            right_gap=right_gap,
        )

        plan = None
        for scale in self.TWO_COL_SCALES:
            candidate = self._plan_two_col(
                render(scale), delimiter_width, left_logo, right_logo,
                right_alignment, **geometry)
            if self._fits(candidate["boxes"], canvas_width, gap):
                plan = candidate
                break

        if plan is None:
            for scale in self.SINGLE_COL_SCALES:
                candidate = self._plan_single_col(
                    render(scale), left_logo, right_logo, **geometry)
                if candidate["fits"]:
                    plan = candidate
                    break
            if plan is None:
                # 兜底：单列 + 裁切，保证任何极端输入都能出图
                plan = self._plan_single_col(
                    render(self.SINGLE_COL_MIN_SCALE), left_logo, right_logo, **geometry)

        canvas_height = plan["canvas_height"]
        footer_start_y = top_margin + img.height
        canvas = Image.new("RGBA", (canvas_width, canvas_height), color)
        canvas.paste(img, (left_margin, top_margin), mask=img if img.mode == "RGBA" else None)

        for element, x, y in plan["placements"]:
            if element is not None:
                canvas.paste(element, (int(x), int(y)),
                             mask=element if element.mode == "RGBA" else None)

        if center_logo:
            logo_height = center_logo_height or (canvas_height - footer_start_y)
            resize_ctx = PipelineContext({"buffer": [center_logo], "height": logo_height})
            ResizeFilter().process(resize_ctx)
            center_logo = resize_ctx.get_buffer()[0]
            center_x = (canvas.width - center_logo.width) // 2
            center_y = footer_start_y + ((canvas.height - footer_start_y) - center_logo.height) // 2
            canvas.paste(center_logo, (center_x, center_y), mask=center_logo)

        if opacity < 1.0:
            canvas = _apply_opacity(canvas, opacity)

        # 照片被贴在 (left_margin, top_margin)，四周是模板的底色（画框/留白）
        shift_photo_rect(ctx, left_margin, top_margin)
        ctx.update_buffer([canvas]).save_buffer(self.name()).success()

    # ---------------------------------------------------------------- 布局计算

    @staticmethod
    def _open(value):
        return Image.open(value).convert("RGBA") if value else None

    @staticmethod
    def _w(image):
        return image.width if image is not None else 0

    @staticmethod
    def _h(image):
        return image.height if image is not None else 0

    @staticmethod
    def _union(spans):
        x0 = min(s[0] for s in spans)
        y0 = min(s[1] for s in spans)
        x1 = max(s[2] for s in spans)
        y1 = max(s[3] for s in spans)
        return x0, y0, x1 - x0, y1 - y0

    @staticmethod
    def _overlap(a, b, gap=0):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return (ax - gap < bx + bw and bx - gap < ax + aw
                and ay - gap < by + bh and by - gap < ay + ah)

    def _fits(self, boxes, canvas_width, gap):
        """所有元素都在画布内，且两两之间留有间隙。"""
        for x, _y, w, _h in boxes:
            if x - gap < 0 or x + w + gap > canvas_width:
                return False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if self._overlap(boxes[i], boxes[j], gap):
                    return False
        return True

    def _plan_two_col(self, slots, delimiter_width, left_logo, right_logo,
                      right_alignment, left_margin, right_margin, top_margin,
                      img_height, bottom_margin, middle_spacing, common_spacing,
                      canvas_width, delimiter_color="black",
                      align_spacing=None, right_inline=False,
                      right_logo_ratio=1.0, right_gap=0):
        """两列排布：左列机型/参数，右侧日期与 Logo。

        以下为「装裱排版」新增的**可选**参数（v3.5.0），默认值等价于旧行为：
          align_spacing     内容相对照片左右边界的偏移（None → 用 common_spacing）
          right_inline      True 时右侧改为「日期 … 间距 … Logo」单行流式
          right_logo_ratio  inline 模式下 Logo 边长 ＝ 主标题高度 × 该比例
          right_gap         inline 模式下 Logo 与日期的水平间距（px）
        """
        if align_spacing is None:
            align_spacing = common_spacing

        left_top, left_bottom = slots["left_top"], slots["left_bottom"]
        right_top, right_bottom = slots["right_top"], slots["right_bottom"]

        left_h = self._h(left_top) + self._h(left_bottom)
        right_h = self._h(right_top) + self._h(right_bottom)

        # inline 模式下右侧是「日期 + Logo」单行，占用高度应按两者较高者算，
        # 而不是把右列两行相加；Logo 边长锚定左侧主标题的渲染高度。
        inline = bool(right_inline and right_logo)
        inline_logo_size = 0
        if inline:
            title_h = self._h(left_top) or self._h(right_top)
            inline_logo_size = max(1, int(title_h * right_logo_ratio))
            right_h = max(inline_logo_size, self._h(right_top), self._h(right_bottom))

        elem_height = max(left_h, right_h) + middle_spacing

        vpad = max(middle_spacing, int(bottom_margin * 0.10))
        footer_height = max(bottom_margin, elem_height + 2 * vpad)
        footer_start_y = top_margin + img_height
        canvas_height = footer_start_y + footer_height
        elem_margin = int((footer_height - elem_height) / 2)
        logo_size = max(elem_height, 1)

        placements, boxes = [], []

        left_logo_width = 0
        if left_logo:
            left_logo_width = footer_height
            placements.append((
                left_logo.resize((left_logo_width, left_logo_width), Image.Resampling.LANCZOS),
                left_margin, footer_start_y))

        # 左列起始 x：edge_align 时 align_spacing=0，正好落在照片左边界上
        l_x = left_margin + left_logo_width + align_spacing
        right_end_x = canvas_width - right_margin

        lt_h, lb_h = self._h(left_top), self._h(left_bottom)
        lt_y = canvas_height - (elem_margin + lb_h + middle_spacing + lt_h)
        lb_y = canvas_height - (elem_margin + lb_h)
        left_spans = []
        if left_top:
            placements.append((left_top, l_x, lt_y))
            left_spans.append((l_x, lt_y, l_x + self._w(left_top), lt_y + lt_h))
        if left_bottom:
            placements.append((left_bottom, l_x, lb_y))
            left_spans.append((l_x, lb_y, l_x + self._w(left_bottom), lb_y + lb_h))
        if left_spans:
            boxes.append(self._union(left_spans))

        rt_h, rb_h = self._h(right_top), self._h(right_bottom)

        if inline:
            # 单行流式：从左到右为「日期 … 间距 … Logo」。
            # Logo 最右边缘贴照片右边界，整组在底栏内垂直居中 ——
            # 与左列文字块的垂直中心线持平（elem_margin 本身就是居中量）。
            date_img = right_top or right_bottom
            date_w, date_h = self._w(date_img), self._h(date_img)
            group_h = max(inline_logo_size, date_h)
            group_y = footer_start_y + (footer_height - group_h) // 2

            logo_x = right_end_x - inline_logo_size
            logo_y = group_y + (group_h - inline_logo_size) // 2
            placements.append((
                right_logo.resize((inline_logo_size, inline_logo_size),
                                  Image.Resampling.LANCZOS), logo_x, logo_y))
            boxes.append((logo_x, logo_y, inline_logo_size, inline_logo_size))

            if date_img is not None:
                date_x = logo_x - max(right_gap, 0) - date_w
                date_y = group_y + (group_h - date_h) // 2
                placements.append((date_img, date_x, date_y))
                boxes.append((date_x, date_y, date_w, date_h))

            return {"canvas_height": canvas_height, "placements": placements,
                    "boxes": boxes}

        rt_y = lt_y + lt_h - rt_h
        rb_y = lb_y + lb_h - rb_h
        rt_x = right_end_x - self._w(right_top) - align_spacing
        rb_x = right_end_x - self._w(right_bottom) - align_spacing
        if right_alignment == Alignment.LEFT:
            rt_x = rb_x = min(rt_x, rb_x)
        right_spans = []
        if right_top:
            placements.append((right_top, rt_x, rt_y))
            right_spans.append((rt_x, rt_y, rt_x + self._w(right_top), rt_y + rt_h))
        if right_bottom:
            placements.append((right_bottom, rb_x, rb_y))
            right_spans.append((rb_x, rb_y, rb_x + self._w(right_bottom), rb_y + rb_h))
        if right_spans:
            boxes.append(self._union(right_spans))

        if right_logo and not inline:
            # 旧排布：Logo 垂直居中占位，右侧用竖分隔线把 Logo 与右列文字隔开。
            # 只有 right_inline 关闭时才走这条路径，保证既有模板外观不变。
            right_text_w = max(self._w(right_top), self._w(right_bottom))
            delimiter_w = max(delimiter_width, 1)
            delimiter_x = canvas_width - right_margin - right_text_w - 2 * common_spacing - delimiter_w
            delimiter_y = int(footer_start_y + elem_margin - logo_size * 0.05)
            placements.append((Image.new("RGBA", (delimiter_w, int(logo_size * 1.1)),
                                         delimiter_color), delimiter_x, delimiter_y))
            boxes.append((delimiter_x, delimiter_y, delimiter_w, int(logo_size * 1.1)))

            logo_x = delimiter_x - common_spacing - logo_size
            logo_y = footer_start_y + elem_margin
            placements.append((right_logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS),
                               logo_x, logo_y))
            boxes.append((logo_x, logo_y, logo_size, logo_size))

        return {"canvas_height": canvas_height, "placements": placements, "boxes": boxes}

    def _plan_single_col(self, slots, left_logo, right_logo, left_margin, right_margin,
                         top_margin, img_height, bottom_margin, middle_spacing,
                         common_spacing, canvas_width, delimiter_color="black",
                         align_spacing=None, right_inline=False, right_logo_ratio=1.0,
                         right_gap=0):
        """单列降级排布。

        当两列排不下（窄画布 / 超长 EXIF）时接管。装裱排版改动同步支持
        `edge_align`（左边界对齐照片左边界）与 `right_logo_ratio`
        （Logo 尺寸锚定主标题高度）—— **否则竖构图会退回旧外观，功能半残**。

        但注意：单列模式下左列四行已经是**堆叠**关系，没有「某一行与 Logo 同行」
        这个概念，所以 `right_inline` 在这里只体现为「Logo 右对齐 + 按比例缩放」，
        不强行拼成单行（拼出来会跟堆叠的文字打架）。
        `right_gap` 同样只被接受、不被使用——调用处用 `**geometry` 统一展开，
        这里保留形参是为了签名兼容（否则会抛 unexpected keyword argument）。
        """
        if align_spacing is None:
            align_spacing = common_spacing

        lines = [slots[key] for key in ("left_top", "left_bottom", "right_top", "right_bottom")]
        lines = [line for line in lines if line is not None and line.width > 0]

        footer_start_y = top_margin + img_height
        if not lines:
            return {"canvas_height": footer_start_y + bottom_margin,
                    "placements": [], "boxes": [], "fits": True}

        vpad = max(middle_spacing, int(bottom_margin * 0.10))
        text_height = sum(self._h(line) for line in lines) + middle_spacing * (len(lines) - 1)
        footer_height = max(bottom_margin, text_height + 2 * vpad)
        canvas_height = footer_start_y + footer_height

        left_logo_width = footer_height if left_logo else 0
        text_x = left_margin + left_logo_width + align_spacing

        logo_size = 0
        if right_logo:
            if right_inline:
                # 与两列路径同样的锚定规则：Logo 边长 = 主标题高度 × 比例
                title_h = self._h(slots.get("left_top")) or self._h(slots.get("right_top"))
                logo_size = max(1, int(title_h * right_logo_ratio))
            else:
                logo_size = max(1, min(footer_height // 2, int(footer_height * 0.45)))

        available = canvas_width - text_x - right_margin - (logo_size + common_spacing if logo_size else 0)
        available = max(available, 1)
        fits = max(self._w(line) for line in lines) <= available

        placements, boxes = [], []
        if left_logo:
            placements.append((left_logo.resize((left_logo_width, left_logo_width),
                                                Image.Resampling.LANCZOS),
                               left_margin, footer_start_y))

        y = footer_start_y + int((footer_height - text_height) / 2)
        for line in lines:
            element = line
            if self._w(line) > available:            # 单行仍然放不下才裁切
                element = line.crop((0, 0, available, line.height))
            placements.append((element, text_x, y))
            boxes.append((text_x, y, self._w(element), self._h(element)))
            y += self._h(line) + middle_spacing

        if right_logo:
            logo_y = footer_start_y + int((footer_height - logo_size) / 2)
            logo_x = canvas_width - right_margin - logo_size
            placements.append((right_logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS),
                               logo_x, logo_y))
            boxes.append((logo_x, logo_y, logo_size, logo_size))

        return {"canvas_height": canvas_height, "placements": placements,
                "boxes": boxes, "fits": fits}

    def name(self) -> str:
        return "watermark"


# ---------------------------------------------------------------- 展签排版辅助

def _load_sized_font(path, size: int):
    """按**真实字号（em 像素）**加载字体。

    注意别和 `rich_text` 的 `height` 混用：那个是「渲染出来的 ink 高度」，
    与 PIL 的 font size 差一个 cap-height 系数（Roboto ≈ 0.71）。
    把 font size 当 height 填进去，字号会大出约 40%。
    """
    candidates = []
    if path:
        p = Path(str(path))
        candidates.append(p if p.is_absolute() else fonts_dir / p)
    candidates += [fonts_dir / "Roboto-Regular.ttf", fonts_dir / "AlibabaPuHuiTi-2-45-Light.otf",
                   "arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]
    for candidate in candidates:
        try:
            if isinstance(candidate, Path) and not candidate.exists():
                continue
            return ImageFont.truetype(str(candidate), int(size))
        except OSError:
            continue
    logger.warning(f"gallery_caption: 字体加载失败 {path!r}，回退默认位图字体")
    return ImageFont.load_default()


def _text_image(text: str, font, fill):
    """把文字渲染成一张**紧贴 ink** 的 RGBA 小图（左上角就是 ink 左上角）。

    ⚠️ 关键点：`ImageDraw.textbbox()` 返回的是**度量盒**，不是 ink 盒。
    实测 Roboto-Bold 178px 的 `"NIKON"`，`textbbox()` 给的是 `(0, 38, 540, 168)`，
    而真正的墨迹只占第 11~528 列 —— `left` 是排版原点（含左侧 side bearing），
    `right` 一直算到 advance 宽度。如果直接按这个盒子开图层再贴到画布上，
    文字会**比指定坐标右移 10 来像素**，右端也多出一段透明空白。
    所以画完必须再 `crop(getbbox())` 裁掉透明边，才是真的"贴紧 ink"。

    这样一来：
      · 图层 x=0 就是可见墨迹的左边缘 → 左对齐照片边界才成立；
      · `layer.width` 就是真实字宽 → 品牌与型号之间的间距才是视觉间距。
    文字为空时返回 None，由调用方按宽度 0 处理。
    """
    if not text:
        return None
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), text, font=font)
    layer = Image.new("RGBA", (max(1, box[2] - box[0]), max(1, box[3] - box[1])),
                      (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((-box[0], -box[1]), text, font=font, fill=fill)
    ink = layer.getbbox()
    return layer.crop(ink) if ink else None


def _contain_square(image: Image.Image, box: int) -> Image.Image:
    """等比缩放使图像完整放进 box×box 的正方形容器，**绝不拉伸变形**。

    非正方形的 Logo 资产缩完会留出空白，由调用方在容器内居中。
    """
    scale = min(box / image.width, box / image.height)
    width = max(1, int(round(image.width * scale)))
    height = max(1, int(round(image.height * scale)))
    if (width, height) == (image.width, image.height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


_TRIM_WHITE_CUT = 250       # 三通道里最暗的一档 ≥ 此值 -> 当作白底
_TRIM_ALPHA_CUT = 8         # 不透明度 ≤ 此值 -> 当作完全透明


def _trim_margins(image: Image.Image, white_cut: int = _TRIM_WHITE_CUT,
                  pad: int = 1) -> Image.Image:
    """裁掉 Logo 资产四周的留白（**纯白**或**透明**都算白）。

    为什么必须裁：内置资产普遍不是满幅的。实测 `nikon.png` 内容占满 100%，
    而 `canon.png` 右侧留白 8.4%、`hasselblad.png` 15.4%、`sony.png` 6.1%。
    不裁的话，「正方形容器 + 等比居中 + 居中贴放」出来的**可见尺寸**和
    **贴边位置**各家都不同 —— 实测可见右边缘内缩 0~28px，同一条产线里
    不同品牌的照片并排看就不齐。

    判据是「三通道最小值 < `white_cut` 且不透明」，不是只看 alpha：
    内置资产是**不透明**的，白边是画在像素里的，alpha 全 255，只看 alpha
    一点都裁不掉（这正是上一版漏掉这条的原因）。

    若整张资产找不到任何非白内容（例如纯白占位图），退回按 alpha 求框；
    仍然找不到就不裁 —— 宁可不动，也不能把 Logo 裁没。
    """
    rgba = image.convert("RGBA")
    arr = np.asarray(rgba)
    if arr.ndim != 3 or arr.shape[2] != 4 or arr.size == 0:
        return rgba
    mask = (arr[:, :, :3].min(axis=2) < white_cut) & (arr[:, :, 3] > _TRIM_ALPHA_CUT)
    if not mask.any():
        mask = arr[:, :, 3] > _TRIM_ALPHA_CUT
    if not mask.any():
        return rgba
    ys, xs = np.where(mask)
    box = (max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
           min(rgba.width, int(xs.max()) + 1 + pad),
           min(rgba.height, int(ys.max()) + 1 + pad))
    if box == (0, 0, rgba.width, rgba.height):
        return rgba
    return rgba.crop(box)


class GalleryCaptionFilter(FilterProcessor):
    """摄影作品展签（Gallery Caption）—— **固定比例**的展签版式。

    与 `watermark` 的分工：

    * `watermark` 是**自适应**布局（先测量、再走降级梯子），适合长度不可控的 EXIF；
    * 这里是**固定比例**版式，所有尺寸都是「照片渲染宽度 W」的百分比。因此
      横构图 / 竖构图 / 任意分辨率输入下，版式比例完全一致，不会因画幅变化走样。

    为什么一律以 W 为基准、而不是最终画布宽度？
      画布宽度 `W + 2×pad_x` 本身就含 W，用它当基准会变成循环放大；
      而且一改外框留白，字号 / Logo / 间距会跟着一起变，比例失控。

    版面（规范给定，全部以 W 为基准）：

    ==========  ==========================================================
    外框        ``pad_x = 2.5%W``、``pad_top = 2.5%W``
    底栏        ``footer_height = 11%W``
    标题        品牌(粗) + 型号(常规)，同字号 ``3.2%W``，间距 ``1.4%W``
    参数        字号 ``1.7%W``，``#747474``
    右侧        日期(右对齐) + 官方 Logo，整组垂直居中于底栏，间距 ``2.0%W``
    ==========  ==========================================================

    Logo 单独说三条（v3.5.0 修）：

    1. **先裁白边**。内置资产普遍带留白（`canon.png` 右留白 8.4%、
       `hasselblad.png` 15.4%），不裁则各家可见尺寸与贴边位置都不同。
    2. **容器边长按形态分档**。徽标类（近方形）``5%W``、字标类（细长）
       ``9%W``，由 ``logo_aspect_wide`` 判分界。
    3. **贴边锚在可见墨迹的右边缘**，不是容器右边缘 —— 保证各品牌
       Logo 的可见右边界严格等于照片右边界。

    照片本身**不做任何处理**：不圆角、不投影、不描边、不裁切、不调色、不加蒙版。
    底栏**不画任何分割线 / 色块 / 边框**，只用留白分组。
    """

    def process(self, ctx: PipelineContext):
        img = ctx.get_buffer()[0]
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        photo_w, photo_h = img.width, img.height

        # ---- 画布：所有尺寸以照片宽度 W 为基准 --------------------------
        pad_x = max(0, ctx.getint("pad_x", int(photo_w * 0.025)))
        pad_top = max(0, ctx.getint("pad_top", pad_x))
        footer_h = max(1, ctx.getint("footer_height", int(photo_w * 0.11)))

        canvas_w = photo_w + 2 * pad_x
        canvas_h = pad_top + photo_h + footer_h
        canvas = Image.new("RGBA", (canvas_w, canvas_h),
                           _parse_color(ctx.get("background", "#FFFFFF")))
        # 照片原样贴入：不加圆角、不投影、不描边，也不做任何裁切/调色
        canvas.paste(img, (pad_x, pad_top), mask=img)

        footer_top = pad_top + photo_h
        photo_left = pad_x
        photo_right = pad_x + photo_w
        footer_center_y = footer_top + footer_h / 2.0

        # ---- 左侧第一行：品牌(粗) + 型号(常规) --------------------------
        title_size = max(1, ctx.getint("title_font_size", int(photo_w * 0.032)))
        title_y = footer_top + int(round(footer_h * ctx.getfloat("title_y_ratio", 0.20)))
        title_gap = ctx.getint("title_gap", int(photo_w * 0.014))

        brand_img = _text_image(str(ctx.get("brand_text") or "").strip(),
                                _load_sized_font(ctx.get("brand_font"), title_size),
                                _parse_color(ctx.get("brand_color", "#111111")))
        model_img = _text_image(str(ctx.get("model_text") or "").strip(),
                                _load_sized_font(ctx.get("model_font"), title_size),
                                _parse_color(ctx.get("model_color", "#222222")))

        # 两个文字对象共用一条行框、各自在框内垂直居中 ——
        # 「同字号 / 同基线 / 同垂直中心」三个要求同时成立；直接对齐 top 会因为
        # 字重与字形不同（ink 高度差 1~2px）而让型号看起来被抬起或下沉。
        brand_w = brand_img.width if brand_img else 0
        brand_h = brand_img.height if brand_img else 0
        model_w = model_img.width if model_img else 0
        model_h = model_img.height if model_img else 0
        line_h = max(brand_h, model_h, 1)
        line_center = title_y + line_h / 2.0

        if brand_img:
            canvas.paste(brand_img,
                         (photo_left, int(round(line_center - brand_h / 2.0))),
                         mask=brand_img)
        # 型号 x 由**品牌的真实 ink 宽度**决定，不用空格凑间距
        model_x = photo_left + brand_w + title_gap
        if model_img:
            canvas.paste(model_img,
                         (model_x, int(round(line_center - model_h / 2.0))),
                         mask=model_img)
        title_right = model_x + model_w if model_img else photo_left + brand_w

        # ---- 左侧第二行：EXIF 参数（左边缘与品牌、照片左边严格对齐） ------
        meta_size = max(1, ctx.getint("meta_font_size", int(photo_w * 0.017)))
        meta_y = footer_top + int(round(footer_h * ctx.getfloat("meta_y_ratio", 0.62)))
        meta_img = _text_image(str(ctx.get("meta_text") or "").strip(),
                               _load_sized_font(ctx.get("meta_font"), meta_size),
                               _parse_color(ctx.get("meta_color", "#747474")))
        if meta_img:
            canvas.paste(meta_img, (photo_left, meta_y), mask=meta_img)
        meta_right = photo_left + (meta_img.width if meta_img else 0)

        # ---- 右侧：官方 Logo（可见右边缘贴照片右边界、垂直居中于底栏） ------
        # 容器边长按**资产形态**分两档：徽标类（近方形）用 `logo_size`，
        # 字标类（细长文字）用 `logo_size_wide` —— 同一个方框里，字标的可见
        # 高度只有徽标的十几分之一（实测尼康 200×200 vs 索尼 180×35），
        # 视觉重量完全不是一回事，同一条产线混品牌并排就会显得不齐。
        logo_size = max(1, ctx.getint("logo_size", int(photo_w * 0.050)))
        logo_size_wide = max(1, ctx.getint("logo_size_wide", int(photo_w * 0.090)))
        logo_aspect_wide = ctx.getfloat("logo_aspect_wide", 2.5)
        date_gap = ctx.getint("date_logo_gap", int(photo_w * 0.020))
        logo_left = photo_right
        logo_placed = False
        logo_value = ctx.get("logo")
        # 模板里必须写 `{{ (auto_logo() or '')|replace('\\','/') }}`：
        # `None` 经 Jinja 的 `replace` 会被 `soft_str()` 成字符串 `'None'`
        # （**真值**），判空失效，底下 `Image.open('None')` 每张图刷一条
        # WARNING。字符串判空兜一层，防旧模板。
        if logo_value and str(logo_value).strip().lower() not in ("", "none"):
            try:
                with Image.open(str(logo_value)) as handle:
                    logo = _trim_margins(handle.convert("RGBA"))
                aspect = logo.width / logo.height if logo.height else 1.0
                box = logo_size_wide if aspect >= logo_aspect_wide else logo_size
                fitted = _contain_square(logo, box)
                # 贴边锚在**可见墨迹的右边缘**，不是容器的右边缘：
                # 资产裁完仍可能不是正方形，锚容器会让可见右边界内缩。
                logo_left = photo_right - fitted.width
                canvas.paste(fitted,
                             (logo_left, int(round(footer_center_y - fitted.height / 2.0))),
                             mask=fitted)
                logo_placed = True
            except (OSError, ValueError) as exc:
                logger.warning(f"gallery_caption: Logo 打不开 {logo_value!r}: {exc}")

        # ---- 日期：右对齐到 Logo 左侧，按 ink bbox 垂直居中 ---------------
        # 注意右边界锚在 **Logo 左边缘**（logo_left），不是照片右边界 ——
        # 写成 photo_right - date_gap 会让日期直接压到 Logo 上。
        date_size = max(1, ctx.getint("date_font_size", meta_size))
        date_img = _text_image(str(ctx.get("date_text") or "").strip(),
                               _load_sized_font(ctx.get("date_font"), date_size),
                               _parse_color(ctx.get("date_color", "#858585")))
        date_right = (logo_left - date_gap) if logo_placed else photo_right
        if date_img:
            # 用 ink bbox 的**垂直中心**对齐底栏中线，而不是让 date_y = logo_y ——
            # 文字坐标自带 ascender/descender 余量，直接照抄 logo 的 y 会偏上
            date_x = int(round(date_right - date_img.width))
            date_y = int(round(footer_center_y - date_img.height / 2.0))
            canvas.paste(date_img, (date_x, date_y), mask=date_img)
            date_left = date_x
        else:
            date_left = date_right

        # ---- 碰撞兜底：固定比例版式没有降级梯子，挤到一起时必须能察觉 ------
        left_right = max(title_right, meta_right)
        if left_right + 8 > date_left:
            logger.warning(
                "gallery_caption: 底栏内容过宽可能重叠"
                f"（左侧到 {left_right}px，右侧从 {date_left}px 起）。"
                "考虑精简 EXIF 或减小字号。")

        shift_photo_rect(ctx, pad_x, pad_top)
        ctx.update_buffer([canvas]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "gallery_caption"


class WatermarkWithTimestampFilter(FilterProcessor):
    """在图片右下角叠加时间戳。"""

    def process(self, ctx: PipelineContext):
        img = ctx.get_buffer()[0]
        if "height" not in ctx:
            ctx.set("height", int(img.height * 0.02))
        processor = get_processor("multi_rich_text")
        if not processor:
            raise RuntimeError("multi_rich_text processor not found")
        processor().process(ctx)
        text = ctx.get_buffer()[0]
        text_x = int(img.width * 0.93) - text.width
        text_y = int(img.height * 0.95)
        img.paste(text, (text_x, text_y), mask=text)
        ctx.update_buffer([img]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "watermark_with_timestamp"


class RoundedCornerFilter(FilterProcessor):
    """圆角（border-radius，单位 px）。"""

    def process(self, ctx: PipelineContext):
        radius = ctx.getint("border_radius", 10)
        buffer = []
        for img in ctx.get_buffer():
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            width, height = img.size
            mask = Image.new("L", (width, height), 0)
            ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (width, height)], radius=radius, fill=255)
            output = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            output.paste(img, (0, 0))
            output.putalpha(mask)
            buffer.append(output)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "rounded_corner"


class OutlineFilter(FilterProcessor):
    """沿图片边缘描一圈极细的线。

    解决的具体问题：浅色照片放进白色画框时，照片边缘和留白都是亮的、会糊在
    一起，整张图看着「软」。描一条 1px、透明度约 10% 的细边把边缘收住，
    观感接近专业器材水印的做法（也是标准水印2 没有的细节）。

    放在 `rounded_corner` 之后、`shadow` 之前：圆角先切好，细边沿着同一条
    圆角路径描，两者半径一致才不会露角。
    """

    def process(self, ctx: PipelineContext):
        line_width = max(1, ctx.getint("outline_width", 1))
        color = ctx.getcolor("outline_color", (0, 0, 0, 26))
        radius = ctx.getint("border_radius", 0)

        buffer = []
        for img in ctx.get_buffer():
            base = img.convert("RGBA")
            layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
            # 画到 w-1 / h-1：线条完全落在画布内，不会被裁掉半条
            box = (0, 0, max(0, base.width - 1), max(0, base.height - 1))
            draw = ImageDraw.Draw(layer)
            if radius > 0:
                draw.rounded_rectangle(box, radius=int(radius), outline=color, width=line_width)
            else:
                draw.rectangle(box, outline=color, width=line_width)
            buffer.append(Image.alpha_composite(base, layer))
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "outline"


class ShadowFilter(FilterProcessor):
    """投影（带 alpha 幂函数衰减，边缘干净）。"""
    def process(self, ctx: PipelineContext):
        shadow_color = ctx.getcolor("shadow_color", (0, 0, 0, 180))
        shadow_radius = ctx.getint("shadow_radius", 30)
        falloff = ctx.getfloat("falloff", 1.5)
        buffer = []
        for img in ctx.get_buffer():
            original = img.convert("RGBA") if img.mode != "RGBA" else img
            w, h = original.size
            if shadow_radius <= 0:
                buffer.append(img)
                continue
            padding = int(shadow_radius * 2)
            background = Image.new("RGBA", (w + padding * 2, h + padding * 2), (0, 0, 0, 0))
            shadow_layer = Image.new("RGBA", (w, h), shadow_color)
            shadow_layer.putalpha(original.getchannel("A"))
            background.paste(shadow_layer, (padding, padding))
            blurred = background.filter(ImageFilter.GaussianBlur(shadow_radius))
            blurred = self._apply_alpha_falloff(blurred, falloff)
            blurred.paste(original, (padding, padding), mask=original)
            buffer.append(blurred)
            shift_photo_rect(ctx, padding, padding)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    @staticmethod
    def _apply_alpha_falloff(img: Image.Image, gamma: float) -> Image.Image:
        _, _, _, a = img.split()
        alpha = np.array(a, dtype=np.float32) / 255.0
        alpha = np.power(alpha, gamma)
        alpha[alpha < 0.01] = 0
        img.putalpha(Image.fromarray((alpha * 255).astype(np.uint8), mode="L"))
        return img

    def name(self) -> str:
        return "shadow"


class CropFilter(FilterProcessor):
    """居中裁切到指定宽高，可带偏移。"""

    def process(self, ctx: PipelineContext):
        width = ctx.getint("width", 0)
        height = ctx.getint("height", 0)
        try:
            offset = json.loads(ctx.get("offset", "[]") or "[]")
        except (ValueError, TypeError):
            offset = []

        buffer = []
        for img in ctx.get_buffer():
            target_w = width if width > 0 else img.width
            target_h = height if height > 0 else img.height
            target_w = min(target_w, img.width)
            target_h = min(target_h, img.height)
            left = (img.width - target_w) // 2 + (offset[0] if len(offset) > 0 else 0)
            top = (img.height - target_h) // 2 + (offset[1] if len(offset) > 1 else 0)
            left = max(0, min(left, img.width - target_w))
            top = max(0, min(top, img.height - target_h))
            buffer.append(img.crop((left, top, left + target_w, top + target_h)))
            crop_photo_rect(ctx, left, top, target_w, target_h)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "crop"


# ------------------------------------------------------------------ 新增能力

class OpacityFilter(FilterProcessor):
    """整体不透明度（0~1），常用于压低 Logo / 水印存在感。"""

    def process(self, ctx: PipelineContext):
        opacity = ctx.getfloat("opacity", 1.0)
        buffer = [img.convert("RGBA") if img.mode != "RGBA" else img for img in ctx.get_buffer()]
        if opacity < 1.0:
            buffer = [_apply_opacity(img, opacity) for img in buffer]
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "opacity"


class RotateFilter(FilterProcessor):
    """按角度旋转（expand 保持内容完整），可选填背景色。"""

    def process(self, ctx: PipelineContext):
        angle = ctx.getfloat("angle", 0.0)
        background = ctx.getcolor("background", (0, 0, 0, 0))
        expand = ctx.getbool("expand", True)
        buffer = []
        for img in ctx.get_buffer():
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            buffer.append(img.rotate(angle, resample=Image.Resampling.BICUBIC,
                                     expand=expand, fillcolor=background))
        # 旋转后照片矩形不再是轴对齐的，追踪结果为未知
        clear_photo_rect(ctx)
        ctx.update_buffer(buffer).save_buffer(self.name()).success()

    def name(self) -> str:
        return "rotate"


class TileWatermarkFilter(FilterProcessor):
    """平铺水印：把文字/Logo 按网格铺满整张图，适合防盗图。"""

    def process(self, ctx: PipelineContext):
        img = ctx.get_buffer()[0].convert("RGBA")
        text = ctx.get("text", "")
        angle = ctx.getfloat("angle", -30.0)
        opacity = ctx.getfloat("opacity", 0.18)
        gap_x = ctx.getint("gap_x", int(img.width * 0.28))
        gap_y = ctx.getint("gap_y", int(img.height * 0.28))
        color = ctx.get("color", "white")
        height = ctx.getint("height", max(16, int(img.height * 0.035)))
        font_path = ctx.get("font_path")

        # 生成一枚水印单元
        unit_ctx = PipelineContext({
            "processor_name": "rich_text",
            "text": text,
            "color": color,
            "height": height,
            "font_path": font_path,
            "trim": True,
            "save_buffer": False,
        })
        from processor.generators import RichTextGenerator
        RichTextGenerator().process(unit_ctx)
        unit = unit_ctx.get_buffer()[0]
        unit = unit.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))
        unit = _apply_opacity(unit, opacity)

        step_x = max(unit.width + gap_x, 1)
        step_y = max(unit.height + gap_y, 1)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        row = 0
        y = -unit.height
        while y < img.height + unit.height:
            offset = (unit.width // 2) if row % 2 else 0
            x = -unit.width - offset
            while x < img.width + unit.width:
                overlay.paste(unit, (int(x), int(y)), mask=unit)
                x += step_x
            y += step_y
            row += 1

        ctx.update_buffer([Image.alpha_composite(img, overlay)]).save_buffer(self.name()).success()

    def name(self) -> str:
        return "tile_watermark"


class SimpleTextWatermarkFilter(FilterProcessor):
    """自定义文字水印：位置 / 字号 / 颜色 / 不透明度 / 旋转 / 描边。

    面向不想写模板的普通用户，由 GUI 直接拼装。
    """

    POSITIONS = {"top_left", "top_center", "top_right", "middle_left", "center",
                 "middle_right", "bottom_left", "bottom_center", "bottom_right"}

    def process(self, ctx: PipelineContext):
        img = ctx.get_buffer()[0].convert("RGBA")
        text = ctx.get("text", "")
        if not text:
            ctx.update_buffer([img]).success()
            return

        position = ctx.get("position", "bottom_right")
        margin = ctx.getint("margin", int(min(img.width, img.height) * 0.04))
        opacity = ctx.getfloat("opacity", 0.85)
        angle = ctx.getfloat("angle", 0.0)
        color = ctx.get("color", "white")
        stroke_color = ctx.get("stroke_color")
        stroke_width = ctx.getint("stroke_width", 0)
        height = ctx.getint("height", max(14, int(img.height * 0.035)))
        font_path = ctx.get("font_path")

        unit_ctx = PipelineContext({
            "processor_name": "rich_text",
            "text": text,
            "color": color,
            "height": height,
            "font_path": font_path,
            "trim": True,
            "save_buffer": False,
        })
        from processor.generators import RichTextGenerator
        RichTextGenerator().process(unit_ctx)
        unit = unit_ctx.get_buffer()[0]

        if stroke_width > 0 and stroke_color:
            from PIL import ImageFont
            from processor.generators import load_font
            font = load_font(font_path)
            bbox = font.getbbox(text)
            canvas = Image.new("RGBA", (bbox[2] - bbox[0] + stroke_width * 2,
                                        bbox[3] - bbox[1] + stroke_width * 2), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            draw.text((stroke_width - bbox[0], stroke_width - bbox[1]), text, font=font,
                      fill=_parse_color(color), stroke_width=stroke_width,
                      stroke_fill=_parse_color(stroke_color))
            from processor.filters import TrimFilter
            trim_ctx = PipelineContext({"buffer": [canvas], "save_buffer": False})
            TrimFilter().process(trim_ctx)
            canvas = trim_ctx.get_buffer()[0]
            resize_ctx = PipelineContext({"buffer": [canvas], "height": height, "save_buffer": False})
            ResizeFilter().process(resize_ctx)
            unit = resize_ctx.get_buffer()[0]

        if angle:
            unit = unit.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))
        unit = _apply_opacity(unit, opacity)

        x, y = self._resolve_position(position, img.size, unit.size, margin)
        img.paste(unit, (int(x), int(y)), mask=unit)
        ctx.update_buffer([img]).save_buffer(self.name()).success()

    @staticmethod
    def _resolve_position(position, canvas_size, unit_size, margin):
        cw, ch = canvas_size
        uw, uh = unit_size
        if "left" in position:
            x = margin
        elif "right" in position:
            x = cw - uw - margin
        else:
            x = (cw - uw) // 2
        if "top" in position:
            y = margin
        elif "bottom" in position:
            y = ch - uh - margin
        else:
            y = (ch - uh) // 2
        return x, y

    def name(self) -> str:
        return "simple_text_watermark"


def _apply_opacity(img: Image.Image, opacity: float) -> Image.Image:
    """按比例缩放 alpha 通道。"""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda p: int(p * opacity))
    return Image.merge("RGBA", (r, g, b, a))
