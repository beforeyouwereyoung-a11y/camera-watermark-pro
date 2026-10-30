"""配置与目录管理（全部基于 DATA_ROOT 的绝对路径，彻底摆脱工作目录依赖）。"""
import configparser
import tomllib
from pathlib import Path

from core import CONFIG_PATH, DATA_ROOT, PROJECT_INFO

# 可写资源目录（首次运行时会从内置资源播种）
CONFIG_DIR = DATA_ROOT / "config"
fonts_dir = CONFIG_DIR / "fonts"
logos_dir = CONFIG_DIR / "logos"
templates_dir = CONFIG_DIR / "templates"
PRESETS_DIR = CONFIG_DIR / "presets"

DEFAULT_INPUT = DATA_ROOT / "input"
DEFAULT_OUTPUT = DATA_ROOT / "output"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": "15050",
    "debug": "False",
    "input_folder": str(DEFAULT_INPUT),
    "output_folder": str(DEFAULT_OUTPUT),
    "override_existed": "False",
    "supported_file_suffixes": ".jpeg,.jpg,.png,.heic,.heif,.webp,.tif,.tiff",
    "quality": "100",
    "subsampling": "0",
    "output_format": "original",
    "long_edge": "0",
    "name_pattern": "{name}_{template}",
    "preserve_exif": "True",
    "crop_preset": "none",
    "open_output_when_done": "False",
    # ---- 界面 ----
    # light | dark | auto（跟随系统）
    "theme": "light",
    # ---- 性能 ----
    # serial | auto | 具体进程数（2/4/8…）
    "workers": "auto",
    # 多进程生效的最小批量（太少反而被进程启动开销拖慢）
    "workers_min_batch": "4",
    # EXIF 缓存条目上限
    "exif_cache_size": "2048",
    # 预览长边上限（越小越快，越大越清晰）
    "preview_max_edge": "1400",
    # ---- 自定义 Logo ----
    "logo_name": "",
    "logo_scale": "100",
    "logo_opacity": "100",
    "logo_corner": "bottom_right",
}

DEFAULT_TEMPLATE = "标准水印2"

# v3.4.0：命名规则默认值由 `{name}` 升级为 `{name}_{template}`。
# 旧值的坑：同一张图换模板后输出路径完全相同，会被判成「已存在」直接跳过，
# 用户必须换输出目录才能重新处理。
LEGACY_NAME_PATTERN = "{name}"


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    # 先灌入默认值，保证任何缺失项都有兜底
    config["DEFAULT"] = dict(DEFAULTS)
    config["render"] = {"template_name": DEFAULT_TEMPLATE}
    if CONFIG_PATH.exists():
        try:
            config.read(CONFIG_PATH, encoding="utf-8")
        except Exception:
            pass
    # 补全历史配置缺失的新键
    for key, value in DEFAULTS.items():
        if key not in config["DEFAULT"]:
            config["DEFAULT"][key] = value
    # 一次性迁移：只替换「还是旧默认值」的情况。
    # 用户自己改过的规则一律不动 —— 命名规则是用户的显式意图，不能覆盖。
    if str(config["DEFAULT"].get("name_pattern", "")).strip() == LEGACY_NAME_PATTERN:
        config["DEFAULT"]["name_pattern"] = DEFAULTS["name_pattern"]
    return config


def save_config(config: configparser.ConfigParser) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        config.write(f)


def load_project_info() -> dict:
    try:
        with open(PROJECT_INFO, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {"project": {"version": "1.0.0", "name": "camera-watermark-pro"}}


def ensure_dirs() -> None:
    for d in (fonts_dir, logos_dir, templates_dir, PRESETS_DIR, DEFAULT_INPUT, DEFAULT_OUTPUT):
        d.mkdir(parents=True, exist_ok=True)
