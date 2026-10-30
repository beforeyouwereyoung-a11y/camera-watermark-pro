"""本地服务：配置、文件、预览、批量处理（SSE）、模板管理、预设、性能。"""
import hashlib
import io
import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from core import (APP_DISPLAY_NAME, APP_NAME, BUNDLE_ROOT, DATA_ROOT, IS_FROZEN, PORTABLE,
                  ensure_user_data)
from core import imaging
from core import perf
from core.configs import ensure_dirs, load_config, load_project_info, save_config
from core.configs import logos_dir
from core.jobs import manager
from core.logger import init_from_config, logger
from core.presets import delete_preset, get_preset, list_presets, save_preset
from core.util import (EXIFTOOL_AVAILABLE, build_context, convert_heic_to_jpeg, exif_cache_stats,
                       exif_status, get_exif, get_template, get_template_content, list_files,
                       list_fonts, list_logos, list_templates, log_rt, save_template,
                       delete_template, render_template_json, render_pipeline_json,
                       is_same_path, is_subpath,
                       normalize_path, resolve_output_root, set_exif_cache_size)
from processor.core import intersect_rect, scale_rect, start_process
from processor.schema import CATEGORY_META, list_processor_schema

# ---------------------------------------------------------------- 初始化
ensure_user_data()
ensure_dirs()

config = load_config()
project_info = load_project_info()
init_from_config(config)
set_exif_cache_size(config.getint("DEFAULT", "exif_cache_size"))

api = Flask(__name__,
            template_folder=str(BUNDLE_ROOT / "templates"),
            static_folder=str(BUNDLE_ROOT / "static"))
api.config["JSON_AS_ASCII"] = False

# 桌面窗口引用（由 desktop_app 注入，用于原生文件夹选择）
_window = None
# 原生拖拽是否已就绪（由 desktop_app 注册 DOM drop 监听后置位）
_native_drop_ready = False
_preview_cache: dict = {}
_preview_lock = threading.Lock()


def set_window(win):
    global _window
    _window = win


def set_native_drop_ready(flag: bool = True):
    """标记原生拖拽可用（WebView2 需要 pywebview DOM 监听才会回传真实路径）。"""
    global _native_drop_ready
    _native_drop_ready = bool(flag)


def _suffixes() -> set:
    return {s.strip().lower() for s in config.get("DEFAULT", "supported_file_suffixes").split(",") if s.strip()}


# ---------------------------------------------------------------- 页面

@api.route("/")
def index():
    return render_template(
        "index.html",
        title=APP_DISPLAY_NAME,
        version=project_info.get("project", {}).get("version", "1.0.0"),
    )


@api.route("/favicon.ico")
def favicon():
    icon = BUNDLE_ROOT / "assets" / "logo.ico"
    if icon.exists():
        return send_file(str(icon), mimetype="image/x-icon")
    return ("", 204)


# ---------------------------------------------------------------- 元信息

@api.route("/api/v1/meta")
def meta():
    return jsonify({
        "app_name": APP_DISPLAY_NAME,
        "version": project_info.get("project", {}).get("version", "1.0.0"),
        "portable": PORTABLE,
        "frozen": IS_FROZEN,
        "native_drop": _native_drop_ready,
        "data_root": str(DATA_ROOT),
        "templates": list_templates(),
        "fonts": list_fonts(),
        "logos": list_logos(),
        "presets": list_presets(),
        "theme": config.get("DEFAULT", "theme", fallback="light"),
        "perf": perf_settings(),
        "cores": perf.cpu_total(),
        **exif_status(),
    })


def perf_settings() -> dict:
    """当前性能相关设置（供界面显示与「性能自检」）。"""
    return {
        "workers": config.get("DEFAULT", "workers", fallback="auto"),
        "workers_min_batch": config.getint("DEFAULT", "workers_min_batch", fallback=4),
        "exif_cache_size": config.getint("DEFAULT", "exif_cache_size", fallback=2048),
        "preview_max_edge": config.getint("DEFAULT", "preview_max_edge", fallback=1400),
        "cores": perf.cpu_total(),
        "max_workers": perf.max_workers(),
        "onefile": perf.IS_ONEFILE,
        "exif_cache": exif_cache_stats(),
    }


def logo_settings() -> dict:
    return {
        "logo_name": config.get("DEFAULT", "logo_name", fallback=""),
        "logo_scale": config.getint("DEFAULT", "logo_scale", fallback=100),
        "logo_opacity": config.getint("DEFAULT", "logo_opacity", fallback=100),
        "logo_corner": config.get("DEFAULT", "logo_corner", fallback="bottom_right"),
    }


@api.route("/api/v1/config", methods=["GET"])
def get_config():
    template_name = config.get("render", "template_name", fallback="标准水印2")
    try:
        template_content = get_template_content(template_name)
    except FileNotFoundError:
        template_name = "标准水印2"
        template_content = get_template_content(template_name)

    return jsonify({
        "input_folder": config.get("DEFAULT", "input_folder"),
        "output_folder": config.get("DEFAULT", "output_folder"),
        "override_existed": config.getboolean("DEFAULT", "override_existed"),
        "quality": config.getint("DEFAULT", "quality"),
        "subsampling": config.getint("DEFAULT", "subsampling"),
        "output_format": config.get("DEFAULT", "output_format"),
        "long_edge": config.getint("DEFAULT", "long_edge"),
        "name_pattern": config.get("DEFAULT", "name_pattern"),
        "preserve_exif": config.getboolean("DEFAULT", "preserve_exif"),
        "crop_preset": config.get("DEFAULT", "crop_preset"),
        "open_output_when_done": config.getboolean("DEFAULT", "open_output_when_done"),
        "template_name": template_name,
        "template": template_content,
        "templates": list_templates(),
        "theme": config.get("DEFAULT", "theme", fallback="light"),
        **logo_settings(),
        "perf": perf_settings(),
    })


