"""多进程批量处理的「工作单元」。

为什么单独一个模块？
--------------------
Windows 下 `multiprocessing` 用 **spawn** 启动子进程：子进程会把主模块
重新 import 一遍。如果把处理函数放在 `app.py` 里，每个子进程都会连带把
Flask 应用、配置、日志全部初始化一遍 —— 既慢又容易出怪问题。

所以这里刻意保持「零 Web 依赖」：只导入图像处理链，不碰 Flask。
子进程 import 这个模块时会顺带完成处理器注册（`import processor` 的副作用）。

另外，`process_file()` 必须是**模块级函数**（可被 pickle），
不能是闭包或类方法，否则 spawn 模式下会直接报 PicklingError。
"""
from __future__ import annotations

import os

from PIL import Image

# 触发处理器注册（processor/__init__.py 的副作用）
import processor  # noqa: F401
from core.configs import logos_dir
from core.imaging import apply_aspect_crop, apply_custom_logo, save_output
from core.logger import logger
from core.util import build_context, render_template_json
from processor.core import start_process

# 高像素机身（如 102MP 中画幅）会超过 Pillow 默认的 ~89MP 解压炸弹阈值，
# 导致直接抛 DecompressionBombError。这里放宽到 400MP 并保留告警，
# 既支持大图，又不会把真正的异常图片放进来。
_SAFE_MAX_PIXELS = 400_000_000
try:
    Image.MAX_IMAGE_PIXELS = _SAFE_MAX_PIXELS
except Exception:  # noqa: BLE001
    pass


def _build_simple_pipeline(params: dict, ctx: dict):
    """根据简单水印参数构造处理器管道（与 app 层保持同一套语义）。"""
    exif = ctx.get("exif") or {}
    raw_date = exif.get("DateTimeOriginal") or exif.get("CreateDate") or ""
    date_str = raw_date[:10].replace(":", "-") if raw_date else ""
    text = (params.get("text", "") or "")
    text = (text.replace("{name}", ctx.get("filename", ""))
                .replace("{date}", date_str)
                .replace("{model}", exif.get("CameraModelName", "")))

    if params.get("tile"):
        return [{
            "processor_name": "tile_watermark",
            "text": text,
            "angle": params.get("angle", -30),
            "opacity": params.get("opacity", 0.18),
            "color": params.get("color", "white"),
            "font_path": params.get("font_path"),
        }]
    return [{
        "processor_name": "simple_text_watermark",
        "text": text,
        "position": params.get("position", "bottom_right"),
        "color": params.get("color", "white"),
        "opacity": params.get("opacity", 0.85),
        "angle": params.get("angle", 0),
        "height": params.get("height", 0),
        "stroke_color": params.get("stroke_color"),
        "stroke_width": params.get("stroke_width", 0),
        "font_path": params.get("font_path"),
    }]


def resolve_pipeline(options: dict, input_path: str):
    """把任务选项解析成 (处理器管道, 渲染上下文)。

    与 app 层的 `_resolve_pipeline` 行为一致，但只依赖 core / processor。
    """
    ctx = build_context(input_path)
    if options.get("mode") == "simple":
        return _build_simple_pipeline(options.get("simple") or {}, ctx), ctx
    template_name = options.get("template_name") or "标准水印2"
    return render_template_json(template_name, ctx), ctx


def _apply_logo(img, logo: dict):
    """叠加用户自定义 Logo（与模板里的 auto_logo 互不干扰）。"""
    name = (logo or {}).get("logo_name") or ""
    if not name:
        return img
    path = logos_dir / os.path.basename(str(name))
    if not path.exists():
        return img
    return apply_custom_logo(
        img, str(path),
        scale_pct=logo.get("logo_scale", 100),
        opacity_pct=logo.get("logo_opacity", 100),
        corner=logo.get("logo_corner", "bottom_right"),
    )


def process_file(task: dict) -> dict:
    """处理单张照片（在子进程 / 线程中执行）。

    task 字段：
        input_path, output_path  —— 绝对路径，由父进程算好
        options                  —— 本次任务的完整参数快照
        exif                     —— 父进程已读取的 EXIF（省掉一次 exiftool 调用）

    返回：
        {input_path, output_path, status, error}
        status ∈ success | failure | skipped
    """
    input_path = task.get("input_path")
    output_path = task.get("output_path")
    options = task.get("options") or {}
    exif = task.get("exif")

    name = os.path.basename(str(input_path))
    try:
        if not os.path.exists(str(input_path)):
            return {"input_path": input_path, "output_path": output_path,
                    "status": "failure", "error": "文件不存在"}

        # 覆盖策略由父进程判定（避免子进程之间争抢同一个输出文件）
        if task.get("skip_existing"):
            return {"input_path": input_path, "output_path": output_path,
                    "status": "skipped", "error": "目标文件已存在（可勾选“覆盖已存在的文件”）"}

        pipeline, ctx = resolve_pipeline(options, str(input_path))
        if exif is not None and not ctx.get("exif"):
            ctx["exif"] = exif

        result = start_process(pipeline, input_path=str(input_path),
                               output_path=None, exif=ctx["exif"])
        result = apply_aspect_crop(result, options.get("crop_preset", "none"))
        result = _apply_logo(result, options.get("logo") or {})
        save_output(
            result, output_path, source_path=str(input_path),
            preserve_exif=bool(options.get("preserve_exif", True)),
            quality=int(options.get("quality", 95)),
            subsampling=int(options.get("subsampling", 0)),
            long_edge=int(options.get("long_edge", 0)),
        )
        return {"input_path": input_path, "output_path": output_path,
                "status": "success", "error": None}
    except Exception as e:  # noqa: BLE001
        logger.exception(f"处理失败: {input_path}")
        return {"input_path": input_path, "output_path": output_path,
                "status": "failure", "error": f"{type(e).__name__}: {e}"}


def probe_worker() -> dict:
    """给主进程用的「自检」入口：确认子进程能正常加载处理链。

    除了处理器数量，还回报子进程解析出的 DATA_ROOT 与模板可见性 ——
    多进程最常见的坑就是「子进程把数据目录算成了另一个地方」，
    于是模板读不到、整张照片失败，而串行却完全正常。
    """
    from core import DATA_ROOT, PORTABLE

    info = {"pid": os.getpid(), "data_root": str(DATA_ROOT), "portable": PORTABLE}
    try:
        from core.util import list_templates
        templates = list_templates()
        info["templates"] = len(templates)
    except Exception as e:  # noqa: BLE001
        info["templates"] = 0
        info["template_error"] = str(e)

    try:
        from processor.core import get_all_processors
        info["processors"] = len(get_all_processors())
        info["ok"] = True
    except Exception as e:  # noqa: BLE001
        info["processors"] = 0
        info["ok"] = False
        info["error"] = str(e)
    return info
