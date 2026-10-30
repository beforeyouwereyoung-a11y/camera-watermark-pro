"""Jinja2 模板自定义函数：按百分比计算尺寸、自动匹配品牌 Logo。"""
import re

from jinja2 import Environment, pass_context

from core.configs import logos_dir


def _to_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


@pass_context
def vw(context, percent):
    """图片宽度的百分比 -> 像素（基于 EXIF 原始宽度）。"""
    exif = context.get("exif", {}) or {}
    return int(_to_int(exif.get("ImageWidth")) * float(percent) / 100)


@pass_context
def vh(context, percent):
    """图片高度的百分比 -> 像素（基于 EXIF 原始高度）。"""
    exif = context.get("exif", {}) or {}
    return int(_to_int(exif.get("ImageHeight")) * float(percent) / 100)


@pass_context
def vw_round(context, percent):
    """图片宽度的百分比 -> 像素，**四舍五入**（基于 EXIF 原始宽度）。

    与 `vw()` 的区别只有一个：`vw()` 是 `int()` 截断，这里是 `round()`。
    两者最多差 1px，但有两个实际影响：
      1. 设计稿里写的是 `round(W * x)`，用 `vw()` 转写会系统性偏小半像素；
      2. 截断会让「小分辨率输入」的比例比大分辨率略小一点（如 178/5568 =
         3.1968% vs 51/1600 = 3.1875%），跨分辨率一致性检查会留下毛刺。
    """
    exif = context.get("exif", {}) or {}
    return int(round(_to_int(exif.get("ImageWidth")) * float(percent) / 100))


# ---------------------------------------------------------------- Logo 匹配
#
# 资产名和 EXIF 里的厂商写法对不上是常态，所以在比之前先各自规范化：
#   资产 "leica_logo.png" -> "leicalogo"  -> 剥后缀 -> "leica"
#   Make "LEICA CAMERA AG" -> "leicacameraag"        -> "leica" ∈ "leicacameraag" ✓
_LOGO_KEY_STRIP = re.compile(r"[^a-z0-9]+")
_LOGO_SUFFIXES = ("logo", "brand", "mark")


def _logo_key(text) -> str:
    """归一化成「只含小写字母数字」的匹配键。"""
    return _LOGO_KEY_STRIP.sub("", str(text or "").lower())


def _strip_logo_suffix(stem: str) -> str:
    """剥掉资产名尾部的 logo / brand / mark（`leicalogo` -> `leica`）。"""
    for suffix in _LOGO_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)]
    return stem


@pass_context
def auto_logo(context, brand: str = None):
    """根据相机品牌自动匹配内置 Logo，找不到返回 None。

    ⚠️ 调用方要防「渲染成字符串 `'None'`」：模板里写
        "logo": "{{ (auto_logo() or '') | replace('\\\\', '/') }}"
    如果写成 `{{auto_logo()|replace('\\\\','/')}}`，`None` 会被 Jinja 的
    `replace` 过滤器 `soft_str()` 成字符串 `'None'`（**真值**），
    处理器就会去 `Image.open('None')`，每次出图刷一条 WARNING。

    匹配规则（v3.5.0 修）：
      1. 两侧都做**规范化**后再比 —— 只留字母数字、并剥掉资产名尾部的
         `logo` / `brand` / `mark` 后缀。这样 `leica_logo.png` 能命中
         `LEICA CAMERA AG`（旧规则里 `leica_logo` 不是 `leicacameraag`
         的子串，徕卡直接失配）。
      2. **Model 优先于 Make**。厂商在 Make 里写母公司时，真机上贴的是子品牌：
         `RICOH IMAGING` + `PENTAX K-3 Mark III` 应该命中 `pentax.png`，
         旧规则只比 Make，把理光的标贴到了宾得机身上。
      3. 资产目录按文件名排序遍历，先命中者胜。
    """
    exif = context.get("exif", {}) or {}
    make = brand or exif.get("Make") or "default"
    model = exif.get("CameraModelName") or exif.get("Model") or ""
    make_key, model_key = _logo_key(make), _logo_key(model)

    if not logos_dir.exists():
        return None

    fallback = None
    for f in sorted(logos_dir.iterdir()):
        if f.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        stem = _logo_key(f.stem)
        stem = _strip_logo_suffix(stem)
        if not stem:
            continue
        if stem in model_key:                    # 更具体，直接采纳
            return str(f.absolute()).replace("\\", "/")
        if stem in make_key and fallback is None:
            fallback = f
    return str(fallback.absolute()).replace("\\", "/") if fallback else None


