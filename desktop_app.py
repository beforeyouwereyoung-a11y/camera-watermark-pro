"""桌面入口：WebView2 原生窗口 + 系统托盘 + 单实例保护。

用法：
    源码运行  python desktop_app.py
    打包运行  双击 相机水印工具.exe
"""
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 确保以本文件所在目录为基准导入（打包后同样适用）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import APP_DISPLAY_NAME, BUNDLE_ROOT, DATA_ROOT  # noqa: E402
from core.configs import load_config  # noqa: E402
from core.logger import logger  # noqa: E402

import app as server  # noqa: E402

SINGLE_INSTANCE_PORT = 15299

# 拖拽通道的注册状态。两个线程（loaded 事件回调 / 兜底轮询线程）都可能来注册，
# 必须用锁 + 标志位保证「最多注册一次」。
_drop_lock = threading.Lock()
_drop_state = {"registered": False, "attempts": 0}


def _single_instance_guard():
    """用一个本地端口占用做单实例锁；失败说明已有实例在运行。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
        return sock
    except OSError:
        return None


def _wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _load_tray_image():
    from PIL import Image
    for candidate in (BUNDLE_ROOT / "assets" / "logo.ico",
                      BUNDLE_ROOT / "assets" / "logo.png",
                      BUNDLE_ROOT / "config" / "logos" / "default.png"):
        if candidate.exists():
            try:
                return Image.open(str(candidate)).convert("RGBA")
            except Exception:
                continue
    return Image.new("RGBA", (64, 64), (60, 120, 200, 255))


def _register_native_drop(window, quiet: bool = False) -> bool:
    """让 WebView2 把拖拽进来的文件「真实路径」交给界面。

    为什么不能只写 JS：WebView2 出于安全考虑，网页里
    `dataTransfer.files[0].path` 是拿不到的（只有虚拟文件名），所以纯前端
    拖拽只能提示「无法读取路径」。

    pywebview 对此有专门通道：只要用它的 DOM API 注册过 `drop` 监听
    （内部会把 _dnd_state['num_listeners'] 加一），WebView2 就会通过
    `FilesDropped` 把真实路径回传，并挂到
    `event['dataTransfer']['files'][i]['pywebviewFullPath']` 上。

    这里注册监听并把路径转发给页面里的 window.__onNativeDrop()。

    `quiet=True` 用于重试轮询：前几次页面还没加载完，`#dropzone` 找不到是
    正常现象，不该刷一屏 warning。
    """
    # 只允许注册一次：重复注册会让 num_listeners 递增，
    # 同一个 drop 事件被处理多次，路径列表里出现重复项。
    if _drop_state["registered"]:
        return True

    def _warn(msg: str):
        if quiet:
            logger.debug(msg)
        else:
            logger.warning(msg)

    try:
        from webview.dom import DOMEventHandler
    except Exception as e:  # noqa: BLE001
        _warn(f"pywebview DOM API 不可用，原生拖拽已禁用: {e}")
        return False

    def _on_drop(event):
        try:
            data = event.get("dataTransfer") or {}
            files = data.get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files
                     if isinstance(f, dict) and f.get("pywebviewFullPath")]
            if not paths:
                logger.warning("拖拽事件未携带真实路径（可能未注册 DOM 监听）")
                return
            # 去重但保序，避免上面提到的重复注册造成重复项
            seen, uniq = set(), []
            for p in paths:
                if p not in seen:
                    seen.add(p)
                    uniq.append(p)
            payload = json.dumps(uniq, ensure_ascii=False)
            window.evaluate_js(f"window.__onNativeDrop && window.__onNativeDrop({payload})")
            logger.info(f"收到拖拽路径 {len(uniq)} 个")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理拖拽事件失败: {e}")

    try:
        element = window.dom.get_element("#dropzone")
    except Exception as e:  # noqa: BLE001
        _warn(f"查找拖拽区域失败: {e}")
        return False

    if element is None:
        _warn("页面中没有 #dropzone，原生拖拽不可用")
        return False

    try:
        element.on("drop", DOMEventHandler(_on_drop, prevent_default=True))
    except Exception as e:  # noqa: BLE001
        _warn(f"注册拖拽监听失败: {e}")
        return False

    with _drop_lock:
        _drop_state["registered"] = True
        _drop_state["attempts"] = _drop_state["attempts"] + 1
    server.set_native_drop_ready(True)
    logger.info("原生拖拽已就绪（WebView2 真实路径通道）")
    return True


def _arm_native_drop(window, timeout: float = 60.0, interval: float = 0.6) -> bool:
    """确保拖拽通道一定被注册上（带重试的兜底）。

    为什么不只依赖 `window.events.loaded`：这个事件在正常情况下很好用，
    但它**不是可靠的**。已实测到两种它不触发（或触发时页面仍未就绪）的情形：
      * 窗口由非交互式会话拉起（后台任务、服务、CI）时，WebView2 可能
        始终完不成导航，`loaded` 永远不来；
      * 机器冷启动 + 首次创建 WebView2 用户数据目录，首屏可能慢到事件已经
        发出而我们还没挂上监听。

    用户反馈里的「拖拽文件夹提示当前环境不支持拖拽」正是这一类问题：
    注册没成功，页面就退回纯前端拖拽，而 WebView2 下拿不到真实路径。
    所以这里再主动轮询重试，两条路谁先成功算谁。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _drop_state["registered"]:
            return True
        attempt = _drop_state["attempts"] + 1
        # 第一次失败照常 warning，之后降级成 debug，避免刷日志
        if _register_native_drop(window, quiet=attempt > 1):
            if attempt > 1:
                logger.info(f"原生拖拽在第 {attempt} 次尝试后注册成功")
            return True
        with _drop_lock:
            _drop_state["attempts"] = attempt
        time.sleep(interval)
    logger.warning("原生拖拽通道注册超时，已回退为「点击浏览选择文件夹」")
    return False


