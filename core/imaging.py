"""图像输出：格式转换、长边限制、EXIF 回写。"""
import io
import re
from pathlib import Path

from PIL import Image

from core.logger import logger
from core.util import copy_exif

JPEG_EXT = {".jpg", ".jpeg"}
PNG_EXT = {".png"}
WEBP_EXT = {".webp"}
TIFF_EXT = {".tif", ".tiff"}


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """把带透明通道的图合成到白底，避免存 JPEG 时透明区变黑。"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB") if img.mode != "RGB" else img


def apply_long_edge(img: Image.Image, long_edge: int) -> Image.Image:
    """限制长边尺寸（只缩小，不放大，避免画质损失）。"""
    if not long_edge or long_edge <= 0:
        return img
    current = max(img.width, img.height)
    if current <= long_edge:
        return img
    scale = long_edge / current
    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def save_output(img: Image.Image, output_path, source_path=None, preserve_exif: bool = True,
                quality: int = 95, subsampling: int = 0, long_edge: int = 0) -> None:
    """按目标扩展名保存图像，并在需要时把原图元数据回写过去。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()

    img = apply_long_edge(img, long_edge)

    if suffix in JPEG_EXT:
        img = _flatten_to_rgb(img)
        img.save(path, format="JPEG", quality=int(quality), subsampling=int(subsampling),
                 optimize=True, progressive=True)
    elif suffix in PNG_EXT:
        if img.mode not in ("RGB", "RGBA", "L", "P"):
            img = img.convert("RGBA")
        img.save(path, format="PNG", optimize=True)
    elif suffix in WEBP_EXT:
        img.save(path, format="WEBP", quality=int(quality), method=6)
    elif suffix in TIFF_EXT:
        img.save(path, format="TIFF", compression="tiff_lzw")
    else:
        # 未知扩展名，退回 PNG
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.save(path, format="PNG")

    if preserve_exif and source_path:
        if not copy_exif(str(source_path), str(path)):
            logger.debug(f"EXIF 未回写（exiftool 不可用或失败）: {path}")

    logger.success(f"已生成: {path}")


CROP_PRESETS = {
    "1:1": 1.0,
    "4:5": 4 / 5,
    "3:4": 3 / 4,
    "2:3": 2 / 3,
    "3:2": 3 / 2,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
}


def aspect_crop_box(width: int, height: int, preset: str):
    """算出「按画幅比例居中裁切」的裁切窗 (left, top, right, bottom)。

    不裁切时返回 None。抽成独立函数是为了让预览侧能同步修正
    「原图在成片中的位置」，避免裁切之后对比模式错位。
    """
    if not preset or preset in ("none", "original", ""):
        return None
    ratio = CROP_PRESETS.get(preset)
    if not ratio or not height:
        return None
    current = width / height
    if abs(current - ratio) < 0.005:
        return None
    if current > ratio:
        new_w = int(round(height * ratio))
        left = (width - new_w) // 2
        return (left, 0, left + new_w, height)
    new_h = int(round(width / ratio))
    top = (height - new_h) // 2
    return (0, top, width, top + new_h)


def apply_aspect_crop(img: Image.Image, preset: str) -> Image.Image:
    """按社交平台画幅比例居中裁切（none 表示不裁切）。"""
    box = aspect_crop_box(img.width, img.height, preset)
    return img.crop(box) if box else img


def render_preview_bytes(img: Image.Image, max_edge: int = 1400, quality: int = 88) -> bytes:
    """把处理结果编码为 JPEG 字节流，供界面实时预览。"""
    preview = img.copy()
    preview = apply_long_edge(preview, max_edge)
    preview = _flatten_to_rgb(preview)
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


# EXIF 方向标记里这几档会把宽高对调
_ORIENT_SWAP = {5, 6, 7, 8}