@pass_context
def exif_or(context, *keys, default="-"):
    """按顺序取第一个非空 EXIF 字段。"""
    exif = context.get("exif", {}) or {}
    for k in keys:
        v = exif.get(k)
        if v not in (None, "", "0"):
            return v
    return default


# ---------------------------------------------------------------- 镜头型号精简
#
# 背景：镜头型号在 EXIF 里往往是一长串，例如
#   "NIKKOR Z DX 50-250mm f/4.5-6.3 VR"   共 34 字符
# 横构图时放得下，但竖构图下底栏变窄，整串会挤成 3~4 行、或者触发排版降级。
# 真正有信息量的只有「镜头名 + 焦段」，光圈之后的部分（光圈值、防抖、代次）
# 对照片本身没有辨识价值。
#
# 截断规则：括号注释先去掉 -> 在第一个光圈标记处截断 -> 兜底超长加省略号。
_LENS_NOTE = re.compile(r"[（(][^）)]*[）)]")
_LENS_APERTURE = re.compile(r"\s+[fF]\s*/?\s*\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?")
_LENS_MAX_LEN = 26


def lens_short(value, limit: int = _LENS_MAX_LEN):
    """把镜头型号精简成「镜头名 + 焦段」，便于窄画幅排版。

    >>> lens_short("NIKKOR Z DX 50-250mm f/4.5-6.3 VR")
    'NIKKOR Z DX 50-250mm'
    >>> lens_short("FE 24-70mm F2.8 GM II")
    'FE 24-70mm'
    >>> lens_short("RF24-105mm F4 L IS USM")
    'RF24-105mm'
    >>> lens_short(None)
    '-'
    """
    if value is None:
        return "-"
    text = str(value).strip()
    if not text:
        return "-"

    text = _LENS_NOTE.sub("", text)
    match = _LENS_APERTURE.search(text)
    if match:
        text = text[:match.start()]

    # 截断后可能留下尾部的空格、连字符、逗号
    text = text.strip().strip("-–—,;:·").strip()
    if not text:
        return "-"

    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


# ---------------------------------------------------------------- 机型拆分
#
# 「摄影作品展签」版面要求把机型拆成**两个独立文字对象**：
#     NIKON（粗体，品牌）  +  Z50 II（常规体，型号）
# 中间用 NIKON 的真实渲染宽度 + 固定间距定位，不能用空格凑。
# ⚠️ v3.5.0 修：品牌的第一权威来源是 **EXIF `Make`**，不是 `Model`。
#
#   旧模板写成 `exif.CameraModelName or exif.Make`，把 `Make` 当**兜底**，
#   而 `camera_brand()` 又只是「取首个词」—— 于是「Model 里不含品牌」的机型
#   全部拆坏。实测 11 台真实样张错 7 台：
#     SONY       / ILCE-7M3      -> 品牌「ILCE-7M3」 型号「-」
#     FUJIFILM   / X-T5          -> 品牌「X-T5」     型号「-」
#     Panasonic  / DC-S5M2       -> 品牌「DC-S5M2」  型号「-」
#     OM Digital / OM-1          -> 品牌「OM-1」     型号「-」
#     Hasselblad / X2D 100C      -> 品牌「X2D」      型号「100C」
#     Apple      / iPhone 15 Pro -> 品牌「iPhone」   型号「15 Pro」
#
# 尼康二代/三代机在 EXIF 里写成 `Z50_2` / `Z7_3`，且用下划线连接 ——
# 直接 `replace('_',' ')` 会得到 `Z50 2`，所以先还原成罗马数字。
_CAMERA_SERIAL = re.compile(r"_(\d+)$")
_CAMERA_ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V",
                 "6": "VI", "7": "VII", "8": "VIII", "9": "IX"}