def main():
    guard = _single_instance_guard()
    config = load_config()
    host = config.get("DEFAULT", "host")
    port = config.getint("DEFAULT", "port")
    url = f"http://{host}:{port}"

    if guard is None:
        # 已有实例：直接打开已有界面
        logger.info("检测到已有实例在运行，打开现有界面")
        webbrowser.open(url)
        return

    # 后台线程启动本地服务
    server_thread = threading.Thread(
        target=server.run_server,
        kwargs={"host": host, "port": port, "debug": False},
        daemon=True,
    )
    server_thread.start()

    if not _wait_for_server(host, port):
        logger.error("本地服务启动失败")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "本地服务启动失败，请查看 logs 目录。",
                                             APP_DISPLAY_NAME, 0x10)
        except Exception:
            pass
        return

    import webview

    window = webview.create_window(
        APP_DISPLAY_NAME,
        url,
        width=1400,
        height=920,
        min_size=(1120, 720),
        text_select=True,
        confirm_close=False,
    )
    server.set_window(window)

    def _on_loaded():
        # loaded 的回调本身跑在子线程里，可以安全调用 evaluate_js
        # （如果在 GUI 线程里调用会互相等待而卡死）。
        if not _arm_native_drop(window):
            logger.info("原生拖拽未启用，将回退为「点击浏览选择文件夹」")

    try:
        window.events.loaded += _on_loaded
    except Exception:  # noqa: BLE001
        logger.debug("注册 loaded 事件失败", exc_info=True)

    # 兜底：`loaded` 事件在非交互式会话 / 冷启动慢的机器上可能一直不来。
    # 这条线程与上面的 loaded 回调互相竞争，谁先注册成功算谁（内部有锁）。
    threading.Thread(
        target=_arm_native_drop, args=(window,), daemon=True
    ).start()

    state = {"tray": None, "quitting": False}

    def _show_window():
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def _open_path(path):
        def handler(icon=None, item=None):
            try:
                target = Path(path)
                target.mkdir(parents=True, exist_ok=True)
                os.startfile(str(target))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"打开目录失败 {path}: {e}")
        return handler

    def _quit(icon=None, item=None):
        state["quitting"] = True
        try:
            if state["tray"]:
                state["tray"].stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass
        os._exit(0)

    def _start_tray():
        try:
            import pystray
        except Exception as e:  # noqa: BLE001
            logger.warning(f"托盘不可用: {e}")
            return
        menu = pystray.Menu(
            pystray.MenuItem("打开主界面", lambda i, it: _show_window(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开输入目录", _open_path(DATA_ROOT / "input")),
            pystray.MenuItem("打开输出目录", _open_path(DATA_ROOT / "output")),
            pystray.MenuItem("打开配置目录", _open_path(DATA_ROOT / "config")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _quit),
        )
        tray = pystray.Icon(APP_DISPLAY_NAME, _load_tray_image(),
                            APP_DISPLAY_NAME, menu)
        state["tray"] = tray
        tray.run()

    threading.Thread(target=_start_tray, daemon=True).start()

    def _on_closing():
        """点右上角关闭时收进托盘，而不是直接退出。"""
        if state["quitting"]:
            return True
        try:
            window.hide()
        except Exception:
            pass
        return False

    try:
        window.events.closing += _on_closing
    except Exception:
        pass

    webview.start(debug=config.getboolean("DEFAULT", "debug"))


if __name__ == "__main__":
    # ⚠️ 必须在做任何事之前调用，且必须是 freeze_support 而不是别的。
    #
    # 打包成 exe 后，Windows 上多进程只能走 spawn：子进程是「把 exe 再启动一遍」，
    # 由 PyInstaller 引导器带 `--multiprocessing-fork` 参数重新拉起。freeze_support()
    # 会识别这个参数，把流程转交给 multiprocessing 的引导逻辑。
    # 不调用它的话，每个子进程都会把整个桌面程序重跑一遍 —— 表现是「一点开始处理
    # 就疯狂弹出新窗口」，或者干脆卡死。源码模式（python desktop_app.py）不会暴露
    # 这个问题，因为那时 spawn 是重新拉起解释器而不是 exe，所以必须在打包后专门验证。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
