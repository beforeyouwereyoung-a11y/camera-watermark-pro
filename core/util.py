"""通用工具：EXIF 读取（exiftool + Pillow 降级）、文件扫描、模板读写。"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from functools import wraps
from pathlib import Path

from PIL import ExifTags, Image
from jinja2 import Template

from core import BUNDLE_ROOT, DATA_ROOT
from core.configs import templates_dir
from core.jinja2renders import auto_logo, render_env, vh, vw  # noqa: F401
from core.logger import logger

# ---------------------------------------------------------------- exiftool

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _find_exiftool():
    """定位 exiftool：用户目录优先，内置兜底，最后看系统 PATH。"""
    candidates = [
        DATA_ROOT / "exiftool" / "exiftool.exe",
        BUNDLE_ROOT / "exiftool" / "exiftool.exe",
        DATA_ROOT / "exiftool" / "exiftool",
        BUNDLE_ROOT / "exiftool" / "exiftool",
    ]
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which("exiftool")
    return Path(found) if found else None


EXIFTOOL_PATH = _find_exiftool()
EXIFTOOL_AVAILABLE = EXIFTOOL_PATH is not None

# EXIF 缓存：改成带容量上限的 LRU。
#
# 旧实现是「满了就整个清空」，在几千张的批量里会出现周期性「集体失忆」，
# 于是反复调用 exiftool（每次一个子进程，Windows 上 ~80ms）。
# 换成 OrderedDict + 逐条淘汰后，命中率在长批量里稳定在 95% 以上。
DEFAULT_EXIF_CACHE_SIZE = 2048
_exif_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_exif_lock = threading.Lock()
_exif_cache_size = DEFAULT_EXIF_CACHE_SIZE
_exif_stats = {"hit": 0, "miss": 0}


def set_exif_cache_size(size) -> int:
    """设置 EXIF 缓存容量（条目数）。"""
    global _exif_cache_size
    try:
        value = int(size)
    except (TypeError, ValueError):
        return _exif_cache_size
    _exif_cache_size = max(64, min(value, 200_000))
    with _exif_lock:
        while len(_exif_cache) > _exif_cache_size:
            _exif_cache.popitem(last=False)
    return _exif_cache_size


def clear_exif_cache() -> None:
    with _exif_lock:
        _exif_cache.clear()
        _exif_stats["hit"] = 0
        _exif_stats["miss"] = 0


def exif_cache_stats() -> dict:
    with _exif_lock:
        total = _exif_stats["hit"] + _exif_stats["miss"]
        return {
            "size": len(_exif_cache),
            "capacity": _exif_cache_size,
            "hit": _exif_stats["hit"],
            "miss": _exif_stats["miss"],
            "hit_rate": round(_exif_stats["hit"] / total * 100, 1) if total else 0.0,
        }



def _run_exiftool(args, timeout=30):
    """静默执行 exiftool（Windows 下不弹黑窗口）。"""
    env = dict(os.environ)
    env.setdefault("LC_ALL", "C")
    env.setdefault("LANG", "C")
    kwargs = {"stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL,
              "env": env, "timeout": timeout}
    if _NO_WINDOW:
        kwargs["creationflags"] = _NO_WINDOW
    return subprocess.check_output([str(EXIFTOOL_PATH), *args], **kwargs)


def _exif_from_pillow(path) -> dict:
    """exiftool 不可用时的降级方案：用 Pillow 读取常见 EXIF 字段。

    键名尽量对齐 exiftool 的输出，保证模板不用改动。
    """
    result: dict = {}
    try:
        with Image.open(path) as img:
            result["ImageWidth"] = str(img.width)
            result["ImageHeight"] = str(img.height)
            raw = img.getexif()
            if not raw:
                return result
            # 部分机型把参数放在 Exif IFD 里
            ifd = raw.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {}
            merged = {}
            for k, v in raw.items():
                merged[ExifTags.TAGS.get(k, str(k))] = v
            for k, v in (ifd or {}).items():
                merged.setdefault(ExifTags.TAGS.get(k, str(k)), v)

        def put(target, *sources):
            for s in sources:
                v = merged.get(s)
                if v not in (None, ""):
                    result[target] = str(v)
                    return

        put("Make", "Make")
        put("Model", "Model")
        put("CameraModelName", "Model")
        put("LensModel", "LensModel", "LensType")
        put("FNumber", "FNumber")
        put("ApertureValue", "ApertureValue")
        put("AperatureValue", "ApertureValue")
        put("ShutterSpeedValue", "ShutterSpeedValue", "ExposureTime")
        put("ShutterSpeed", "ShutterSpeedValue", "ExposureTime")
        put("ExposureTime", "ExposureTime")
        put("ISO", "ISOSpeedRatings", "PhotographicSensitivity", "ISO")
        put("FocalLength", "FocalLength")
        put("FocalLengthIn35mmFormat", "FocalLengthIn35mmFilm")
        put("DateTimeOriginal", "DateTimeOriginal", "DateTimeDigitized", "DateTime")
        put("CreateDate", "DateTimeOriginal", "DateTimeDigitized", "DateTime")
        put("Software", "Software")
        put("Orientation", "Orientation")
    except Exception as e:
        logger.debug(f"Pillow EXIF fallback failed for {path}: {e}")
    return result


def get_exif(path) -> dict:
    """读取照片 EXIF，带 LRU 缓存；exiftool 不可用时自动降级到 Pillow。"""
    key = None
    try:
        st = os.stat(path)
        key = (str(path), int(st.st_mtime), st.st_size)
        with _exif_lock:
            if key in _exif_cache:
                _exif_cache.move_to_end(key)   # LRU：命中即刷新位置
                _exif_stats["hit"] += 1
                return _exif_cache[key]
    except OSError:
        pass

    exif_dict: dict = {}
    if EXIFTOOL_AVAILABLE:
        try:
            # -m 忽略次要错误：少数手机样张有残缺标签，不加会让整次读取失败
            output = _run_exiftool(["-m", "-d", "%Y-%m-%d %H:%M:%S%3f%z", str(path)])
            text = output.decode("utf-8", errors="ignore")
            for line in text.splitlines():
                if ":" not in line:
                    continue
                key_part, _, value = line.partition(":")
                key_part = re.sub(r"\s+", "", key_part.strip())
                key_part = key_part.replace("/", "")
                if key_part:
                    exif_dict[key_part] = value.strip()
        except Exception as e:
            logger.error(f"exiftool failed for {path}: {e}")

    # 关键字段缺失时用 Pillow 补全（例如 exiftool 缺失、或图片无完整 EXIF）
    if not exif_dict or "ImageWidth" not in exif_dict:
        fallback = _exif_from_pillow(path)
        for k, v in fallback.items():
            exif_dict.setdefault(k, v)

    if key is not None:
        with _exif_lock:
            _exif_stats["miss"] += 1
            _exif_cache[key] = exif_dict
            _exif_cache.move_to_end(key)
            while len(_exif_cache) > _exif_cache_size:
                _exif_cache.popitem(last=False)   # 逐条淘汰最久未用的
    return exif_dict


def exif_status() -> dict:
    return {
        "exiftool_available": EXIFTOOL_AVAILABLE,
        "exiftool_path": str(EXIFTOOL_PATH) if EXIFTOOL_PATH else None,
    }


def copy_exif(src: str, dst: str) -> bool:
    """把源图的元数据整体复制到输出图（修复成片丢 EXIF 的问题）。

    注意：Orientation 必须用 `#=` 强制按数值写入。
    实测该 exiftool 构建下 `-Orientation=1` 会被错误写成 3（Rotate 180），
    导致成片在浏览器里被翻转 180°。
    """
    if not EXIFTOOL_AVAILABLE:
        return False
    try:
        _run_exiftool([
            "-overwrite_original", "-quiet",
            "-TagsFromFile", str(src),
            "-all:all",
            "-Orientation#=1",   # 像素方向已由 exif_transpose 处理，强制置为正常
            str(dst),
        ])
        return True
    except Exception as e:
        logger.warning(f"copy_exif failed {src} -> {dst}: {e}")
        return False


# ---------------------------------------------------------------- 路径工具

# 输入目录与输出目录相同时，自动改用这个子目录名，避免覆盖原图 / 全部被跳过。
SAME_DIR_OUTPUT_SUBDIR = "水印输出"


def normalize_path(path) -> str:
    """把路径规范化成可比较的形式（绝对路径 + 统一大小写与分隔符）。

    Windows 下 `E:\\a\\b` 与 `e:/A/B/` 必须视为同一路径。
    """
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
    except Exception:
        return str(path)


def is_same_path(a, b) -> bool:
    """判断两个路径是否指向同一个位置。"""
    if not a or not b:
        return False
    return normalize_path(a) == normalize_path(b)


def is_subpath(child, parent) -> bool:
    """判断 child 是否位于 parent 内部（parent 自身不算）。"""
    if not child or not parent:
        return False
    try:
        c = normalize_path(child)
        p = normalize_path(parent)
        return c != p and c.startswith(p.rstrip(os.sep) + os.sep)
    except Exception:
        return False


def resolve_output_root(input_root: str, output_root: str):
    """计算真正生效的输出目录。

    关键修复：当输出目录与输入目录相同时，`resolve_output_name()` 会算出与
    原图完全一样的路径，随后因「目标已存在且未勾选覆盖」而把**所有**照片静默
    跳过 —— 界面上却仍提示「处理完成」，用户会以为程序没干活。

    这里统一改为：输入 == 输出时，自动输出到 `<输入目录>/水印输出`，
    既不会覆盖原图，也不会出现「零产出却提示完成」。

    返回 (effective_output_root, note)；note 为需要提示给用户的一句话（可为 None）。
    """
    if not output_root:
        output_root = input_root
    if is_same_path(input_root, output_root):
        effective = os.path.join(str(input_root), SAME_DIR_OUTPUT_SUBDIR)
        note = (f"输出目录与输入目录相同，已自动改为「{effective}」，"
                f"避免覆盖原图。")
        return effective, note
    return str(output_root), None


# ---------------------------------------------------------------- 文件扫描

def list_files(path: str, suffixes: set, depth: int = 0, max_depth: int = 20,
               exclude_dirs=None):
    """递归扫描目录，返回可用的文件树。

    exclude_dirs：需要跳过的目录（绝对路径集合），用于把输出目录排除在
    输入扫描之外，避免「输出目录在输入目录里」时把成片再次当作素材。
    """
    result = []
    root = Path(path)
    if not root.exists() or depth > max_depth:
        return result

    excluded = {normalize_path(d) for d in (exclude_dirs or []) if d}

    try:
        items = list(root.iterdir())
        dirs = sorted([i for i in items if i.is_dir()], key=lambda x: x.name.lower())
        files = sorted([i for i in items if i.is_file()],
                       key=lambda x: (x.stat().st_mtime, x.name.lower()), reverse=True)

        for item in dirs:
            if item.name.startswith(".") or item.is_symlink():
                continue
            if normalize_path(item) in excluded:
                continue
            children = list_files(str(item), suffixes, depth + 1, max_depth, excluded)
            if children:
                result.append({"label": item.name, "value": str(item),
                               "is_file": False, "children": children})

        for item in files:
            if item.name.startswith("."):
                continue
            if item.suffix.lower() in suffixes:
                result.append({"label": item.name, "value": str(item), "is_file": True})
    except PermissionError:
        logger.debug(f"list_files: permission denied {path}")
    except Exception as e:
        logger.error(f"list_files failed {path}: {e}")
    return result

    try:
        items = list(root.iterdir())
        dirs = sorted([i for i in items if i.is_dir()], key=lambda x: x.name.lower())
        files = sorted([i for i in items if i.is_file()],
                       key=lambda x: (x.stat().st_mtime, x.name.lower()), reverse=True)

        for item in dirs:
            if item.name.startswith(".") or item.is_symlink():
                continue
            children = list_files(str(item), suffixes, depth + 1, max_depth)
            if children:
                result.append({"label": item.name, "value": str(item),
                               "is_file": False, "children": children})

        for item in files:
            if item.name.startswith("."):
                continue
            if item.suffix.lower() in suffixes:
                result.append({"label": item.name, "value": str(item), "is_file": True})
    except PermissionError:
        logger.debug(f"list_files: permission denied {path}")
    except Exception as e:
        logger.error(f"list_files failed {path}: {e}")
    return result


def log_rt(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        logger.debug(f"[monitor]api#{func.__name__} cost {(time.time() - start) * 1000:.2f}ms")
        return result
    return wrapper


def convert_heic_to_jpeg(path: str, quality: int = 90) -> io.BytesIO:
    """HEIC/HEIF 转 JPEG 字节流，供预览使用。"""
    with Image.open(path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        buffer.seek(0)
        return buffer


# ---------------------------------------------------------------- 模板管理

def get_template_path(template_name: str) -> Path:
    return templates_dir / f"{template_name}.json"


def get_template(template_name: str) -> Template:
    path = get_template_path(template_name)
    if not path.exists():
        # 找不到就退回默认模板，避免直接崩
        path = get_template_path("标准水印2")
    with open(path, encoding="utf-8") as f:
        template_str = f.read()
    # 必须走带自定义过滤器的环境：裸 Template(source) 在构造时就编译，
    # 过滤器名在编译期解析，晚注册会直接抛 No filter named '...'。
    return render_env().from_string(template_str)


def get_template_content(template_name: str) -> str:
    path = get_template_path(template_name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def save_template(template_name: str, content: str) -> None:
    path = get_template_path(template_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def delete_template(template_name: str) -> None:
    path = get_template_path(template_name)
    if path.exists():
        path.unlink()


def list_templates() -> list:
    if not templates_dir.exists():
        return []
    return sorted([f.stem for f in templates_dir.glob("*.json")])


def list_fonts() -> list:
    from core.configs import fonts_dir
    if not fonts_dir.exists():
        return []
    return sorted([f.name for f in fonts_dir.iterdir()
                   if f.suffix.lower() in {".ttf", ".otf", ".ttc"}])


def list_logos() -> list:
    from core.configs import logos_dir
    if not logos_dir.exists():
        return []
    return sorted([f.name for f in logos_dir.iterdir()
                   if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}])


def render_template_json(template_name: str, context: dict):
    """渲染模板为处理器配置列表。"""
    template = get_template(template_name)
    return json.loads(template.render(context))


def render_pipeline_json(pipeline, context: dict):
    """把「还没落盘的处理器管道」渲染成最终配置列表。

    可视化编辑器改的是内存里的管道，并没有写成模板文件，所以不能走
    `render_template_json`（那个按名字去磁盘读文件）。这里用**同一套 Jinja
    全局函数**（vh / vw / auto_logo）渲染一段 JSON 文本，保证「编辑器预览」
    与「真正批量处理」的渲染语义完全一致。

    踩过的坑：早期这个接口把原始 pipeline 直接丢给 `start_process`，漏了 Jinja
    这一步，于是模板里的 `{{auto_logo()|...}}` 被原样当成文件名去打开，报
    `[Errno 22] Invalid argument: "{{auto_logo()|replace('\\', '/')}}"`。
    现象是「编辑器里一加步骤预览就崩，但正常预览没问题」——
    因为正常预览走的是 `render_template_json`，本来就会渲染。
    """
    if not pipeline:
        return pipeline
    text = json.dumps(pipeline, ensure_ascii=False)
    return json.loads(render_env().from_string(text).render(context))


def build_context(input_path: str, files=None) -> dict:
    """构造模板渲染上下文。"""
    p = Path(input_path)
    return {
        "exif": get_exif(input_path),
        "filename": p.stem,
        "file_dir": str(p.parent.absolute()).replace("\\", "/"),
        "file_path": str(p).replace("\\", "/"),
        "files": files or [],
    }