# `Make` 里写的是**法人全称**，不是品牌名；这张表把全称收敛到品牌写法。
# 键 = 去掉所有非字母数字并小写后的串。表里没有的走「取第一个词」兜底。
_BRAND_CANONICAL = {
    "nikoncorporation": "NIKON", "nikon": "NIKON",
    "canoninc": "Canon", "canon": "Canon",
    "sony": "SONY", "sonycorporation": "SONY",
    "fujifilm": "FUJIFILM", "fujifilmcorporation": "FUJIFILM",
    "panasonic": "Panasonic", "panasoniccorporation": "Panasonic",
    "leicacameraag": "LEICA", "leica": "LEICA",
    "hasselblad": "Hasselblad", "hasselbladhb": "Hasselblad",
    "apple": "Apple",
    "ricohimaging": "RICOH", "ricoh": "RICOH", "ricohcompanyltd": "RICOH",
    "pentax": "PENTAX", "pentaxricohimaging": "PENTAX",
    "omdigitalsolutions": "OM SYSTEM", "omsystem": "OM SYSTEM",
    "olympus": "OLYMPUS", "olympuscorporation": "OLYMPUS",
    "sigmacorporation": "SIGMA", "sigma": "SIGMA",
    "tamron": "TAMRON", "dji": "DJI", "gopro": "GoPro",
    "google": "Google", "samsung": "SAMSUNG", "xiaomi": "Xiaomi",
    "huawei": "HUAWEI", "honor": "HONOR", "oppo": "OPPO",
    "vivo": "vivo", "oneplus": "OnePlus", "meizu": "MEIZU",
    "motorola": "motorola", "nothing": "Nothing",
}

# 子品牌：`Model` 里点名、但与 `Make` 不同 —— 真机上贴的就是**子品牌**的标。
#   · 理光映像出的 PENTAX 机身，机身贴的是 PENTAX；
#   · OM Digital Solutions 出的 OM-1 贴的是 OM SYSTEM（不是奥林巴斯标）；
#   · 松下的 LUMIX 子线，Logo 资产仍叫 panasonic.png，故只规整品牌文字。
_SUBBRANDS = (
    ("PENTAX", "PENTAX"),
    ("OM-", "OM SYSTEM"),
    ("OM ", "OM SYSTEM"),
    ("LUMIX", "Panasonic"),
)


def _clean_camera_text(value) -> str:
    """机型串归一化：`Z50_2` -> `Z50 II`，并把多空格压成一个。"""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    text = _CAMERA_SERIAL.sub(
        lambda m: " " + _CAMERA_ROMAN.get(m.group(1), m.group(1)), text)
    text = text.replace("_", " ").strip()
    return re.sub(r"\s+", " ", text)


def _norm_brand(value) -> str:
    """把 `Make` 的法人全称收敛成品牌写法；表里没有就取第一个词。"""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    key = _logo_key(text)
    if key in _BRAND_CANONICAL:
        return _BRAND_CANONICAL[key]
    words = text.split()
    first_key = _logo_key(words[0]) if words else ""
    if first_key in _BRAND_CANONICAL:
        return _BRAND_CANONICAL[first_key]
    return words[0] if words else ""


def split_camera(make, model=None):
    """把 EXIF 的机型信息拆成 (品牌, 型号)。

    两种调用形态：

    * ``split_camera(make, model)`` —— 推荐。品牌取 ``Make``，型号取 ``Model``。
    * ``split_camera("NIKON Z50_2")`` —— 兼容旧调用，单串里首个词当品牌。

    >>> split_camera("NIKON CORPORATION", "NIKON Z50_2")
    ('NIKON', 'Z50 II')
    >>> split_camera("SONY", "ILCE-7M3")
    ('SONY', 'ILCE-7M3')
    >>> split_camera("FUJIFILM", "X-T5")
    ('FUJIFILM', 'X-T5')
    >>> split_camera("Hasselblad", "X2D 100C")
    ('Hasselblad', 'X2D 100C')
    >>> split_camera("Apple", "iPhone 15 Pro")
    ('Apple', 'iPhone 15 Pro')
    >>> split_camera("RICOH IMAGING", "PENTAX K-3 Mark III")
    ('PENTAX', 'K-3 Mark III')
    >>> split_camera("OM Digital Solutions", "OM-1")
    ('OM SYSTEM', 'OM-1')
    >>> split_camera("NIKON Z50_2")
    ('NIKON', 'Z50 II')
    >>> split_camera("NIKON CORPORATION")
    ('NIKON', 'CORPORATION')
    >>> split_camera(None)
    ('-', '-')
    """
    if model is None:                       # 单串模式：整体当型号串
        make_text, model_text = "", _clean_camera_text(make)
    else:
        make_text, model_text = _clean_camera_text(make), _clean_camera_text(model)

    if not make_text and not model_text:
        return "-", "-"

    brand = _norm_brand(make_text)
    if not brand:                           # Make 缺失：退回型号串首个词
        brand = model_text.split(" ")[0] if model_text else ""

    upper = model_text.upper()
    for token, sub_brand in _SUBBRANDS:
        if upper.startswith(token):
            brand = sub_brand
            break

    # 型号剥掉与品牌同名的前缀（"PENTAX K-3 Mark III" -> "K-3 Mark III"）；
    # "OM-1" 与品牌 "OM SYSTEM" 不同名，保持原样。
    parts = model_text.split(" ")
    if parts and brand and parts[0].upper() == brand.upper():
        parts = parts[1:]
    model_out = " ".join(parts).strip() or "-"
    return brand or "-", model_out


