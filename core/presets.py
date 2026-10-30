"""预设方案：把「一整套处理参数」存成一个文件，一键切换。

设计取舍
--------
* 预设是**参数快照**，不是模板。模板只描述水印长什么样；预设还要带上
  画幅裁切、输出格式、命名规则、Logo 微调等一整套「场景配置」。
* 每个预设一个 JSON 文件（`config/presets/<名称>.json`），而不是塞进
  config.ini。好处：用户可以直接把文件拷给同事；损坏一个不影响其他；
  也不会把 ini 撑成一坨难以手工维护的键值堆。
* 读取时做**字段白名单**过滤：即使预设文件被手改出多余字段，也不会污染
  配置对象。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.configs import PRESETS_DIR
from core.logger import logger

# 允许出现在预设里的字段（白名单）
PRESET_FIELDS = (
    "mode",              # template | simple
    "template_name",
    "crop_preset",
    "output_format",
    "name_pattern",
    "quality",
    "long_edge",
    "preserve_exif",
    "override_existed",
    "simple",            # 自定义文字水印参数（字号为画面高度百分比，与界面一致）
    "logo",              # 自定义 Logo 微调 {logo_name, logo_scale, logo_opacity, logo_corner}
)

# Windows 文件名不允许 \ / : * ? " < > |。
# 直接替换成下划线会把「小红书 3:4」变成「小红书 3_4」很难看，
# 所以优先换成视觉相近的全角字符，实在不行才用下划线。
_ILLEGAL_MAP = {
    "\\": "／", "/": "／", ":": "：", "*": "＊", "?": "？",
    '"': "＂", "<": "＜", ">": "＞", "|": "｜",
}


def _safe_name(name: str) -> str:
    """把预设名清洗成安全的文件名，尽量保留可读性。"""
    text = str(name or "").strip()
    for bad, good in _ILLEGAL_MAP.items():
        text = text.replace(bad, good)
    text = "".join(ch for ch in text if ord(ch) >= 32)
    text = text.strip(". ")
    return text[:64] or "未命名预设"


def _preset_path(name: str) -> Path:
    return PRESETS_DIR / f"{_safe_name(name)}.json"


def _sanitize(data: dict) -> dict:
    out = {}
    for key in PRESET_FIELDS:
        if key in data and data[key] is not None:
            out[key] = data[key]
    return out


def list_presets() -> list:
    """返回全部预设（按名称排序）。"""
    if not PRESETS_DIR.exists():
        return []
    items = []
    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            items.append({
                "name": path.stem,
                "desc": payload.get("desc", ""),
                "config": _sanitize(payload.get("config") or {}),
                "builtin": bool(payload.get("builtin", False)),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"预设 {path.name} 读取失败: {e}")
    return items


def get_preset(name: str) -> dict | None:
    path = _preset_path(name)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"预设 {name} 解析失败: {e}")
        return None
    return {
        "name": path.stem,
        "desc": payload.get("desc", ""),
        "config": _sanitize(payload.get("config") or {}),
        "builtin": bool(payload.get("builtin", False)),
    }


def save_preset(name: str, config: dict, desc: str = "") -> dict:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _preset_path(name)
    payload = {
        "desc": str(desc or "")[:120],
        "config": _sanitize(config or {}),
    }
    # 内置预设被用户覆盖时，保留 builtin 标记，方便界面区分
    existing = get_preset(name)
    if existing and existing.get("builtin"):
        payload["builtin"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"name": path.stem, "desc": payload["desc"],
            "config": payload["config"], "builtin": payload.get("builtin", False)}


def delete_preset(name: str) -> bool:
    path = _preset_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------- 内置预设

BUILTIN_PRESETS = [
    {
        "name": "朋友圈竖版 4:5",
        "desc": "4:5 竖构图 + 标准水印 + 长边 2000，适合朋友圈 / 小红书",
        "config": {
            "mode": "template",
            "template_name": "标准水印2",
            "crop_preset": "4:5",
            "output_format": "jpg",
            "quality": 92,
            "long_edge": 2000,
            "name_pattern": "{name}",
            "preserve_exif": True,
            "override_existed": False,
        },
    },
    {
        "name": "小红书 3:4 高清",
        "desc": "3:4 竖版 + 长边 2400 + 质量 95，兼顾清晰度与体积",
        "config": {
            "mode": "template",
            "template_name": "标准水印2",
            "crop_preset": "3:4",
            "output_format": "jpg",
            "quality": 95,
            "long_edge": 2400,
            "name_pattern": "{name}",
            "preserve_exif": True,
            "override_existed": False,
        },
    },
    {
        "name": "横版宽幅 16:9",
        "desc": "16:9 横构图 + 长边 2560，适合公众号头图 / 视频封面",
        "config": {
            "mode": "template",
            "template_name": "右下角参数",
            "crop_preset": "16:9",
            "output_format": "jpg",
            "quality": 95,
            "long_edge": 2560,
            "name_pattern": "{name}",
            "preserve_exif": True,
            "override_existed": False,
        },
    },
    {
        "name": "原图归档（带日期前缀）",
        "desc": "不裁切、保持原格式，文件名前加拍摄日期，方便按时间归档",
        "config": {
            "mode": "template",
            "template_name": "标准水印",
            "crop_preset": "none",
            "output_format": "original",
            "quality": 100,
            "long_edge": 0,
            "name_pattern": "{date}_{name}",
            "preserve_exif": True,
            "override_existed": False,
        },
    },
    {
        "name": "防盗图平铺水印",
        "desc": "平铺自定义文字 + 长边 1600，出图轻量、带满屏水印",
        "config": {
            "mode": "simple",
            "crop_preset": "none",
            "output_format": "jpg",
            "quality": 88,
            "long_edge": 1600,
            "name_pattern": "{name}",
            "preserve_exif": False,
            "override_existed": False,
            "simple": {
                "text": "© {date} 我的作品",
                "position": "bottom_right",
                "height": 3.5,
                "opacity": 18,
                "angle": -30,
                "color": "#ffffff",
                "strokeColor": "#000000",
                "strokeWidth": 0,
                "tile": True,
            },
        },
    },
    {
        "name": "网页轻量版",
        "desc": "长边 1600 + WebP + 质量 82，体积最小，适合发网页",
        "config": {
            "mode": "template",
            "template_name": "标准水印2",
            "crop_preset": "none",
            "output_format": "webp",
            "quality": 82,
            "long_edge": 1600,
            "name_pattern": "{name}",
            "preserve_exif": False,
            "override_existed": False,
        },
    },
]


def seed_builtin_presets(force: bool = False) -> int:
    """首次运行时写入内置预设（已存在的不覆盖，除非 force=True）。"""
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    created = 0
    for item in BUILTIN_PRESETS:
        path = _preset_path(item["name"])
        if path.exists() and not force:
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"desc": item["desc"], "config": item["config"],
                       "builtin": True}, f, ensure_ascii=False, indent=2)
        created += 1
    if created:
        logger.info(f"已初始化 {created} 个内置预设方案")
    return created
