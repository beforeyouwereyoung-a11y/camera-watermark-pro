"""处理器参数元数据（供可视化模板编辑器使用）。

为什么单独写一份声明，而不是去解析处理器源码？

1. 处理器读参数用的是 `ctx.getint("blur_radius", 5)` 这类**动态键名**，
   静态解析拿不到「默认值 / 取值范围 / 中文名」，而且极易被重构打散。
2. 模板 JSON 里同一个键在不同处理器里语义不同（`height` 在 resize 里是
   像素高度，在 rich_text 里是字号），需要人工标注才能给出正确的控件。
3. 这份元数据是**纯展示层**：即使某个键没被登记，编辑器也会退化成
   通用「文本输入」，不会因为元数据缺失而崩掉。

字段说明（param）：
    key      参数名（写进模板 JSON 的键）
    label    界面上的中文名
    type     text | textarea | number | range | bool | color | select | font
             | logo | position | json
    default  默认值
    min/max/step   range / number 用
    options  select 用，[{value, label}]
    hint     输入框下面的小字说明
    advanced  True 表示折叠在「更多参数」里
"""

from __future__ import annotations

# ---------------------------------------------------------------- 枚举选项

POSITION_OPTIONS = [
    {"value": "top_left", "label": "左上"},
    {"value": "top_center", "label": "上中"},
    {"value": "top_right", "label": "右上"},
    {"value": "middle_left", "label": "左中"},
    {"value": "center", "label": "正中"},
    {"value": "middle_right", "label": "右中"},
    {"value": "bottom_left", "label": "左下"},
    {"value": "bottom_center", "label": "下中"},
    {"value": "bottom_right", "label": "右下"},
]

ALIGNMENT_OPTIONS = [
    {"value": "start", "label": "起始（左/上）"},
    {"value": "center", "label": "居中"},
    {"value": "end", "label": "末尾（右/下）"},
]

DIRECTION_OPTIONS = [
    {"value": "horizontal", "label": "水平"},
    {"value": "vertical", "label": "垂直"},
    {"value": "diagonal", "label": "对角线"},
    {"value": "radial", "label": "径向"},
]

EASING_OPTIONS = [
    {"value": "linear", "label": "线性"},
    {"value": "ease_in", "label": "缓入"},
    {"value": "ease_out", "label": "缓出"},
    {"value": "ease_in_out", "label": "缓入缓出"},
]


# ---------------------------------------------------------------- 元数据