@api.route("/api/v1/config", methods=["POST"])
def update_config():
    try:
        data = request.get_json() or {}
        mapping = {
            "input_folder": ("DEFAULT", str),
            "output_folder": ("DEFAULT", str),
            "override_existed": ("DEFAULT", lambda v: str(bool(v))),
            "quality": ("DEFAULT", lambda v: str(int(v))),
            "subsampling": ("DEFAULT", lambda v: str(int(v))),
            "output_format": ("DEFAULT", str),
            "long_edge": ("DEFAULT", lambda v: str(int(v))),
            "name_pattern": ("DEFAULT", str),
            "preserve_exif": ("DEFAULT", lambda v: str(bool(v))),
            "crop_preset": ("DEFAULT", str),
            "open_output_when_done": ("DEFAULT", lambda v: str(bool(v))),
            "theme": ("DEFAULT", str),
            "logo_name": ("DEFAULT", str),
            "logo_scale": ("DEFAULT", lambda v: str(int(v))),
            "logo_opacity": ("DEFAULT", lambda v: str(int(v))),
            "logo_corner": ("DEFAULT", str),
        }
        for key, (section, caster) in mapping.items():
            if key in data:
                config.set(section, key, caster(data[key]))

        # 性能设置单独处理：workers 允许 "auto"/"serial"/数字
        if "workers" in data:
            config.set("DEFAULT", "workers", str(data["workers"]))
        for key in ("workers_min_batch", "exif_cache_size", "preview_max_edge"):
            if key in data:
                config.set("DEFAULT", key, str(int(data[key])))
        if "exif_cache_size" in data:
            set_exif_cache_size(data["exif_cache_size"])

        if "template_name" in data:
            config.set("render", "template_name", str(data["template_name"]))

        save_config(config)

        if "template" in data and "template_name" in data:
            save_template(str(data["template_name"]), data["template"])

        return jsonify({"message": "配置已保存"})
    except Exception as e:  # noqa: BLE001
        logger.exception("保存配置失败")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------- 文件

@api.route("/api/v1/file/tree")
@log_rt
def file_tree():
    suffixes = _suffixes()
    input_folder = config.get("DEFAULT", "input_folder")
    output_folder = config.get("DEFAULT", "output_folder")

    # 输出目录若在输入目录内部，扫描输入时要跳过它，否则上一次的成片会被
    # 当成新素材再次处理（出现 水印输出/水印输出/xxx.jpg 这种套娃）。
    # 注意：这里要用「生效后的」输出目录 —— 当输入==输出时它会被自动改成
    # <输入>/水印输出，只比较配置里的原值是不够的。
    effective_output, _ = resolve_output_root(input_folder, output_folder)
    exclude = []
    for candidate in (output_folder, effective_output):
        if candidate and is_subpath(candidate, input_folder):
            exclude.append(candidate)

    return jsonify({
        "input_files": [{"children": list_files(input_folder, suffixes, exclude_dirs=exclude),
                         "label": "Root", "value": input_folder, "is_file": False}],
        "output_files": [{"children": list_files(effective_output, suffixes), "label": "Root",
                          "value": effective_output, "is_file": False}],
    })


def _serve_image(path: str, max_edge: int = 0):
    """统一返回图片字节流（HEIC 自动转 JPEG，可选缩放）。"""
    suffix = Path(path).suffix.lower()
    if suffix in {".heic", ".heif"}:
        return send_file(convert_heic_to_jpeg(path), mimetype="image/jpeg")

    if max_edge:
        from PIL import Image
        with Image.open(path) as img:
            if max(img.width, img.height) > max_edge:
                scale = max_edge / max(img.width, img.height)
                img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                                 Image.Resampling.LANCZOS)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            buffer.seek(0)
        return send_file(buffer, mimetype="image/jpeg")

    return send_file(path, as_attachment=False)


@api.route("/api/v1/file")
def get_file():
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "缺少 path 参数"}), 400
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path) or os.path.isdir(abs_path):
        return jsonify({"error": "文件不存在"}), 404
    try:
        max_edge = int(request.args.get("max", 0) or 0)
        response = _serve_image(abs_path, max_edge)
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@api.route("/api/v1/thumb")
def get_thumb():
    """缩略图，用于文件网格。"""
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "缺少 path 参数"}), 400
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return jsonify({"error": "文件不存在"}), 404
    try:
        return _serve_image(abs_path, max_edge=int(request.args.get("size", 320)))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------- 预览

def _substitute_tokens(text: str, ctx: dict) -> str:
    """支持自定义水印文字里的 {name} / {date} 占位符。"""
    if not text:
        return text
    exif = ctx.get("exif") or {}
    raw_date = exif.get("DateTimeOriginal") or exif.get("CreateDate") or ""
    date_str = raw_date[:10].replace(":", "-") if raw_date else ""
    return (text.replace("{name}", ctx.get("filename", ""))
                .replace("{date}", date_str)
                .replace("{model}", exif.get("CameraModelName", "")))


def _build_simple_pipeline(params: dict, ctx: dict):
    """根据简单水印参数构造处理器管道。"""
    text = _substitute_tokens(params.get("text", ""), ctx)
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


