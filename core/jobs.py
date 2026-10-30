"""批量任务管理：进度、ETA、可取消、事件流。

每个任务拥有独立的事件队列，HTTP 层通过 SSE 消费，界面据此显示进度并支持中途取消。
"""
import queue
import threading
import time
import uuid


class Job:
    """一次批量处理任务。"""

    def __init__(self, files, options: dict):
        self.id = uuid.uuid4().hex[:12]
        self.files = list(files)
        self.options = dict(options or {})
        self.total = len(self.files)

        self.processed = 0
        self.success = 0
        self.failure = 0
        self.skipped = 0
        self.current = ""
        self.message = ""
        self.status = "pending"  # pending | running | done | cancelled | error

        self.results = []   # [{file, status, error, output}]
        self.errors = []    # [{file, error}]
        self.skipped_items = []   # [{file, reason, output}]

        # 本次任务实际使用的目录（由 process/start 显式传入，避免依赖配置文件）
        self.input_root = self.options.get("input_root") or ""
        self.output_root = self.options.get("output_root") or ""
        self.note = self.options.get("output_note") or ""

        # 实际使用的执行引擎：serial / pool / pool-fallback（由调度器回填）
        self.engine = "serial"
        self.workers = 1

        self.started_at = None
        self.finished_at = None

        self.cancel_event = threading.Event()
        self.queue: "queue.Queue" = queue.Queue()
        self.lock = threading.Lock()

    # ---------------- 进度快照 ----------------
    def snapshot(self) -> dict:
        with self.lock:
            percent = round(self.processed / self.total * 100) if self.total else 0
            eta = None
            elapsed = None
            if self.started_at:
                elapsed = time.time() - self.started_at
                if self.processed > 0 and self.status == "running":
                    avg = elapsed / self.processed
                    remaining = (self.total - self.processed) * avg
                    eta = int(max(0, remaining))
                elif self.status in ("done", "cancelled"):
                    eta = 0
            return {
                "id": self.id,
                "total": self.total,
                "processed": self.processed,
                "success": self.success,
                "failure": self.failure,
                "skipped": self.skipped,
                "current": self.current,
                "message": self.message,
                "status": self.status,
                "percent": percent,
                "elapsed": int(elapsed) if elapsed is not None else None,
                "eta": eta,
                "errors": list(self.errors),
                "cancelled": self.cancel_event.is_set(),
                "input_root": self.input_root,
                "output_root": self.output_root,
                "note": self.note,
                "engine": self.engine,
                "workers": self.workers,
                "duration": (round(self.finished_at - self.started_at, 2)
                             if (self.finished_at and self.started_at) else None),
                "avg_ms": self._avg_ms(),
            }

    def _avg_ms(self):
        """单张平均耗时（毫秒），用于让用户直观看到多进程的收益。"""
        if not self.started_at or not self.finished_at or not self.processed:
            return None
        return round((self.finished_at - self.started_at) * 1000 / self.processed)

    def result_items(self) -> list:
        """结果面板用的清单：成功项带输出路径，跳过/失败项带原因。"""
        with self.lock:
            return [dict(item) for item in self.results]

    def output_files(self) -> list:
        """成功产出的文件路径（按处理顺序）。"""
        with self.lock:
            return [item["output"] for item in self.results
                    if item.get("status") == "success" and item.get("output")]

    def emit(self, event: str, data: dict = None):
        self.queue.put((event, data or {}))

    # ---------------- 状态推进 ----------------
    def mark_running(self):
        with self.lock:
            self.status = "running"
            self.started_at = time.time()
        self.emit("progress", self.snapshot())

    def mark_file_start(self, name: str):
        with self.lock:
            self.current = name
            self.message = f"正在处理：{name}"
        self.emit("progress", self.snapshot())

    def mark_file_done(self, name: str, status: str, error: str = None, output: str = None,
                       source: str = None):
        """记录一张照片的处理结果。

        source 是原图绝对路径，结果面板要靠它做「成片 ↔ 原图」对比。
        """
        with self.lock:
            self.processed += 1
            if status == "success":
                self.success += 1
            elif status == "skipped":
                self.skipped += 1
                self.skipped_items.append({"file": name, "reason": error or "已存在", "output": output})
            else:
                self.failure += 1
                if error:
                    self.errors.append({"file": name, "error": error})
            self.results.append({"file": name, "status": status, "error": error,
                                 "output": output, "source": source})
            label = {"success": "完成", "failure": "失败", "skipped": "跳过"}.get(status, status)
            self.message = f"{label}：{name}"
        self.emit("progress", self.snapshot())

    def _summarize_skips(self) -> str:
        """把跳过原因归纳成一句人话，避免用户看到「成功 0」却不知为何。"""
        if not self.skipped_items:
            return ""
        reasons = {}
        for item in self.skipped_items:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        parts = [f"{r}（{c} 张）" for r, c in reasons.items()]
        return "；跳过原因：" + "、".join(parts)

    def mark_done(self, cancelled: bool = False):
        with self.lock:
            self.status = "cancelled" if cancelled else "done"
            self.finished_at = time.time()
            head = "已取消" if cancelled else "处理完成"
            self.message = (f"{head} —— 成功 {self.success}，跳过 {self.skipped}，"
                            f"失败 {self.failure}")
            if self.skipped:
                self.message += self._summarize_skips()
            if not cancelled and self.success == 0 and self.skipped == 0 and self.failure == 0:
                self.message += "（没有找到可处理的照片）"
            if self.success and self.output_root:
                self.message += f" · 输出目录：{self.output_root}"
            if self.note:
                self.message = f"{self.note} {self.message}"
            # 让用户看得见「快在哪里」：用时 + 单张均速 + 实际并行度
            if self.started_at and self.finished_at and self.processed:
                cost = self.finished_at - self.started_at
                avg = cost * 1000 / self.processed
                speed = f" · 用时 {cost:.1f} 秒（单张均速 {avg:.0f}ms"
                speed += f" · {self.workers} 进程并行）" if self.workers > 1 else "）"
                self.message += speed
        self.emit("complete", self.snapshot())

    def mark_error(self, message: str):
        with self.lock:
            self.status = "error"
            self.message = message
            self.finished_at = time.time()
        self.emit("complete", self.snapshot())


class JobManager:
    """任务注册表（线程安全）。"""

    def __init__(self):
        self._jobs: dict = {}
        self._lock = threading.Lock()

    def create(self, files, options: dict) -> Job:
        job = Job(files, options)
        with self._lock:
            self._jobs[job.id] = job
            # 控制内存占用：只保留最近 30 个任务
            if len(self._jobs) > 30:
                for key in list(self._jobs.keys())[:-30]:
                    self._jobs.pop(key, None)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job and job.status in ("pending", "running"):
            job.cancel_event.set()
            return True
        return False


manager = JobManager()