def _camera_fields(context, value):
    """把各种调用形态归一成 `(Make, Model)` 交给 `split_camera`。

    模板推荐写 ``{{ camera_brand() }}`` / ``{{ camera_model() }}`` —— 函数自己
    从 exif 里取 ``Make`` + ``CameraModelName``（即 ``Model``）。

    兼容两种旧写法：
      * ``{{ camera_brand(exif.CameraModelName) }}`` —— 有 context 时，
        传进来的值当型号、`Make` 仍从 exif 取（品牌照样修得对）；
      * ``camera_brand("NIKON Z50_2")`` —— 直接调用，无 context，
        整串走单串模式。
    """
    if not hasattr(context, "get"):          # 旧签名：唯一入参就是机型串
        return None, (context if value is None else value)
    exif = context.get("exif", {}) or {}
    if value is not None:
        return exif.get("Make"), value
    # 注意 `or ""`：只有 Make 没有 Model 时必须进**两参**模式，
    # 否则 split_camera 会把整串 "NIKON CORPORATION" 当型号串，型号位渲染出
    # "CORPORATION"。
    model = exif.get("CameraModelName") or exif.get("Model") or ""
    return exif.get("Make"), model


@pass_context
def camera_brand(context, value=None):
    """品牌文字（以 `Make` 为准，`Model` 只作兜底）。"""
    return split_camera(*_camera_fields(context, value))[0]


@pass_context
def camera_model(context, value=None):
    """型号文字（品牌前缀已剥掉）。"""
    return split_camera(*_camera_fields(context, value))[1]


def register_filters(template) -> None:
    """给已有的 Jinja2 模板对象补挂自定义全局函数与过滤器。

    注意过滤器必须挂到 `template.environment.filters`：`Template` 本身没有
    `filters` 属性（只有 `globals` 是实例属性）。

    更要注意：裸 `Template(source)` 在**构造时就会编译**，而过滤器名是在编译期
    解析的 —— 因此对含自定义过滤器的模板，必须走 `render_env()`（先建好环境再
    编译），否则会抛 `No filter named '...'`。
    """
    for name, fn in _CUSTOM_GLOBALS.items():
        template.globals[name] = fn
    for name, fn in _CUSTOM_FILTERS.items():
        template.globals[name] = fn
        template.environment.filters[name] = fn


def _build_env() -> Environment:
    env = Environment(autoescape=False)
    for name, fn in _CUSTOM_GLOBALS.items():
        env.globals[name] = fn
    for name, fn in _CUSTOM_FILTERS.items():
        env.globals[name] = fn
        env.filters[name] = fn
    return env


# 所有自定义全局函数 / 过滤器集中登记一份。以前 register_filters 与 _build_env
# 各写一遍，加一个忘一处就会变成「模板能存不能跑」——v3.4.0 加 lens_short 时踩过。
_CUSTOM_GLOBALS = {
    "vh": vh,
    "vw": vw,
    "vwr": vw_round,
    "auto_logo": auto_logo,
}

_CUSTOM_FILTERS = {
    "lens_short": lens_short,
    "camera_brand": camera_brand,
    "camera_model": camera_model,
}


# 共享环境：过滤器在编译前就已就位，且自带已编译模板的 LRU 缓存。
RENDER_ENV = _build_env()


def render_env() -> Environment:
    """返回挂了全部自定义函数/过滤器的 Jinja2 环境。

    所有从字符串渲染模板的入口都应该用它，而不是裸 `Template(...)`。
    """
    return RENDER_ENV
