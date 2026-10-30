# 构建与开发说明 · Building from source

本文说明如何从源码运行、打包便携版，以及可选的 ExifTool 安装。适用于 Windows 10/11 + Python 3.13+。

---

## 1. 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python desktop_app.py   # 原生桌面窗口（WebView2 + 托盘）
python app.py           # 浏览器模式，默认 http://127.0.0.1:15050
```

- 桌面窗口依赖 **WebView2 运行时**（Win10/11 通常已自带；空白窗口时安装
  [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)）。
- `pywebview` 在 Windows 上会自动携带 `pythonnet`。

## 2. 可选：安装 ExifTool

程序优先用 [ExifTool](https://exiftool.org/) 读写 EXIF；缺失时自动降级为 Pillow（常见字段仍可读，功能不中断）。

一键安装到项目本地 `exiftool/`：

```powershell
pwsh scripts/setup_exiftool.ps1
```

或手动下载 Windows 64 位包（`exiftool-XX.XX_64.zip`），把其中的 `exiftool(-k).exe` 重命名为 `exiftool.exe`，与 `exiftool_files\` 一起放进项目根目录的 `exiftool\` 文件夹。

> `exiftool/` 已被 `.gitignore` 忽略，不会进入仓库。ExifTool 采用 Perl Artistic 2.0 + GPL v1 双许可，分发其二进制时请遵守其许可证。

## 3. 目录与路径约定

程序区分两类根目录（见 `core/__init__.py`）：

| 常量 | 含义 | 源码模式 | 打包模式 |
|---|---|---|---|
| `BUNDLE_ROOT` | 只读资源根（config/static/templates/assets） | 项目根目录 | PyInstaller 解包临时目录 `sys._MEIPASS` |
| `DATA_ROOT` | 可写数据根（config 副本、input/output/logs） | 项目根目录 | exe 所在目录；不可写时回退 `%LOCALAPPDATA%\CameraWatermarkPro` |

资源查找顺序：`DATA_ROOT` 优先，`BUNDLE_ROOT` 兜底。首次运行会把内置 `config/` 播种到数据目录，**从不覆盖、从不删除**用户已有文件。

## 4. 打包（PyInstaller）

```powershell
pip install pyinstaller
```

### 4.1 文件夹版（onedir，启动快，推荐）

```powershell
pyinstaller --noconfirm --clean --windowed `
  --name CameraWatermarkPro `
  --icon assets\logo.ico `
  --add-data "config;config" `
  --add-data "static;static" `
  --add-data "templates;templates" `
  --add-data "assets;assets" `
  --hidden-import processor.filters `
  --hidden-import processor.generators `
  --hidden-import processor.mergers `
  --hidden-import pywebview.platforms.winforms `
  desktop_app.py
```

产物在 `dist\CameraWatermarkPro\`，整个文件夹一起分发。

### 4.2 单文件版（onefile，单文件好拷贝）

```powershell
pyinstaller --noconfirm --clean --windowed --onefile `
  --name CameraWatermarkPro `
  --icon assets\logo.ico `
  --add-data "config;config" `
  --add-data "static;static" `
  --add-data "templates;templates" `
  --add-data "assets;assets" `
  --hidden-import processor.filters `
  --hidden-import processor.generators `
  --hidden-import processor.mergers `
  --hidden-import pywebview.platforms.winforms `
  desktop_app.py
```

### 4.3 若要内置 ExifTool

先按第 2 节安装到项目本地 `exiftool/`，再给上面的命令追加：

```powershell
  --add-data "exiftool;exiftool" `
```

> 注意：内置 ExifTool（含整套 Perl 库）体积约 30 MB、近 500 个文件；是否需要内置请权衡。仓库默认不内置。

### 4.4 打包要点与常见坑

- **必须显式声明 hiddenimports**：`processor/__init__.py` 用 `importlib` 动态导入 `processor.filters / generators / mergers`，PyInstaller 静态分析追踪不到，漏了会在运行时报「没有可用处理器」。
- **`--windowed`** 隐藏控制台窗口；调试初期可去掉以查看日志。
- **单文件版启动慢、子进程效率低**：onefile 每次 spawn 子进程都要重新解包整个包（含 WebView2/.NET 运行时），故单文件版默认降低并行度。日常分发建议 onedir。
- **写权限探针**：程序用固定文件 `.cwp_write_probe` 判断目录可写（并发安全），放到 `C:\Program Files` 等只读目录会自动回退 `%LOCALAPPDATA%`。
- 打包后请用 `python e2e_packaged_test.py`（针对打包产物）或手动验证：窗口能开、预览能出、批处理能跑、托盘能退出。

## 5. 测试

```powershell
python selftest.py
python smoke_test.py
python test_features.py
python test_v340_features.py
python ui_verify.py      # 界面自动化，需 Chrome / Playwright
```