def preview_size(path: str, max_edge: int = 1400):
    """算出该图在预览时的画布尺寸，与 `load_for_preview` 的结果严格一致。

    只读文件头与方向标记、不解码像素：预览第一步本来就要解码一次原图，
    这里不能为了拿尺寸先把图完整读一遍。

    用途见 `app._preview_context()`：模板里的 `{{vh(3)}}` 是「图片尺寸的百分比」，
    预览必须按预览画布的尺寸来解释，否则留白和字号会被放大数倍。
    """
    with Image.open(path) as img:
        width, height = img.size
        try:
            orientation = img.getexif().get(0x0112, 1)
        except Exception:  # noqa: BLE001
            orientation = 1
    if orientation in _ORIENT_SWAP:
        width, height = height, width
    if max_edge and max(width, height) > max_edge:
        scale = max_edge / max(width, height)
        width = max(1, round(width * scale))
        height = max(1, round(height * scale))
    return width, height


def load_for_preview(path: str, max_edge: int = 1400) -> Image.Image:
    """为预览加载图片：**先降采样，再交给处理器链**。

    这是预览性能的关键优化。原实现是「按原图尺寸跑完整条管道，最后才缩小」，
    一张 6000×4000 的照片要在全分辨率下做高斯模糊、圆角、阴影，单次预览
    动辄 1.5~4 秒；而且这些中间结果随即被丢弃。

    改成先降到预览尺寸再渲染后：
      * 渲染面积减少约 20~30 倍，预览耗时通常降到 100~300ms；
      * 水印的尺寸参数本来就走 `vh()` / 百分比，视觉结果基本一致。

    注意 `exif_transpose` 必须在这里做掉，因为处理器链后续不再读文件。
    """
    from PIL import ImageOps

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img.load()
    if max_edge and max(img.width, img.height) > max_edge:
        scale = max_edge / max(img.width, img.height)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.Resampling.LANCZOS)
    return img