def _preview_context(input_path: str):
    """构造「预览专用」的渲染上下文。

    预览的实现是**先把原图降采样、再跑整条处理器链**（为了把一次预览从
    1.5~4 秒压到 100~300ms）。但模板里的 `{{vh(3)}} / {{vw(3)}}` 表达的是
    「图片尺寸的百分比」，如果仍按**原始 EXIF 尺寸**解释，预览里的留白与字号
    就会被放大「原图长边 / 预览长边」倍：

        6000×4000 的照片在 1400px 预览里，百分比参数偏大约 4.3 倍，
        底栏能吃掉半张画面 —— 预览与实际成片完全不是一回事。

    这里把尺寸字段换成预览画布的真实尺寸，其余 EXIF 字段（机型/光圈/日期）照旧，
    预览的构图比例就与最终成片一致。
    """
    ctx = build_context(input_path)
    max_edge = config.getint("DEFAULT", "preview_max_edge", fallback=1400) or 1400
    try:
        width, height = imaging.preview_size(input_path, max_edge)
    except Exception:  # noqa: BLE001
        logger.debug("预览尺寸探测失败，按原始尺寸渲染", exc_info=True)
        return ctx
    # build_context 里的 exif 可能是带缓存的共享字典，必须复制后再改
    exif = dict(ctx.get("exif") or {})
    exif["ImageWidth"] = width
    exif["ImageHeight"] = height
    ctx["exif"] = exif
    return ctx


def _resolve_pipeline(payload: dict, input_path: str):
    """把前端请求解析成 (处理器管道, 渲染上下文)。"""
    ctx = _preview_context(input_path)
    mode = payload.get("mode", "template")
    if mode == "simple":
        return _build_simple_pipeline(payload.get("simple") or {}, ctx), ctx
    template_name = payload.get("template_name") or config.get("render", "template_name")
    return render_template_json(template_name, ctx), ctx


def _resolve_logo_for_request(args) -> dict:
    """预览用的 Logo 参数：请求参数优先，未传则回落到配置。"""
    def _int(name, fallback):
        raw = args.get(name)
        if raw in (None, ""):
            return fallback
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return fallback

    name = args.get("logo_name")
    if name is None:
        name = config.get("DEFAULT", "logo_name", fallback="")
    return {
        "logo_name": name or "",
        "logo_scale": _int("logo_scale", config.getint("DEFAULT", "logo_scale", fallback=100)),
        "logo_opacity": _int("logo_opacity", config.getint("DEFAULT", "logo_opacity", fallback=100)),
        "logo_corner": args.get("logo_corner") or config.get("DEFAULT", "logo_corner",
                                                             fallback="bottom_right"),
    }


def _logo_abs_path(name: str) -> str | None:
    if not name:
        return None
    target = logos_dir / os.path.basename(name)
    return str(target) if target.exists() else None


def _apply_logo(img, logo: dict):
    path = _logo_abs_path(logo.get("logo_name") or "")
    if not path:
        return img
    return imaging.apply_custom_logo(
        img, path,
        scale_pct=logo.get("logo_scale", 100),
        opacity_pct=logo.get("logo_opacity", 100),
        corner=logo.get("logo_corner", "bottom_right"),
    )


def _pick_sample_image() -> str | None:
    """没指定样张时，用输入目录里的第一张图。"""
    tree = list_files(config.get("DEFAULT", "input_folder"), _suffixes())
    candidates = []

    def _collect(nodes):
        for n in nodes:
            if n.get("is_file"):
                candidates.append(n["value"])
            else:
                _collect(n.get("children") or [])

    _collect(tree)
    return candidates[0] if candidates else None


def _render_preview_bundle(abs_path: str, pipeline: list, crop_preset: str, logo: dict,
                           exif: dict = None, max_edge: int = None):
    """渲染预览，返回 (JPEG 字节流, 元信息)，带缓存。

    元信息里的 `photo_rect` 是「原始照片内容在成片里的矩形」。
    界面的「对比」模式要用它把原图对齐到成片的实际位置 —— 因为模板的画框/
    留白/投影会把成片画布撑大，两张图尺寸天生不同，不给出这个矩形就只能
    各自居中缩放，看起来就是一张盖住另一张。
    """
    max_edge = max_edge or config.getint("DEFAULT", "preview_max_edge", fallback=1400) or 1400

    key_src = json.dumps([abs_path, os.path.getmtime(abs_path), pipeline, crop_preset,
                          logo, max_edge], ensure_ascii=False, sort_keys=True)
    cache_key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()
    with _preview_lock:
        if cache_key in _preview_cache:
            return _preview_cache[cache_key]

    if exif is None:
        exif = get_exif(abs_path)

    # 关键性能优化：先把原图降到预览尺寸，再跑处理器链。
    # 原实现按原图全分辨率渲染完再缩小，一张 6000×4000 要 1.5~4 秒。
    # 注意：Jinja 渲染（{{vh(3)}} 等百分比参数）必须已经按预览尺寸解析过，
    # 见 `_preview_context()`，这里的 exif 由调用方传入。
    source = imaging.load_for_preview(abs_path, max_edge=max_edge)
    result, traced = start_process(pipeline, input_path=abs_path, output_path=None,
                                   exif=exif, initial_buffer=[source], return_meta=True)
    rect = traced.get("photo_rect")

    # 画幅裁切会改变画布，矩形要跟着裁
    box = imaging.aspect_crop_box(result.width, result.height, crop_preset)
    if box:
        left, top, right, bottom = box
        result = result.crop(box)
        rect = intersect_rect(rect, left, top, right - left, bottom - top)

    result = _apply_logo(result, logo)

    # 编码前还会按 max_edge 降采样，矩形同样要缩放，才能和前端拿到的图片对齐
    scale = min(1.0, max_edge / max(result.width, result.height)) if max(result.size) else 1.0
    meta = {
        "width": max(1, round(result.width * scale)),
        "height": max(1, round(result.height * scale)),
        "photo_rect": list(scale_rect(rect, scale)) if rect else None,
    }
    data = imaging.render_preview_bytes(result, max_edge=max_edge, quality=88)

    with _preview_lock:
        _preview_cache[cache_key] = (data, meta)
        if len(_preview_cache) > 60:
            _preview_cache.clear()
    return data, meta


