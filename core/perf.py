"""性能相关：进程数决策 + 多进程批处理调度（含串行兜底）。

设计原则：**多进程是加速手段，不是正确性前提。**

任何一步出问题（spawn 失败、子进程崩溃、打包环境不支持）都必须能无缝退回
串行执行，并且保证「已处理的结果不丢、未处理的任务不跳」。因此这里的调度器
维护一个 pending 集合，池子挂了就把剩下的任务用串行跑完。
"""
from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED

from core import IS_FROZEN, IS_ONEFILE
from core.logger import logger

# 进程数上限：再多也不会更快（磁盘 IO 与 exiftool 子进程会成为瓶颈），
# 反而会显著抬高内存占用（每张 6000 万像素的 RGBA 图约 240MB）。
MAX_WORKERS = 8

# 单文件 exe 模式下，每个 spawn 子进程都要让 PyInstaller 引导器把整个压缩包
# 重新解包到新的临时目录（本程序带 WebView2/.NET，一次解包数秒）。因此这里
# 把上限压到 4：再多的进程只会把时间花在解包上。文件夹便携版没有这个开销。
ONEFILE_MAX_WORKERS = 4


def cpu_total() -> int:
    try:
        return os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        return 4


def max_workers() -> int:
    """当前运行形态下的进程数上限。"""
    return ONEFILE_MAX_WORKERS if IS_ONEFILE else MAX_WORKERS


def worker_count(mode, total: int, min_batch: int = 4) -> int:
    """根据配置和待处理数量决定用几个进程。

    mode: "serial" / "auto" / 数字字符串（"2"、"4"…）
    """
    text = str(mode or "auto").strip().lower()
    if text in ("serial", "off", "0", "1", "single"):
        return 1

    cap = max_workers()
    if text in ("auto", "", "none"):
        cores = cpu_total()
        # 留一个核给界面/系统，避免拖动窗口都卡
        workers = max(1, min(cores - 1, cap))
    else:
        try:
            workers = int(float(text))
        except (TypeError, ValueError):
            workers = 1
        workers = max(1, min(workers, cap))

    # 任务太少时，进程启动开销（Windows spawn 约 0.3~1s/进程）比省下的时间还多
    if total < max(1, int(min_batch)):
        return 1
    return min(workers, max(1, total))


def _failure(task: dict, message: str) -> dict:
    return {"input_path": task.get("input_path"), "output_path": task.get("output_path"),
            "status": "failure", "error": message}


def _cancelled(task: dict) -> dict:
    return {"input_path": task.get("input_path"), "output_path": task.get("output_path"),
            "status": "skipped", "error": "任务已取消"}


def run_tasks(tasks, workers: int, cancel_event, on_start, on_result) -> str:
    """执行任务列表，返回实际使用的模式："serial" | "pool" | "pool-fallback"。

    on_start(task)   —— 任务开始（用于更新「正在处理」）
    on_result(result) —— 单个任务结束（用于累计成功/跳过/失败）
    """
    if not tasks:
        return "serial"

    if workers <= 1:
        _run_serial(tasks, cancel_event, on_start, on_result)
        return "serial"

    pending = list(tasks)
    try:
        _run_pool(pending, workers, cancel_event, on_start, on_result)
        return "pool"
    except Exception as e:  # noqa: BLE001
        logger.exception(f"多进程调度失败，回退串行：{e}")
        remaining = [t for t in pending if not t.get("_done")]
        logger.warning(f"多进程回退串行，剩余 {len(remaining)} 张")
        _run_serial(remaining, cancel_event, on_start, on_result)
        return "pool-fallback"


def _run_serial(tasks, cancel_event, on_start, on_result):
    from core.worker import process_file
    for task in tasks:
        if cancel_event.is_set():
            return
        on_start(task)
        result = process_file(task)
        task["_done"] = True
        on_result(result)


def _run_pool(tasks, workers, cancel_event, on_start, on_result):
    """用 spawn 上下文的进程池跑任务。

    spawn 而非 fork：Windows 只有 spawn；且 fork 会把 Flask / WebView2 的
    句柄一起复制到子进程，极易出现莫名其妙的卡死。
    """
    from core.worker import process_file

    ctx = multiprocessing.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)

    futures = {}
    try:
        for task in tasks:
            if cancel_event.is_set():
                break
            on_start(task)
            futures[executor.submit(process_file, task)] = task

        pending_futures = set(futures)
        while pending_futures:
            done, pending_futures = wait(pending_futures, timeout=0.35,
                                         return_when=FIRST_COMPLETED)
            for future in done:
                task = futures[future]
                if cancel_event.is_set():
                    on_result(_cancelled(task))
                    continue
                try:
                    result = future.result()
                except Exception as e:  # noqa: BLE001
                    # 子进程级异常（BrokenProcessPool / PicklingError…）
                    raise RuntimeError(f"子进程执行失败: {e}") from e
                task["_done"] = True
                on_result(result)

            if cancel_event.is_set():
                # 停掉还没开始的任务，已开始的自然结束
                for future in pending_futures:
                    task = futures[future]
                    if future.cancel():
                        task["_done"] = True
                        on_result(_cancelled(task))
                pending_futures = set()
                break
    finally:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except Exception:  # noqa: BLE001
            logger.debug("进程池关闭异常", exc_info=True)


# ---------------------------------------------------------------- 健康检查

def _probe_target(queue):
    try:
        from core.worker import probe_worker
        queue.put(probe_worker())
    except Exception as e:  # noqa: BLE001
        queue.put({"ok": False, "error": str(e), "pid": os.getpid()})


def probe_multiprocessing(timeout: float = 25.0) -> dict:
    """真起一个子进程验证多进程可用性（打包环境尤其需要）。

    这是给界面上的「性能自检」按钮用的：如果这里失败，批处理会自动串行，
    用户可以据此判断是环境问题还是配置问题。
    """
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_probe_target, args=(queue,), daemon=True)
    result: dict
    try:
        proc.start()
        try:
            result = queue.get(timeout=timeout)
        except Exception:  # noqa: BLE001
            result = {"ok": False, "error": "子进程无响应（超时）"}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"无法启动子进程：{e}"}
    finally:
        try:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            queue.close()
        except Exception:  # noqa: BLE001
            pass

    result.setdefault("cores", cpu_total())
    result.setdefault("max_workers", max_workers())
    result["onefile"] = IS_ONEFILE
    result["frozen"] = IS_FROZEN
    return result


def suggested_workers(mode, total: int, min_batch: int = 4) -> int:
    return worker_count(mode, total, min_batch)