_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename_token(text) -> str:
    """把模板名之类的任意文本清洗成可以安全放进文件名的片段。

    Windows 文件名不允许 `\\ / : * ? " < > |`，模板名（如「画框Pro-中」）本身
    是合法的，但用户自建模板可能带斜杠，不洗一遍会直接写出一个非法路径。
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", str(text or "")).strip(" .")
    return cleaned or "未命名"


def resolve_output_name(input_path: str, input_root: str, output_root: str,
                        output_format: str = "original", name_pattern: str = "{name}",
                        index: int = 0, exif: dict = None, template_name: str = "") -> str:
    """根据命名规则与输出格式，计算输出文件的绝对路径。

    安全约束：结果必须落在 output_root 之内。
    如果待处理文件并不在 input_root 下（例如输入目录被改过、或调用方传错），
    `os.path.relpath` 会算出一串 `..`，拼出来的路径会「逃逸」到输出目录之外，
    成片就落到一个谁也想不到的地方 —— 用户看到的现象就是「处理完成但没有输出」。
    这里统一拦截：逃逸时改为直接平铺在 output_root 下。

    exif：可选。批量处理时父进程已经读过 EXIF，传进来可以省掉一次 exiftool
    子进程调用（Windows 上每次约 80ms，1000 张就是 80 秒）。

    template_name：可选。命名规则里的 `{template}` 会渲染成清洗过的模板名。
    默认命名规则 `{name}_{template}` 依赖它，这样「同一张图换模板」就不会
    再因为输出文件名相同而被判成「已存在」直接跳过。
    """
    import datetime
    import os

    from core.util import is_subpath

    src = Path(input_path)
    try:
        relative = os.path.relpath(str(src), str(input_root))
    except ValueError:
        relative = src.name
    rel_path = Path(relative)

    stem = rel_path.stem
    parent = rel_path.parent

    # 输出格式 -> 扩展名
    if output_format in (None, "", "original"):
        ext = src.suffix
    elif output_format == "jpg":
        ext = ".jpg"
    elif output_format == "png":
        ext = ".png"
    elif output_format == "webp":
        ext = ".webp"
    else:
        ext = f".{output_format.lstrip('.')}"

    now = datetime.datetime.now()
    exif_date = ""
    try:
        if exif is None:
            from core.util import get_exif
            exif = get_exif(str(src))
        raw = (exif or {}).get("DateTimeOriginal") or (exif or {}).get("CreateDate") or ""
        exif_date = raw[:10].replace(":", "-") if raw else ""
    except Exception:
        exif_date = ""

    try:
        new_stem = name_pattern.format(
            name=stem,
            index=index,
            index3=f"{index:03d}",
            date=exif_date or now.strftime("%Y-%m-%d"),
            datetime=now.strftime("%Y%m%d_%H%M%S"),
            template=safe_filename_token(template_name),
        )
    except (KeyError, IndexError, ValueError):
        new_stem = f"{stem}_wm"

    if not new_stem:
        new_stem = stem

    root = Path(output_root)
    result = root / parent / f"{new_stem}{ext}"

    # 拦截「逃逸出输出目录」的情况
    if not is_subpath(str(result), str(root)):
        result = root / f"{new_stem}{ext}"

    return str(result)


# ---------------------------------------------------------------- 自定义 Logo

# 100% 缩放对应「Logo 高度 = 画面高度的 8%」。
# 选 8% 是因为在 6000×4000 的片子上大约是 320px 高，
# 既有存在感又不至于喧宾夺主；用户可以用滑杆在 10%~300% 之间微调。
LOGO_BASE_RATIO = 0.08

LOGO_CORNERS = {
    "top_left": "左上",
    "top_right": "右上",
    "bottom_left": "左下",
    "bottom_right": "右下",
    "center": "居中",
}


def apply_custom_logo(img: Image.Image, logo_path: str, scale_pct=100, opacity_pct=100,
                      corner: str = "bottom_right", margin_ratio: float = 0.04) -> Image.Image:
    """把用户上传的自定义 Logo 合成到成片上（尺寸 / 位置 / 透明度可调）。

    与模板里的 `{{auto_logo()}}` 是两条互不干扰的通路：
      * `auto_logo()` 按 EXIF 品牌自动挑内置 Logo，是模板作者用的；
      * 这里是**用户级后处理**，任何模板都能叠加自己的 Logo（例如工作室水印）。

    合成放在整条管道**之后**、裁切**之后**，这样 Logo 的定位是相对最终画幅的，
    不会因为裁切偏移而跑出画面。
    """
    if not logo_path or not Path(logo_path).exists():
        return img

    try:
        with Image.open(logo_path) as raw:
            logo = raw.convert("RGBA")
            logo.load()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"自定义 Logo 载入失败 {logo_path}: {e}")
        return img

    try:
        scale = max(1, int(scale_pct)) / 100.0
    except (TypeError, ValueError):
        scale = 1.0
    try:
        opacity = max(0, min(int(opacity_pct), 100)) / 100.0
    except (TypeError, ValueError):
        opacity = 1.0

    canvas = img.convert("RGBA")
    target_h = max(8, int(canvas.height * LOGO_BASE_RATIO * scale))
    ratio = logo.width / logo.height if logo.height else 1.0
    target_w = max(8, int(target_h * ratio))
    logo = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)

    if opacity < 1.0:
        r, g, b, a = logo.split()
        a = a.point(lambda p: int(p * opacity))
        logo = Image.merge("RGBA", (r, g, b, a))

    margin = max(0, int(min(canvas.width, canvas.height) * float(margin_ratio or 0)))
    if corner == "top_left":
        x, y = margin, margin
    elif corner == "top_right":
        x, y = canvas.width - target_w - margin, margin
    elif corner == "bottom_left":
        x, y = margin, canvas.height - target_h - margin
    elif corner == "center":
        x, y = (canvas.width - target_w) // 2, (canvas.height - target_h) // 2
    else:  # bottom_right
        x, y = canvas.width - target_w - margin, canvas.height - target_h - margin

    canvas.alpha_composite(logo, (max(0, x), max(0, y)))
    return canvas