def _render_preview(abs_path: str, pipeline: list, crop_preset: str, logo: dict,
                    exif: dict = None, max_edge: int = None):
    """渲染预览并返回 JPEG 字节流（带缓存）。失败时抛异常，由调用方转成 JSON 错误。"""
    return _render_preview_bundle(abs_path, pipeline, crop_preset, logo, exif, max_edge)[0]


@api.route("/api/v1/preview/meta")
def preview_meta():
    """返回预览图的几何信息：画布尺寸 + 原始照片在成片中的矩形。

    与 /api/v1/preview 共用同一份渲染缓存，所以紧接着调用几乎不额外耗时。
    """
    payload = {
        "mode": request.args.get("mode", "template"),
        "template_name": request.args.get("template_name"),
        "crop_preset": request.args.get("crop_preset", "none"),
        "simple": {
            "text": request.args.get("text", ""),
            "position": request.args.get("position", "bottom_right"),
            "color": request.args.get("color", "white"),
            "opacity": float(request.args.get("opacity", 0.85) or 0.85),
            "angle": float(request.args.get("angle", 0) or 0),
            "height": int(request.args.get("height", 0) or 0),
            "stroke_color": request.args.get("stroke_color") or None,
            "stroke_width": int(request.args.get("stroke_width", 0) or 0),
            "font_path": request.args.get("font_path") or None,
            "tile": request.args.get("tile", "false").lower() == "true",
        },
    }
    path = request.args.get("path") or _pick_sample_image()
    if not path:
        return jsonify({"error": "请先选择一张图片或设置输入目录"}), 400
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return jsonify({"error": "文件不存在"}), 404

    try:
        pipeline, ctx = _resolve_pipeline(payload, abs_path)
        _, meta = _render_preview_bundle(
            abs_path, pipeline,
            crop_preset=payload.get("crop_preset", "none"),
            logo=_resolve_logo_for_request(request.args),
            exif=ctx.get("exif"),
        )
        return jsonify(meta)
    except Exception as e:  # noqa: BLE001
        logger.exception("预览几何信息计算失败")
        return jsonify({"error": str(e)}), 500


@api.route("/api/v1/preview")
def preview():
    """实时渲染预览：返回带水印效果的 JPEG。"""
    payload = {
        "mode": request.args.get("mode", "template"),
        "template_name": request.args.get("template_name"),
        "crop_preset": request.args.get("crop_preset", "none"),
        "simple": {
            "text": request.args.get("text", ""),
            "position": request.args.get("position", "bottom_right"),
            "color": request.args.get("color", "white"),
            "opacity": float(request.args.get("opacity", 0.85) or 0.85),
            "angle": float(request.args.get("angle", 0) or 0),
            "height": int(request.args.get("height", 0) or 0),
            "stroke_color": request.args.get("stroke_color") or None,
            "stroke_width": int(request.args.get("stroke_width", 0) or 0),
            "font_path": request.args.get("font_path") or None,
            "tile": request.args.get("tile", "false").lower() == "true",
        },
    }

    path = request.args.get("path") or _pick_sample_image()
    if not path:
        return jsonify({"error": "请先选择一张图片或设置输入目录"}), 400

    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return jsonify({"error": "文件不存在"}), 404

    try:
        pipeline, ctx = _resolve_pipeline(payload, abs_path)
        data = _render_preview(
            abs_path, pipeline,
            crop_preset=payload.get("crop_preset", "none"),
            logo=_resolve_logo_for_request(request.args),
            exif=ctx.get("exif"),
        )
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as e:  # noqa: BLE001
        logger.exception("预览渲染失败")
        return jsonify({"error": str(e)}), 500