# 说明：`tokens` 用来提示「这个参数支持 Jinja2 表达式 / 占位符」，
# 编辑器会在输入框下面显示一条小提示，减少用户写错模板的挫败感。
PROCESSOR_SCHEMA: dict[str, dict] = {
    # ============================ 滤镜 ============================
    "blur": {
        "label": "高斯模糊",
        "category": "filter",
        "desc": "整张图做高斯模糊，常用来生成「背景虚化」底图。",
        "params": [
            {"key": "blur_radius", "label": "模糊半径", "type": "range",
             "default": 5, "min": 0, "max": 80, "step": 1},
        ],
    },
    "resize": {
        "label": "缩放",
        "category": "filter",
        "desc": "按宽 / 高 / 比例缩放。宽高同时给则精确到像素。",
        "params": [
            {"key": "width", "label": "目标宽度(px)", "type": "number", "default": 0},
            {"key": "height", "label": "目标高度(px)", "type": "number", "default": 0},
            {"key": "scale", "label": "缩放比例", "type": "number", "default": 0,
             "step": 0.1, "hint": "例如 0.5 表示缩小一半；填了宽/高时此项忽略"},
        ],
    },
    "trim": {
        "label": "自动裁边",
        "category": "filter",
        "desc": "自动裁掉与四角同色的空白边缘。",
        "params": [
            {"key": "trim_left", "label": "裁左边", "type": "bool", "default": True},
            {"key": "trim_right", "label": "裁右边", "type": "bool", "default": True},
            {"key": "trim_top", "label": "裁上边", "type": "bool", "default": True},
            {"key": "trim_bottom", "label": "裁下边", "type": "bool", "default": True},
        ],
    },
    "margin": {
        "label": "加留白",
        "category": "filter",
        "desc": "给四周补留白，常用于做出「白边卡片」效果。",
        "params": [
            {"key": "left_margin", "label": "左留白", "type": "number", "default": 0,
             "tokens": True},
            {"key": "right_margin", "label": "右留白", "type": "number", "default": 0,
             "tokens": True},
            {"key": "top_margin", "label": "上留白", "type": "number", "default": 0,
             "tokens": True},
            {"key": "bottom_margin", "label": "下留白", "type": "number", "default": 0,
             "tokens": True, "hint": "支持 {{vh(2)}} 这类按画面高度自适应的写法"},
            {"key": "margin_color", "label": "留白颜色", "type": "color",
             "default": "white"},
        ],
    },
    "margin_with_ratio": {
        "label": "按比例补留白",
        "category": "filter",
        "desc": "把画面补到指定画幅比例（如 2.25:1 电影宽幅），多余部分留白。",
        "params": [
            {"key": "ratio", "label": "目标比例", "type": "text", "default": "2.25:1",
             "hint": "格式 宽:高，例如 3:2 / 16:9"},
            {"key": "margin_color", "label": "留白颜色", "type": "color",
             "default": "white", "advanced": True},
        ],
    },
    "watermark": {
        "label": "信息条水印",
        "category": "filter",
        "desc": "经典相机参数条：左上机型、左下镜头、右上参数、右下日期，可带 Logo。"
                "各分区用 multi_rich_text / rich_text 子配置描述，属高级用法。",
        "params": [
            {"key": "color", "label": "文字底色", "type": "color", "default": "white"},
            {"key": "delimiter_color", "label": "分隔条颜色", "type": "color",
             "default": "#ffffff00", "advanced": True},
            {"key": "left_margin", "label": "左间距", "type": "number", "default": 100,
             "tokens": True},
            {"key": "right_margin", "label": "右间距", "type": "number", "default": 100,
             "tokens": True},
            {"key": "top_margin", "label": "上间距", "type": "number", "default": 100,
             "tokens": True},
            {"key": "bottom_margin", "label": "下间距", "type": "number", "default": 100,
             "tokens": True},
            {"key": "right_logo", "label": "右侧 Logo", "type": "logo", "default": "",
             "hint": "一般写 {{auto_logo()}} 由程序按品牌自动匹配"},
            {"key": "left_top", "label": "左上区块", "type": "json", "advanced": True},
            {"key": "left_bottom", "label": "左下区块", "type": "json", "advanced": True},
            {"key": "right_top", "label": "右上区块", "type": "json", "advanced": True},
            {"key": "right_bottom", "label": "右下区块", "type": "json", "advanced": True},
            # 「装裱排版」新增的开关（v3.5.0），默认值 = 旧行为
            {"key": "edge_align", "label": "内容对齐照片边缘", "type": "bool",
             "default": False, "advanced": True,
             "hint": "开启后底栏文字的左右参考线直接对齐照片左右边界；"
                     "关闭时沿用旧的内缩 2% 画布宽"},
            {"key": "right_inline", "label": "右侧单行流式", "type": "bool",
             "default": False, "advanced": True,
             "hint": "开启后右侧改为「日期 … 间距 … Logo」单行，Logo 贴照片右边界；"
                     "关闭时沿用旧的 Logo 垂直居中占位 + 竖分隔线"},
            {"key": "right_logo_ratio", "label": "Logo 相对主标题比例", "type": "number",
             "default": 1.0, "advanced": True,
             "hint": "仅「右侧单行流式」生效；1.0 = 与左侧主标题等高"},
            {"key": "right_gap", "label": "Logo 与日期间距(px)", "type": "number",
             "default": 0, "advanced": True,
             "hint": "仅「右侧单行流式」生效；0 表示按照片宽度自动取值"},
        ],
    },
    "gallery_caption": {
        "label": "摄影作品展签",
        "category": "filter",
        "desc": "固定比例的展签版式：白卡纸留白 + 底栏「品牌/型号 + EXIF」与「日期 + 官方 Logo」。"
                "所有尺寸都按照片宽度 W 的百分比计算，横竖构图比例一致。"
                "照片本身不做任何处理（不圆角、不投影、不描边、不裁切、不调色）。",
        "params": [
            {"key": "background", "label": "背景色", "type": "color", "default": "#FFFFFF",
             "hint": "展签底色，通常纯白"},
            {"key": "pad_x", "label": "左右留白", "type": "number", "default": 0,
             "tokens": True, "hint": "建议写 {{vwr(2.5)}}；0 表示按照片宽度自动取值"},
            {"key": "pad_top", "label": "顶部留白", "type": "number", "default": 0,
             "tokens": True, "hint": "建议写 {{vwr(2.5)}}"},
            {"key": "footer_height", "label": "底栏高度", "type": "number", "default": 0,
             "tokens": True, "hint": "建议写 {{vwr(11)}}"},
            {"key": "brand_text", "label": "品牌文字", "type": "text", "default": "",
             "tokens": True,
             "hint": "建议写 {{ camera_brand() }}：以 EXIF Make 为准、清洗厂商全称，"
                     "粗体绘制"},
            {"key": "model_text", "label": "型号文字", "type": "text", "default": "",
             "tokens": True,
             "hint": "建议写 {{ camera_model() }}：取 Model 并剥掉品牌前缀，常规体绘制"},
            {"key": "title_font_size", "label": "标题字号", "type": "number", "default": 0,
             "tokens": True, "hint": "PIL 字号（em 像素），建议 {{vwr(3.2)}}"},
            {"key": "meta_text", "label": "参数文字", "type": "text", "default": "",
             "tokens": True},
            {"key": "meta_font_size", "label": "参数字号", "type": "number", "default": 0,
             "tokens": True, "hint": "建议 {{vwr(1.7)}}"},
            {"key": "date_text", "label": "日期文字", "type": "text", "default": "",
             "tokens": True},
            {"key": "logo", "label": "右侧 Logo", "type": "logo", "default": "",
             "hint": "建议写 {{ (auto_logo() or '')|replace('\\\\', '/') }} —— "
                     "`or ''` 不能省：None 经 replace 会变成字符串 'None'，"
                     "判空失效后每张图刷一条警告日志"},
            {"key": "logo_size", "label": "Logo 尺寸", "type": "number", "default": 0,
             "tokens": True,
             "hint": "徽标类（近方形）资产的容器边长，建议 {{vwr(5)}}。"
                     "资产四周白边会先被自动裁掉"},
            {"key": "logo_size_wide", "label": "Logo 尺寸（字标）", "type": "number",
             "default": 0, "tokens": True,
             "hint": "字标类（细长）资产的容器边长，建议 {{vwr(9)}} —— "
                     "同一个方框里字标的可见高度只有徽标的十几分之一"},
            {"key": "logo_aspect_wide", "label": "字标判定宽高比", "type": "number",
             "default": 2.5,
             "hint": "裁白边后的宽高比 ≥ 此值就算字标，改用「Logo 尺寸（字标）」"},
            {"key": "date_logo_gap", "label": "日期与 Logo 间距", "type": "number",
             "default": 0, "tokens": True, "hint": "建议 {{vwr(2)}}"},
            {"key": "brand_font", "label": "品牌字体", "type": "font",
             "default": "Roboto-Bold.ttf", "advanced": True},
            {"key": "brand_color", "label": "品牌颜色", "type": "color",
             "default": "#111111", "advanced": True},
            {"key": "model_font", "label": "型号字体", "type": "font",
             "default": "Roboto-Regular.ttf", "advanced": True},
            {"key": "model_color", "label": "型号颜色", "type": "color",
             "default": "#222222", "advanced": True},
            {"key": "title_gap", "label": "品牌与型号间距", "type": "number", "default": 0,
             "tokens": True, "advanced": True,
             "hint": "建议 {{vwr(1.4)}}；按品牌真实 ink 宽度定位，不用空格凑"},
            {"key": "title_y_ratio", "label": "标题纵向位置", "type": "number",
             "default": 0.20, "advanced": True,
             "hint": "占底栏高度的比例，0.20 = 底栏自上而下 20% 处"},
            {"key": "meta_font", "label": "参数字体", "type": "font",
             "default": "Roboto-Regular.ttf", "advanced": True},
            {"key": "meta_color", "label": "参数颜色", "type": "color",
             "default": "#747474", "advanced": True},
            {"key": "meta_y_ratio", "label": "参数纵向位置", "type": "number",
             "default": 0.62, "advanced": True,
             "hint": "占底栏高度的比例，0.62 = 底栏自上而下 62% 处"},
            {"key": "date_font", "label": "日期字体", "type": "font",
             "default": "Roboto-Regular.ttf", "advanced": True},
            {"key": "date_font_size", "label": "日期字号", "type": "number", "default": 0,
             "tokens": True, "advanced": True,
             "hint": "0 表示与参数文字同字号"},
            {"key": "date_color", "label": "日期颜色", "type": "color",
             "default": "#858585", "advanced": True},
        ],
    },
    "watermark_with_timestamp": {
        "label": "右下角时间戳",
        "category": "filter",
        "desc": "在右下角叠一行文字（通常是时间），属便捷封装。",
        "params": [
            {"key": "text", "label": "文字内容", "type": "text", "default": "",
             "tokens": True},
            {"key": "height", "label": "字号", "type": "number", "default": 0,
             "tokens": True, "hint": "0 表示按画面高度自动取值"},
            {"key": "color", "label": "颜色", "type": "color", "default": "white"},
            {"key": "font_path", "label": "字体", "type": "font", "default": ""},
        ],
    },
    "rounded_corner": {
        "label": "圆角",
        "category": "filter",
        "desc": "给图片切圆角（背景模糊模板必用）。",
        "params": [
            {"key": "border_radius", "label": "圆角半径", "type": "number", "default": 10,
             "tokens": True, "hint": "支持 {{vh(1)}} 这类按比例写法"},
        ],
    },
    "shadow": {
        "label": "投影",
        "category": "filter",
        "desc": "在图片外围加一圈柔和阴影，让卡片浮起来。",
        "params": [
            {"key": "shadow_color", "label": "阴影颜色", "type": "color",
             "default": "black"},
            {"key": "shadow_radius", "label": "阴影半径", "type": "range",
             "default": 30, "min": 0, "max": 200, "step": 1},
            {"key": "falloff", "label": "衰减指数", "type": "number", "default": 1.5,
             "step": 0.1, "advanced": True},
        ],
    },
    "outline": {
        "label": "细描边",
        "category": "filter",
        "desc": "沿边缘描一圈极细的线，让浅色照片在白色画框里也能立住。",
        "params": [
            {"key": "outline_width", "label": "线宽(px)", "type": "number", "default": 1},
            {"key": "outline_color", "label": "线颜色", "type": "color", "default": "#0000001A"},
            {"key": "border_radius", "label": "圆角半径(px)", "type": "number", "default": 0,
             "tokens": True, "hint": "与「圆角」步骤保持一致，否则边角会露出来"},
        ],
    },
    "crop": {
        "label": "精确裁切",        "category": "filter",
        "desc": "居中裁到指定像素尺寸，可用 offset 微调位置。",
        "params": [
            {"key": "width", "label": "裁切宽度", "type": "number", "default": 0},
            {"key": "height", "label": "裁切高度", "type": "number", "default": 0},
            {"key": "offset", "label": "偏移 [x, y]", "type": "text",
             "default": "[0, 0]", "advanced": True},
        ],
    },
    "opacity": {
        "label": "整体透明度",
        "category": "filter",
        "desc": "压低整张图的透明度，常用于 Logo / 水印做淡化。",
        "params": [
            {"key": "opacity", "label": "不透明度", "type": "range", "default": 1.0,
             "min": 0, "max": 1, "step": 0.05},
        ],
    },
    "rotate": {
        "label": "旋转",
        "category": "filter",
        "desc": "按角度旋转画布，可选填充背景色。",
        "params": [
            {"key": "angle", "label": "角度", "type": "range", "default": 0,
             "min": -180, "max": 180, "step": 1},
            {"key": "expand", "label": "扩大画布容纳", "type": "bool", "default": True},
            {"key": "background", "label": "背景填充", "type": "color",
             "default": "#00000000", "advanced": True},
        ],
    },
    "tile_watermark": {
        "label": "平铺水印",
        "category": "filter",
        "desc": "把文字按网格铺满整张图，防盗图首选。",
        "params": [
            {"key": "text", "label": "水印文字", "type": "text", "default": "© 我的作品",
             "tokens": True},
            {"key": "angle", "label": "旋转角度", "type": "range", "default": -30,
             "min": -90, "max": 90, "step": 1},
            {"key": "opacity", "label": "不透明度", "type": "range", "default": 0.18,
             "min": 0.02, "max": 1, "step": 0.02},
            {"key": "gap_x", "label": "横向间距", "type": "number", "default": 0,
             "hint": "0 表示按画面宽度自动取值"},
            {"key": "gap_y", "label": "纵向间距", "type": "number", "default": 0},
            {"key": "height", "label": "字号", "type": "number", "default": 0,
             "tokens": True},
            {"key": "color", "label": "颜色", "type": "color", "default": "white"},
            {"key": "font_path", "label": "字体", "type": "font", "default": ""},
        ],
    },
    "simple_text_watermark": {
        "label": "单行文字水印",
        "category": "filter",
        "desc": "九宫格定位的一行文字，位置 / 字号 / 描边都可调。",
        "params": [
            {"key": "text", "label": "水印文字", "type": "text", "default": "",
             "tokens": True},
            {"key": "position", "label": "位置", "type": "position",
             "default": "bottom_right"},
            {"key": "height", "label": "字号", "type": "number", "default": 0,
             "tokens": True},
            {"key": "opacity", "label": "不透明度", "type": "range", "default": 0.85,
             "min": 0.05, "max": 1, "step": 0.05},
            {"key": "angle", "label": "旋转", "type": "range", "default": 0,
             "min": -90, "max": 90, "step": 1},
            {"key": "color", "label": "文字颜色", "type": "color", "default": "white"},
            {"key": "stroke_color", "label": "描边颜色", "type": "color",
             "default": "black", "advanced": True},
            {"key": "stroke_width", "label": "描边宽度", "type": "range", "default": 0,
             "min": 0, "max": 10, "step": 1, "advanced": True},
            {"key": "margin", "label": "边距", "type": "number", "default": 0,
             "advanced": True},
            {"key": "font_path", "label": "字体", "type": "font", "default": ""},
        ],
    },

    # ============================ 生成器 ============================
    "solid_color": {
        "label": "纯色块",
        "category": "generator",
        "desc": "生成一张纯色画布，可作为留白 / 分隔元素。",
        "params": [
            {"key": "width", "label": "宽度", "type": "number", "default": 100},
            {"key": "height", "label": "高度", "type": "number", "default": 100},
            {"key": "color", "label": "颜色", "type": "color", "default": "white"},
        ],
    },
    "gradient_color": {
        "label": "渐变块",
        "category": "generator",
        "desc": "生成线性 / 径向渐变画布。",
        "params": [
            {"key": "width", "label": "宽度", "type": "number", "default": 100},
            {"key": "height", "label": "高度", "type": "number", "default": 100},
            {"key": "start_color", "label": "起始色", "type": "color",
             "default": "#2563eb"},
            {"key": "end_color", "label": "结束色", "type": "color",
             "default": "#7c3aed"},
            {"key": "direction", "label": "方向", "type": "select",
             "default": "horizontal", "options": DIRECTION_OPTIONS},
            {"key": "interpolate_method", "label": "插值曲线", "type": "select",
             "default": "linear", "options": EASING_OPTIONS, "advanced": True},
        ],
    },
    "rich_text": {
        "label": "单段文字",
        "category": "generator",
        "desc": "生成一段文字图像，是模板里最常用的积木。",
        "params": [
            {"key": "text", "label": "文字内容", "type": "textarea", "default": "",
             "tokens": True},
            {"key": "height", "label": "字号", "type": "number", "default": 100,
             "tokens": True, "hint": "支持 {{vh(3)}} 按画面高度自适应"},
            {"key": "color", "label": "颜色", "type": "color", "default": "black"},
            {"key": "font_path", "label": "字体", "type": "font", "default": ""},
            {"key": "is_bold", "label": "加粗", "type": "bool", "default": False},
            {"key": "trim", "label": "自动裁边", "type": "bool", "default": False},
        ],
    },
    "multi_rich_text": {
        "label": "多段富文本",
        "category": "generator",
        "desc": "把多段文字按对齐方式拼成一块，例如「机型 + 镜头」。",
        "params": [
            {"key": "text_segments", "label": "文字片段", "type": "json",
             "hint": "数组，每项形如 {\"text\": \"...\", \"color\": \"#000\", "
                     "\"is_bold\": true}"},
            {"key": "text_alignment", "label": "段内对齐", "type": "select",
             "default": "start", "options": ALIGNMENT_OPTIONS},
            {"key": "text_spacing", "label": "段间距", "type": "number", "default": 10},
            {"key": "height", "label": "字号", "type": "number", "default": 100,
             "tokens": True},
        ],
    },
    "image": {
        "label": "载入图片",
        "category": "generator",
        "desc": "从磁盘载入一张图（Logo / 边框）作为元素。",
        "params": [
            {"key": "path", "label": "图片路径", "type": "logo", "default": ""},
        ],
    },

    # ============================ 合并器 ============================
    "alignment": {
        "label": "叠加对齐",
        "category": "merger",
        "desc": "把前面几步产生的图层按九宫格叠到画布上。",
        "params": [
            {"key": "horizontal_alignment", "label": "水平对齐", "type": "select",
             "default": "center", "options": ALIGNMENT_OPTIONS},
            {"key": "vertical_alignment", "label": "垂直对齐", "type": "select",
             "default": "center", "options": ALIGNMENT_OPTIONS},
            {"key": "offsets", "label": "偏移量", "type": "text", "default": "[]",
             "hint": "形如 [[0,0],[10,10]]，与图层顺序一一对应"},
            {"key": "weights", "label": "层级权重", "type": "text", "default": "[]",
             "advanced": True},
            {"key": "background", "label": "底色", "type": "color",
             "default": "#ffffff00", "advanced": True},
        ],
    },
    "concat": {
        "label": "顺序拼接",
        "category": "merger",
        "desc": "把多个图层按水平 / 垂直方向依次排开。",
        "params": [
            {"key": "direction", "label": "拼接方向", "type": "select",
             "default": "horizontal", "options": DIRECTION_OPTIONS[:2]},
            {"key": "alignment", "label": "交叉轴对齐", "type": "select",
             "default": "end", "options": ALIGNMENT_OPTIONS},
            {"key": "spacing", "label": "间距", "type": "number", "default": 10},
            {"key": "background", "label": "底色", "type": "color",
             "default": "#ffffff00", "advanced": True},
        ],
    },
}

