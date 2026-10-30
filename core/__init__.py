"""
统一路径管理。

设计目标：让「源码运行」和「单文件 exe 运行」使用一致的路径语义，
并实现真正的免安装便携化（配置与资源跟随程序目录）。

- BUNDLE_ROOT : 只读资源根目录
    * 源码模式 = 项目根目录
    * 打包模式 = PyInstaller 解包出的临时目录 (sys._MEIPASS)
- DATA_ROOT   : 可写数据根目录
    * 优先使用 exe 所在目录（真正便携，可整个拷到 U 盘）
    * 若该目录不可写，回退到 %LOCALAPPDATA%/CameraWatermarkPro

资源查找顺序：DATA_ROOT 优先，BUNDLE_ROOT 兜底。
"""
import os
import shutil
import sys
import time
from pathlib import Path

APP_NAME = "CameraWatermarkPro"
APP_DISPLAY_NAME = "相机水印工具 Pro"

IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    # 单文件模式下，PyInstaller 会把 datas 解包到临时目录
    BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    EXE_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_ROOT = Path(__file__).resolve().parent.parent
    EXE_DIR = BUNDLE_ROOT

_EXE_DIR = EXE_DIR  # 兼容内部旧引用

# 单文件 exe 与「带文件夹的便携版」在性能上差别很大：
# 单文件模式下每 spawn 一个子进程，PyInstaller 引导器都要把整个压缩包
# 重新解包到新的临时目录（本程序含 WebView2/.NET 运行时，一次解包要数秒），
# 因此子进程越多反而越慢；文件夹版则不需要解包。
IS_ONEFILE = IS_FROZEN and BUNDLE_ROOT.parent != EXE_DIR

# 写探针文件：固定名字、只追加、从不删除。
PROBE_NAME = ".cwp_write_probe"
# 历史版本用过的「建了再删」探针文件名前缀，只用于清扫旧残留。
PROBE_PREFIX = ".write_probe_"
_PROBE_MAX_BYTES = 1024