@api.route("/api/v1/preview/pipeline", methods=["POST"])
def preview_pipeline():
    """用「尚未保存的处理器管道」直接渲染预览（可视化编辑器专用）。

    编辑器里的改动是内存态，不能先落盘成模板再预览（会污染用户的模板列表），
    所以这里单独收一个 JSON body。
    """
    data = request.get_json() or {}
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, list) or not pipeline:
        return jsonify({"error": "处理管道不能为空"}), 400

    path = data.get("path") or _pick_sample_image()
    if not path:
        return jsonify({"error": "请先选择一张样张或设置输入目录"}), 400
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return jsonify({"error": "样张不存在"}), 404

    logo = data.get("logo") or logo_settings()
    try:
        # 编辑器里的管道是内存态，必须在这里补上 Jinja 渲染，
        # 否则 {{auto_logo()}} / {{vh(3)}} 这类表达式会被当成字面量传给处理器。
        # 用预览专用上下文，保证 {{vh()}} 按预览画布尺寸解析（与批量预览一致）。
        ctx = _preview_context(abs_path)
        rendered_pipeline = render_pipeline_json(pipeline, ctx)
        rendered = _render_preview(
            abs_path, rendered_pipeline,
            crop_preset=data.get("crop_preset", "none"),
            logo=logo,
            exif=ctx.get("exif"),
            max_edge=int(data.get("max_edge") or 0) or None,
        )
        return Response(rendered, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:  # noqa: BLE001
        logger.exception("管道预览渲染失败")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------- 批量处理

def _plan_task(job, input_path: str, index: int, used_outputs: set):
    """在**父进程**里算好输出路径与覆盖判定。

    为什么规划要留在父进程而不是丢给子进程：
      1. 「目标已存在」必须由一个唯一的裁判判定，否则多进程下两个 worker
         会各自看到「不存在」然后同时写同一个文件；
      2. `resolve_output_name` 需要读 EXIF，父进程读一次可以顺手把结果
         通过任务传下去，省掉每个子进程一次 exiftool 调用；
      3. 路径逃逸、与原图重合这类安全问题在这里一次性拦掉，
         不必在子进程里重复实现。
    """
    opts = job.options
    input_root = opts.get("input_root") or config.get("DEFAULT", "input_folder")
    output_root = opts.get("output_root") or config.get("DEFAULT", "output_folder")

    try:
        exif = get_exif(input_path)
    except Exception:  # noqa: BLE001
        exif = {}

    # 默认命名规则是 `{name}_{template}`：把模板名带进文件名，同一张图换模板
    # 就不会因为输出路径相同而被判成「已存在」后跳过。
    # 自定义文字水印没有模板名，用一个稳定的占位标签，保证命名仍然可复现。
    template_label = opts.get("template_name") or config.get("render", "template_name")
    if opts.get("mode", "template") != "template":
        template_label = "自定义"

    output_path = imaging.resolve_output_name(
        input_path, input_root, output_root,
        output_format=opts.get("output_format", "original"),
        name_pattern=opts.get("name_pattern", "{name}"),
        index=index,
        exif=exif,
        template_name=template_label,
    )

    name = os.path.basename(input_path)

    # 兜底：万一输出路径和原图重合，绝不能静默跳过，更不能覆盖原图。
    if is_same_path(output_path, input_path):
        return None, {"input_path": input_path, "output_path": output_path,
                      "status": "failure",
                      "error": "输出路径与原图相同（输出目录不能等于输入目录），已跳过以免覆盖原图"}

    # 命名规则可能把不同源文件映射到同一个输出名（例如固定文件名），
    # 这种冲突必须在提交前拦掉，否则并行写会互相覆盖。
    key = normalize_path(output_path)
    if key in used_outputs:
        return None, {"input_path": input_path, "output_path": output_path,
                      "status": "failure",
                      "error": "输出文件名与本次任务中的另一张照片重复，请调整「命名规则」"}
    used_outputs.add(key)

    skip_existing = os.path.exists(output_path) and not opts.get("override_existed", False)

    return {
        "input_path": input_path,
        "output_path": output_path,
        "options": opts,
        "exif": exif,
        "skip_existing": skip_existing,
    }, None


def _run_job(job):
    job.mark_running()
    try:
        opts = job.options
        workers = perf.worker_count(
            opts.get("workers", "auto"), job.total,
            config.getint("DEFAULT", "workers_min_batch", fallback=4),
        )

        tasks, pre_results = [], []
        used_outputs: set = set()
        for index, file_path in enumerate(job.files, start=1):
            if job.cancel_event.is_set():
                break
            if not os.path.exists(file_path):
                pre_results.append({"input_path": file_path, "output_path": None,
                                    "status": "failure", "error": "文件不存在"})
                continue
            task, pre = _plan_task(job, file_path, index, used_outputs)
            if pre is not None:
                pre_results.append(pre)
            else:
                tasks.append(task)

        # 先把「计划阶段就注定失败」的项记进去，用户才不会看到总数对不上
        for item in pre_results:
            job.mark_file_done(os.path.basename(str(item["input_path"])),
                               item["status"], item.get("error"),
                               source=item.get("input_path"))

        if tasks and not job.cancel_event.is_set():
            job.engine = "pool" if workers > 1 else "serial"
            job.workers = workers if workers > 1 else 1
            job.emit("progress", job.snapshot())
            if workers > 1:
                logger.info(f"批量任务 {job.id}：{len(tasks)} 张，{workers} 进程并行")
            else:
                logger.info(f"批量任务 {job.id}：{len(tasks)} 张，串行处理")

            def on_start(task):
                job.mark_file_start(os.path.basename(str(task["input_path"])))

            def on_result(result):
                job.mark_file_done(
                    os.path.basename(str(result.get("input_path") or "")),
                    result.get("status", "failure"),
                    result.get("error"),
                    result.get("output_path"),
                    source=result.get("input_path"),
                )

            mode = perf.run_tasks(tasks, workers, job.cancel_event, on_start, on_result)
            if mode == "pool-fallback":
                job.engine = "pool-fallback"
                job.workers = 1
            elif mode == "serial":
                job.engine = "serial"
                job.workers = 1

        job.mark_done(cancelled=job.cancel_event.is_set())
    except Exception as e:  # noqa: BLE001
        logger.exception("批量任务异常")
        job.mark_error(str(e))


@api.route("/api/v1/process/start", methods=["POST"])
def start_batch():
    data = request.get_json() or {}
    files = data.get("files") or []
    if not files:
        return jsonify({"error": "没有选中任何文件"}), 400

    # 目录以请求体为准：界面上的「保存配置」是防抖的，不能作为任务依据。
    input_root = (data.get("input_folder") or config.get("DEFAULT", "input_folder")).strip()
    requested_output = (data.get("output_folder") or config.get("DEFAULT", "output_folder")).strip()

    if not input_root or not os.path.isdir(input_root):
        return jsonify({"error": f"输入目录不存在：{input_root or '（空）'}"}), 400

    output_root, note = resolve_output_root(input_root, requested_output)
    if note:
        logger.warning(note)

    # 同步回配置，让界面上的目录框和实际行为保持一致
    try:
        config.set("DEFAULT", "input_folder", input_root)
        config.set("DEFAULT", "output_folder", output_root)
        save_config(config)
    except Exception:
        logger.debug("同步目录配置失败", exc_info=True)

    options = {
        "mode": data.get("mode", "template"),
        "template_name": data.get("template_name") or config.get("render", "template_name"),
        "crop_preset": data.get("crop_preset", config.get("DEFAULT", "crop_preset")),
        "output_format": data.get("output_format", config.get("DEFAULT", "output_format")),
        "name_pattern": data.get("name_pattern", config.get("DEFAULT", "name_pattern")),
        "quality": int(data.get("quality", config.getint("DEFAULT", "quality"))),
        "subsampling": int(data.get("subsampling", config.getint("DEFAULT", "subsampling"))),
        "long_edge": int(data.get("long_edge", config.getint("DEFAULT", "long_edge"))),
        "preserve_exif": bool(data.get("preserve_exif", config.getboolean("DEFAULT", "preserve_exif"))),
        "override_existed": bool(data.get("override_existed", config.getboolean("DEFAULT", "override_existed"))),
        "simple": data.get("simple") or {},
        # 性能：进程数策略随任务下发，任务执行期间不受配置改动影响
        "workers": str(data.get("workers") or config.get("DEFAULT", "workers", fallback="auto")),
        # 自定义 Logo 随任务下发
        "logo": data.get("logo") or logo_settings(),
        # 关键：把目录固化进任务，任务执行期间不再依赖配置文件
        "input_root": input_root,
        "output_root": output_root,
        "output_note": note,
    }

    # 提前建好输出目录，这样即使用户处理中途去看也不会扑空
    try:
        os.makedirs(output_root, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"无法创建输出目录 {output_root}：{e}"}), 400

    job = manager.create(files, options)
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return jsonify({
        "job_id": job.id,
        "total": job.total,
        "input_root": input_root,
        "output_root": output_root,
        "note": note,
    })