# 处理器执行顺序的语义提示（编辑器用来解释「为什么顺序很重要」）
CATEGORY_META = {
    "generator": {"label": "生成器", "color": "#7c3aed",
                  "desc": "凭空造出新图层（文字、色块、图片）"},
    "filter": {"label": "滤镜", "color": "#2563eb",
               "desc": "对当前图层做加工（模糊、圆角、阴影、水印）"},
    "merger": {"label": "合并器", "color": "#0891b2",
               "desc": "把多个图层合成一张（叠加、拼接）"},
}


def _fallback_entry(name: str) -> dict:
    """未登记的处理器：给出一个可用的兜底条目，保证编辑器不空窗。"""
    return {
        "label": name,
        "category": "filter",
        "desc": "未登记的处理器，参数请参考模板 JSON 手工填写。",
        "params": [],
    }


def get_processor_schema(name: str) -> dict:
    entry = PROCESSOR_SCHEMA.get(name) or _fallback_entry(name)
    return {"name": name, **entry}


def list_processor_schema() -> list:
    """返回全部元数据，并标注该处理器是否真的在注册表里可用。

    这样编辑器可以隐藏「文档里有、但当前构建没编进去」的处理器。
    """
    try:
        from processor.core import get_all_processors
        available = set(get_all_processors().keys())
    except Exception:  # noqa: BLE001
        available = set(PROCESSOR_SCHEMA.keys())

    names = sorted(set(PROCESSOR_SCHEMA.keys()) | available)
    result = []
    for name in names:
        item = get_processor_schema(name)
        item["available"] = name in available
        result.append(item)
    return result


def default_config(name: str) -> dict:
    """按元数据生成一份默认的处理器配置（用于「添加步骤」）。"""
    schema = get_processor_schema(name)
    config: dict = {"processor_name": name}
    for param in schema.get("params", []):
        if param.get("advanced"):
            continue
        if "default" in param and param["default"] not in ("", None):
            config[param["key"]] = param["default"]
    return config
