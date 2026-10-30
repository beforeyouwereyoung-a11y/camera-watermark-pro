# 架构说明 · Architecture

本文概述 **相机水印工具 Pro** 的整体结构与关键数据流，帮助新贡献者快速定位代码。

## 1. 总体结构

```
┌───────────────────────────────────────────────────────────────┐
│                        界面层 (前端)                            │
│  templates/index.html + static/app.js + static/app.css         │
│  · 三栏布局：预设/设置 · 待处理照片 · 效果预览                   │
│  · 通过 REST API 与后端交互，SSE 接收处理进度                    │
└───────────────▲───────────────────────────────┬───────────────┘
                │ HTTP / SSE (127.0.0.1)         │
┌───────────────┴───────────────────────────────▼───────────────┐
│                     应用层 (Flask)  app.py                      │
│  REST API：meta / config / file / thumb / preview / process /   │
│  templates / presets / processors / logo / font / perf / drop…  │
└───────────────▲───────────────────────────────┬───────────────┘
                │                               │
┌───────────────┴──────────────┐  ┌─────────────▼───────────────┐
│     渲染管线 processor/       │  │        基础设施 core/        │
│  schema → filters →          │  │  configs 路径/配置           │
│  generators → mergers        │  │  util    EXIF 读取/文件扫描   │
│  （自动注册的处理器集合）      │  │  imaging 图像读写/EXIF 回写   │
│                              │  │  jobs    批处理任务/进度       │
│                              │  │  worker  子进程渲染单元        │
│                              │  │  perf    并行度/性能自检       │
│                              │  │  logger  日志                │
└──────────────────────────────┘  └─────────────────────────────┘
```

## 2. 两种运行形态

| 入口 | 说明 |
|---|---|
| `desktop_app.py` | 桌面入口。启动 Flask（后台线程）→ 打开 WebView2 原生窗口 → 注册系统托盘与单实例锁；窗口关闭收进托盘 |
| `app.py` | 纯 Flask 服务（`python app.py`），浏览器访问 `http://127.0.0.1:15050` |

两者共享同一套 Flask 应用与渲染管线，仅窗口承载方式不同。

## 3. 关键流程

### 3.1 实时预览

1. 前端把当前参数（模板 + 覆盖项）POST 到 `/api/v1/preview/pipeline`；
2. 后端用 `processor` 对一张样图跑一遍管线生成预览；
3. 前端用 `/api/v1/preview/meta` 与缩略图展示；对比模式再叠加原图。

### 3.2 批量处理

1. `/api/v1/process/start` 创建任务（job），返回 `job_id`；
2. `core/jobs.py` 负责调度：父进程先读 EXIF，再按 `core/perf.py` 的并行度分发到多个 `core/worker.py` 子进程；
3. 每个子进程对单张照片跑 `processor` 管线，输出到目标目录（含命名规则、覆盖策略、EXIF 回写）；
4. 前端通过 `/api/v1/process/stream/<job_id>`（SSE）接收进度，`/api/v1/process/cancel/<job_id>` 取消，`/api/v1/process/report/<job_id>` 取汇总。

### 3.3 模板 / 预设的读写

- 模板：`config/templates/*.json`，经 `/api/v1/templates`、`/api/v1/template` 增删改查；`processor/schema.py` 定义了参数结构（含 `tokens` 提示支持 Jinja2 表达式）。
- 预设：`config/presets/*.json`，一键切换整套参数。

## 4. 渲染管线（processor/）

| 模块 | 职责 |
|---|---|
| `schema.py` | 参数 Schema：每个处理器参数的名称、类型、默认值、提示 |
| `filters.py` | 图像滤镜类处理器（圆角、阴影、描边、模糊、裁切、调色等） |
| `generators.py` | 内容生成类处理器（EXIF 信息条、品牌/型号文本、Logo 等） |
| `mergers.py` | 合成类处理器（把生成/滤镜结果合到画布） |
| `core.py` | `ImageProcessor` 基类、注册表、执行引擎 |
| `types.py` | 管线用到的数据/类型定义 |

`processor/__init__.py` 在导入时通过 `importlib` **自动发现并注册**所有具体处理器子类（打包时需在 hiddenimports 显式声明三个模块，见 BUILDING.md）。

## 5. 路径与便携化设计

见 `core/__init__.py` 的 `BUNDLE_ROOT` / `DATA_ROOT` 双根设计，以及 `core/configs.py` 的「默认值兜底 + 首次播种 + 从不覆盖用户文件」策略。这是免安装便携与升级安全的基础。

## 6. 扩展点

- **新增处理器**：在 `processor/filters.py` 或 `generators.py` 中继承 `ImageProcessor` 并实现接口，注册表会自动收录；在 `schema.py` 补参数定义。
- **新增模板**：在 `config/templates/` 放 JSON（可用可视化编辑器导出）。
- **新增预设**：在 `config/presets/` 放 JSON。
- **新增品牌 Logo**：把图片放进 `config/logos/`，文件名含品牌关键字即可命中。