@api.route("/api/v1/process/stream/<job_id>")
def stream_job(job_id):
    job = manager.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    def generate():
        yield f"event: progress\ndata: {json.dumps(job.snapshot(), ensure_ascii=False)}\n\n"
        while True:
            try:
                event, data = job.queue.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                if job.status in ("done", "cancelled", "error"):
                    break
                continue
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if event == "complete":
                break

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@api.route("/api/v1/process/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    ok = manager.cancel(job_id)
    return jsonify({"cancelled": ok})


@api.route("/api/v1/process/status/<job_id>")
def job_status(job_id):
    job = manager.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job.snapshot())


@api.route("/api/v1/process/report/<job_id>")
def job_report(job_id):
    """导出处理报告（纯文本）。"""
    job = manager.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    lines = [f"任务 {job.id}",
             f"输入目录：{job.input_root}",
             f"输出目录：{job.output_root}",
             f"总计 {job.total}，成功 {job.success}，跳过 {job.skipped}，失败 {job.failure}", ""]
    if job.note:
        lines.insert(3, f"提示：{job.note}")
    if job.skipped_items:
        lines.append("跳过清单：")
        for item in job.skipped_items:
            lines.append(f"  - {item['file']}：{item['reason']}")
        lines.append("")
    if job.errors:
        lines.append("失败清单：")
        for item in job.errors:
            lines.append(f"  - {item['file']}: {item['error']}")
    else:
        lines.append("没有失败文件。")
    text = "\n".join(lines)
    return Response(text, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=report_{job.id}.txt"})


# ---------------------------------------------------------------- 模板

@api.route("/api/v1/templates")
def templates_list():
    return jsonify({"templates": list_templates()})


@api.route("/api/v1/template/<path:template_name>", methods=["GET"])
def template_get(template_name):
    try:
        return jsonify({"template_name": template_name, "content": get_template_content(template_name)})
    except FileNotFoundError:
        return jsonify({"error": f"模板 {template_name} 不存在"}), 404


@api.route("/api/v1/template", methods=["POST"])
def template_save():
    data = request.get_json() or {}
    name = (data.get("template_name") or "").strip()
    if not name:
        return jsonify({"error": "模板名不能为空"}), 400
    save_template(name, data.get("content", "[]"))
    return jsonify({"message": f"模板 {name} 已保存", "templates": list_templates()})


@api.route("/api/v1/template/<path:template_name>", methods=["DELETE"])
def template_delete(template_name):
    delete_template(template_name)
    return jsonify({"message": f"模板 {template_name} 已删除", "templates": list_templates()})


@api.route("/api/v1/template/validate", methods=["POST"])
def template_validate():
    """校验模板 JSON 是否合法。"""
    data = request.get_json() or {}
    content = data.get("content", "")
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            return jsonify({"valid": False, "error": "模板顶层必须是数组"})
        unknown = [n.get("processor_name") for n in parsed
                   if isinstance(n, dict) and n.get("processor_name")
                   and n.get("processor_name") not in _available_processors()]
        return jsonify({"valid": True, "count": len(parsed), "unknown": unknown})
    except json.JSONDecodeError as e:
        return jsonify({"valid": False, "error": f"JSON 语法错误：{e}"})


# ---------------------------------------------------------------- 预设方案

@api.route("/api/v1/presets")
def presets_list():
    return jsonify({"presets": list_presets()})


@api.route("/api/v1/preset/<path:name>", methods=["GET"])
def preset_get(name):
    item = get_preset(name)
    if not item:
        return jsonify({"error": f"预设 {name} 不存在"}), 404
    return jsonify(item)


@api.route("/api/v1/preset", methods=["POST"])
def preset_save():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "预设名不能为空"}), 400
    item = save_preset(name, data.get("config") or {}, data.get("desc") or "")
    return jsonify({"message": f"预设「{item['name']}」已保存", "preset": item,
                    "presets": list_presets()})


@api.route("/api/v1/preset/<path:name>", methods=["DELETE"])
def preset_delete(name):
    ok = delete_preset(name)
    if not ok:
        return jsonify({"error": f"预设 {name} 不存在"}), 404
    return jsonify({"message": f"预设 {name} 已删除", "presets": list_presets()})