def _is_writable(path: Path) -> bool:
    """探测目录是否可写（便携模式判定的关键）。

    这里刻意**不创建随机文件、也不删除任何文件**，而是往一个固定名字的探针
    文件追加一个字节。三个原因，每个都是踩过的坑：

    1. **并发安全**。早期用「创建 `.write_probe` 再 `unlink()`」。单进程没事，
       一开多进程批处理就出问题：先建的还没删，另一个子进程已经把它删掉了，
       `unlink()` 抛 FileNotFoundError，被误判成「目录不可写」，该子进程就悄悄
       退回 %LOCALAPPDATA%，然后找不到模板、整张照片处理失败。
       现象是「多进程偶发几张失败、串行却全好」。
       追加写入没有「谁删谁」的问题，天然并发安全。

    2. **不触发删除配额**。受管控环境（沙箱 / 企业策略 / 杀毒软件）对单次批量
       删除有配额，删超了会**直接把进程杀掉**。启动阶段被这么干掉，用户看到的就是
       「双击了但程序打不开」。追加写入完全绕开删除操作。

    3. **不留垃圾**。文件超过 1KB 就用 `'w'` 截断重写（截断不是删除），
       所以便携目录里永远只有这一个探针文件。

    探测失败（只读目录 / 无权限 / ACL 拒绝）时返回 False，调用方会回退到
    %LOCALAPPDATA%。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / PROBE_NAME
        try:
            if probe.exists() and probe.stat().st_size > _PROBE_MAX_BYTES:
                probe.write_text("", encoding="utf-8")  # 截断，不是删除
        except OSError:
            pass
        with open(probe, "a", encoding="utf-8") as f:
            f.write(".")
        return True
    except Exception:
        return False


def stale_probes(directory: Path, max_age: float = 900.0) -> list:
    """列出可以清扫的旧版探针文件（**只读，不删除**）。

    把「挑哪些」和「删哪些」拆开，是为了让调用方能先看一眼再决定 ——
    也方便测试在不产生任何删除操作的前提下验证筛选逻辑
    （删除操作在某些环境里会撞配额，甚至让进程被杀掉）。
    """
    out = []
    try:
        now = time.time()
        for f in directory.glob(f"{PROBE_PREFIX}*"):
            try:
                if now - f.stat().st_mtime > max_age:
                    out.append(f)
            except OSError:
                pass
    except OSError:
        pass
    return sorted(out)


def _sweep_stale_probes(directory: Path, max_age: float = 900.0) -> int:
    """清扫旧版本留下的 `.write_probe_*` 残留文件（只删超过 max_age 秒的）。

    新版本已经不再产生这类文件了（见 `_is_writable`），这个函数纯粹是为了让
    从旧版升级上来的便携目录保持干净。

    **不要在启动路径里调用它**：删除操作在受管控环境里可能撞上配额、甚至让进程
    被杀掉。留给显式的维护动作或测试用例调用。
    """
    removed = 0
    for f in stale_probes(directory, max_age):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _resolve_data_root() -> tuple:
    """决定可写数据根目录。

    优先复用父进程通过环境变量传下来的结论：多进程（spawn）时子进程会继承
    环境变量，这样既能保证「所有进程看到同一个 DATA_ROOT」，又能省掉每个
    子进程重复探测目录的开销。
    """
    inherited = os.environ.get("CWP_DATA_ROOT")
    if inherited:
        candidate = Path(inherited)
        if candidate.exists():
            return candidate, os.environ.get("CWP_PORTABLE", "1") == "1"

    if _is_writable(_EXE_DIR):
        root = _EXE_DIR
        portable = True
    else:
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
        root.mkdir(parents=True, exist_ok=True)
        portable = False

    # 注意：这里**故意不做任何清理**。
    # 启动阶段绝不要执行删除操作 —— 受管控环境（沙箱 / 企业策略 / 杀毒软件）
    # 对删除有配额，删超了会直接把进程杀掉，用户看到的就是「双击了但打不开」。
    # 旧版本留下的 `.write_probe_*` 残留可以用 `_sweep_stale_probes()` 手动清，
    # 新版本已经不再产生这类文件了（见 `_is_writable`）。

    # 传给后续 spawn 出来的子进程
    try:
        os.environ["CWP_DATA_ROOT"] = str(root)
        os.environ["CWP_PORTABLE"] = "1" if portable else "0"
    except Exception:
        pass
    return root, portable


DATA_ROOT, PORTABLE = _resolve_data_root()

CONFIG_PATH = DATA_ROOT / "config" / "config.ini"
PROJECT_INFO = BUNDLE_ROOT / "pyproject.toml"
LOG_DIR = DATA_ROOT / "logs"


def resolve_resource(*parts: str) -> Path:
    """按 DATA_ROOT → BUNDLE_ROOT 顺序解析资源路径，返回第一个存在的。"""
    for root in (DATA_ROOT, BUNDLE_ROOT):
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return BUNDLE_ROOT.joinpath(*parts)


def ensure_user_data() -> None:
    """首次运行时，把内置的模板 / 字体 / Logo 复制到可写目录，并生成默认配置。

    这样用户既能拿到全部内置资源，又能自由增删自己的模板与 Logo，
    而升级程序时这些自定义内容不会被覆盖。

    注意：config.ini **不随程序打包**，而是在这里按 DEFAULTS 现场生成。
    否则开发调试时被改乱的配置会被一起打进安装包，导致新用户首次启动
    就指向了不存在的目录。
    """
    import configparser

    src_config = BUNDLE_ROOT / "config"
    dst_config = DATA_ROOT / "config"
    dst_config.mkdir(parents=True, exist_ok=True)

    for sub in ("templates", "fonts", "logos"):
        s = src_config / sub
        if not s.is_dir():
            continue
        d = dst_config / sub
        d.mkdir(parents=True, exist_ok=True)
        for f in s.iterdir():
            if f.is_file() and not (d / f.name).exists():
                try:
                    shutil.copy2(f, d / f.name)
                except Exception:
                    pass

    ini = dst_config / "config.ini"
    if not ini.exists():
        from core.configs import DEFAULT_TEMPLATE, DEFAULTS

        config = configparser.ConfigParser()
        config["DEFAULT"] = dict(DEFAULTS)
        config["render"] = {"template_name": DEFAULT_TEMPLATE}
        with open(ini, "w", encoding="utf-8") as f:
            config.write(f)

    # 内置预设方案（已存在的不覆盖，用户改过的预设不会被重置）
    try:
        from core.presets import seed_builtin_presets
        seed_builtin_presets()
    except Exception:
        pass


__all__ = [
    "APP_NAME",
    "APP_DISPLAY_NAME",
    "IS_FROZEN",
    "IS_ONEFILE",
    "PORTABLE",
    "BUNDLE_ROOT",
    "DATA_ROOT",
    "EXE_DIR",
    "CONFIG_PATH",
    "PROJECT_INFO",
    "LOG_DIR",
    "PROBE_NAME",
    "PROBE_PREFIX",
    "stale_probes",
    "resolve_resource",
    "ensure_user_data",
]