# ---------------------------------------------------------------- 处理器元数据

def _available_processors() -> set:
    from processor.core import get_all_processors
    return set(get_all_processors().keys())


@api.route("/api/v1/processors")
def processors_schema():
    """可视化模板编辑器用的处理器清单与参数元数据。"""
    return jsonify({
        "processors": list_processor_schema(),
        "categories": CATEGORY_META,
    })


# ---------------------------------------------------------------- 自定义 Logo

ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}
# Logo 是拿来合成的小图，给一个宽松但有上限的尺寸，避免有人塞几百 MB 的图
MAX_LOGO_BYTES = 16 * 1024 * 1024


@api.route("/api/v1/logo/upload", methods=["POST"])
def logo_upload():
    """上传自定义 Logo。

    只做「校验 + 落盘」，不做图像转换：用户拿到的就是他给的文件，
    避免因为我们重编码而丢掉透明通道或降低画质。

    顺序很关键：**先在内存里验，验过了才写盘**。
    早期版本是「先写盘再验证，验证不过就 unlink」，结果在删除受限的环境里
    （比如受限沙箱、只读挂载、杀软拦截）会留下一个坏文件当垃圾。
    改成先验后写，任何路径上都不会产生半成品。
    """
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "没有收到文件"}), 400

    raw_name = os.path.basename(upload.filename)
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_LOGO_EXT:
        return jsonify({"error": f"不支持的图片格式 {ext or '（无扩展名）'}"}), 400

    # 读进内存。FileStorage 背后是临时文件，read() 不会把整包吃进 RAM 直到这里。
    payload = upload.read(MAX_LOGO_BYTES + 1)
    if len(payload) > MAX_LOGO_BYTES:
        return jsonify({"error": f"图片太大（上限 {MAX_LOGO_BYTES // 1024 // 1024} MB）"}), 400
    if not payload:
        return jsonify({"error": "文件是空的"}), 400

    # 先验：确保不是伪装扩展名的坏文件。这一步不碰磁盘。
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(payload)) as probe:
            probe.verify()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"文件不是有效的图片：{e}"}), 400

    stem = Path(raw_name).stem.strip() or "logo"
    target = logos_dir / f"{stem}{ext}"
    # 同名不覆盖，改成加序号，避免用户上传两次就悄悄丢了一份
    index = 2
    while target.exists():
        target = logos_dir / f"{stem}_{index}{ext}"
        index += 1

    logos_dir.mkdir(parents=True, exist_ok=True)
    try:
        target.write_bytes(payload)
    except Exception as e:  # noqa: BLE001
        logger.exception("Logo 上传失败")
        return jsonify({"error": f"保存失败：{e}"}), 500

    config.set("DEFAULT", "logo_name", target.name)
    save_config(config)
    logger.info(f"已上传自定义 Logo：{target.name}")
    return jsonify({"message": f"已上传 {target.name}", "logo_name": target.name,
                    "logos": list_logos()})


@api.route("/api/v1/logo/<path:name>", methods=["DELETE"])
def logo_delete(name):
    target = logos_dir / os.path.basename(name)
    if not target.exists():
        return jsonify({"error": "Logo 不存在"}), 404
    try:
        target.unlink()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    if config.get("DEFAULT", "logo_name", fallback="") == target.name:
        config.set("DEFAULT", "logo_name", "")
        save_config(config)
    return jsonify({"message": f"已删除 {target.name}", "logos": list_logos()})


# ---------------------------------------------------------------- 结果面板

@api.route("/api/v1/process/result/<job_id>")
def job_result(job_id):
    """处理结果清单（供结果确认面板使用）。"""
    job = manager.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    items = []
    for item in job.result_items():
        output = item.get("output")
        exists = bool(output) and os.path.exists(str(output))
        items.append({
            "file": item.get("file"),
            "status": item.get("status"),
            "error": item.get("error"),
            "output": output,
            "output_name": os.path.basename(str(output)) if output else None,
            "source": item.get("source"),
            "exists": exists,
            "size": (os.path.getsize(str(output)) if exists else None),
        })

    return jsonify({
        "job_id": job.id,
        "status": job.status,
        "total": job.total,
        "success": job.success,
        "skipped": job.skipped,
        "failure": job.failure,
        "input_root": job.input_root,
        "output_root": job.output_root,
        "note": job.note,
        "engine": job.engine,
        "workers": job.workers,
        "items": items,
    })


# ---------------------------------------------------------------- 性能

@api.route("/api/v1/perf")
def perf_status():
    return jsonify(perf_settings())


@api.route("/api/v1/perf/probe", methods=["POST", "GET"])
def perf_probe():
    """真起一个子进程验证多进程是否可用（打包环境尤其需要）。"""
    result = perf.probe_multiprocessing()
    result["suggested_workers"] = perf.worker_count(
        config.get("DEFAULT", "workers", fallback="auto"), 100,
        config.getint("DEFAULT", "workers_min_batch", fallback=4),
    )
    result["settings"] = perf_settings()
    return jsonify(result)


@api.route("/api/v1/perf/cache", methods=["DELETE"])
def perf_cache_clear():
    from core.util import clear_exif_cache
    clear_exif_cache()
    return jsonify({"message": "EXIF 缓存已清空", "exif_cache": exif_cache_stats()})


# ---------------------------------------------------------------- 资源 / 系统

@api.route("/api/v1/logo/<path:name>")
def get_logo(name):
    from core.configs import logos_dir
    target = logos_dir / name
    if target.exists():
        return send_file(str(target))
    return jsonify({"error": "not found"}), 404


@api.route("/api/v1/font/<path:name>")
def get_font(name):
    from core.configs import fonts_dir
    target = fonts_dir / name
    if target.exists():
        return send_file(str(target))
    return jsonify({"error": "not found"}), 404


def _folder_dialog_type():
    """取原生文件夹对话框类型枚举（新 API 优先，老版本兜底）。"""
    import webview
    dialog = getattr(webview, "FileDialog", None)
    if dialog is not None and hasattr(dialog, "FOLDER"):
        return dialog.FOLDER
    return getattr(webview, "FOLDER_DIALOG", 20)


def _first_path(result):
    """把 create_file_dialog 的返回值统一成单个路径字符串。"""
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        return str(result[0]) if result else None
    return str(result)


@api.route("/api/v1/pick_folder", methods=["POST"])
def pick_folder():
    """调用原生文件夹选择对话框。"""
    data = request.get_json() or {}
    initial = data.get("initial") or str(DATA_ROOT)

    # 优先用桌面窗口自带的原生对话框
    if _window is not None:
        try:
            result = _window.create_file_dialog(_folder_dialog_type(), directory=initial)
            return jsonify({"folder": _first_path(result)})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"webview 对话框失败，回退 tkinter: {e}")

    # 回退：tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(initialdir=initial)
        root.destroy()
        return jsonify({"folder": folder or None})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@api.route("/api/v1/drop", methods=["POST"])
def handle_drop():
    """处理拖拽进来的路径（文件夹或照片文件）。

    WebView2 下网页拿不到真实路径，真实路径由桌面外壳通过 pywebview 的
    DOM 事件回传，这里负责把它翻译成「输入目录 + 待选文件」。
    """
    data = request.get_json() or {}
    raw_paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
    if not raw_paths:
        return jsonify({"error": "没有收到有效路径"}), 400

    suffixes = _suffixes()
    dirs, files, unknown = [], [], []
    for p in raw_paths:
        if os.path.isdir(p):
            dirs.append(p)
        elif os.path.isfile(p):
            if Path(p).suffix.lower() in suffixes:
                files.append(p)
            else:
                unknown.append(p)
        else:
            unknown.append(p)

    if dirs:
        # 多个文件夹时取它们最浅的公共父目录
        folder = os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]
        selected = []
    elif files:
        folder = os.path.dirname(files[0])
        selected = files
    else:
        return jsonify({
            "error": "拖进来的内容里没有可处理的照片或文件夹"
                     + (f"（已忽略：{', '.join(os.path.basename(u) for u in unknown[:3])}）"
                        if unknown else ""),
        }), 400

    folder = os.path.abspath(folder)
    config.set("DEFAULT", "input_folder", folder)
    try:
        save_config(config)
    except Exception:
        logger.debug("保存拖拽目录失败", exc_info=True)

    return jsonify({
        "folder": folder,
        "selected": selected,
        "count": len(selected),
        "ignored": [os.path.basename(u) for u in unknown],
    })


@api.route("/api/v1/open_folder", methods=["POST"])
def open_folder():
    """在资源管理器中打开目录。"""
    data = request.get_json() or {}
    target = data.get("path") or config.get("DEFAULT", "output_folder")
    try:
        os.startfile(target)  # noqa: S606 (Windows)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@api.route("/api/v1/reveal", methods=["POST"])
def reveal_in_explorer():
    """在资源管理器中**定位并选中**某个文件（结果面板用）。"""
    data = request.get_json() or {}
    path = data.get("path")
    if not path or not os.path.exists(path):
        return jsonify({"error": "文件不存在"}), 404
    try:
        import subprocess
        # explorer 的参数解析比较挑：/select, 与路径之间不能有空格
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')  # noqa: S603,S607
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@api.route("/api/v1/save_as", methods=["POST"])
def save_as():
    """把某个成片另存到用户指定的位置（结果面板的「另存为」）。"""
    data = request.get_json() or {}
    src = data.get("path")
    if not src or not os.path.exists(src):
        return jsonify({"error": "源文件不存在"}), 404

    default_name = os.path.basename(src)
    target = None

    if _window is not None:
        try:
            dialog = getattr(__import__("webview"), "FileDialog", None)
            kind = getattr(dialog, "SAVE", 30) if dialog else 30
            result = _window.create_file_dialog(
                kind, directory=os.path.dirname(src), save_filename=default_name)
            target = _first_path(result)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"另存为对话框失败，回退 tkinter: {e}")

    if not target:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            target = filedialog.asksaveasfilename(
                initialdir=os.path.dirname(src), initialfile=default_name,
                defaultextension=Path(default_name).suffix)
            root.destroy()
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    if not target:
        return jsonify({"cancelled": True})

    try:
        if is_same_path(target, src):
            return jsonify({"message": "目标与原文件相同，无需复制", "path": target})
        shutil.copy2(src, target)
        return jsonify({"message": f"已另存为 {os.path.basename(target)}", "path": target})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"保存失败：{e}"}), 500


@api.route("/api/v1/shutdown", methods=["POST"])
def shutdown():
    """退出程序（供窗口内按钮调用）。"""
    def _exit():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return jsonify({"ok": True})


def run_server(host: str = None, port: int = None, debug: bool = False):
    host = host or config.get("DEFAULT", "host")
    port = port or config.getint("DEFAULT", "port")
    logger.info(f"{APP_DISPLAY_NAME} 服务启动: http://{host}:{port}")
    api.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)


if __name__ == "__main__":
    # 打包后 spawn 子进程需要它把流程转交给 multiprocessing 引导逻辑，
    # 详见 desktop_app.py 中同名调用的说明。
    import multiprocessing

    multiprocessing.freeze_support()
    run_server(debug=config.getboolean("DEFAULT", "debug"))
