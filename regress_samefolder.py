"""回归测试：复现并验证用户反馈的「处理完成但没有输出照片」问题。

用户实际配置（示例）：

    input_folder  = D:\\photos\\example
    output_folder = D:\\photos\\example   <- 完全相同

此时 resolve_output_name() 算出的输出路径 == 原图路径，于是
「目标已存在 + 未勾选覆盖」把所有照片静默跳过，界面却提示「处理完成」，
用户在原目录里自然找不到任何新文件。

期望修复后：
  1. 输入 == 输出时自动改用 <输入目录>/水印输出，不再覆盖也不再全跳过；
  2. 完成消息里能看到输出目录和跳过原因；
  3. /api/v1/drop 能把拖拽路径解析成输入目录 + 待选文件。

运行： python regress_samefolder.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

import app as server  # noqa: E402
from core.util import is_same_path, resolve_output_root  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def make_photo(path: Path, color=(120, 160, 200), size=(1200, 800)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG", quality=92)


def wait_job(client, job_id, timeout=120):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/v1/process/status/{job_id}")
        last = r.get_json()
        if last.get("status") in ("done", "cancelled", "error"):
            return last
        time.sleep(0.25)
    return last or {}


def _snapshot_config():
    """把当前配置冻结一份，测试结束后原样还原。

    踩过的坑：这个用例曾经依赖环境里的 config.ini，开发机上残留的
    `name_pattern = {name}_wm` 让「第二次运行应全部跳过」变成「全部重新生成」，
    用例红了却不是代码的问题。测试必须自带确定性，不能看环境脸色。
    """
    cfg = server.config
    return {key: cfg.get("DEFAULT", key) for key in cfg["DEFAULT"]}


def _restore_config(snapshot):
    cfg = server.config
    for key, value in snapshot.items():
        cfg.set("DEFAULT", key, value)
    server.save_config(cfg)


def _force_test_config():
    """强制成一组确定的参数，屏蔽环境干扰。"""
    cfg = server.config
    cfg.set("DEFAULT", "name_pattern", "{name}")
    cfg.set("DEFAULT", "override_existed", "False")
    cfg.set("DEFAULT", "output_format", "original")
    cfg.set("DEFAULT", "quality", "95")
    cfg.set("DEFAULT", "long_edge", "0")
    cfg.set("DEFAULT", "preserve_exif", "True")
    cfg.set("DEFAULT", "crop_preset", "none")
    cfg.set("DEFAULT", "workers", "serial")
    server.save_config(cfg)


def main():
    print("=" * 72)
    print("回归测试：输入目录 == 输出目录（用户实际踩到的坑）")
    print("=" * 72)

    config_snapshot = _snapshot_config()
    _force_test_config()

    try:
        return _run_case()
    finally:
        _restore_config(config_snapshot)


def _run_case():
    root = Path(tempfile.mkdtemp(prefix="wm_same_"))
    input_dir = root / "青甘大环线" / "出片优选"
    input_dir.mkdir(parents=True, exist_ok=True)
    make_photo(input_dir / "DSC0001.jpg", (200, 90, 90))
    make_photo(input_dir / "DSC0002.jpg", (90, 200, 90))
    make_photo(input_dir / "2024旅行" / "DSC0003.jpg", (90, 90, 200))

    client = server.api.test_client()

    # ---------------- 1. 纯函数层 ----------------
    print("\n[1] resolve_output_root 行为")
    eff, note = resolve_output_root(str(input_dir), str(input_dir))
    check("输入==输出时自动改到子目录", is_same_path(eff, input_dir / "水印输出"), eff)
    check("返回了给用户的提示", bool(note), note)

    other = root / "我要的输出"
    eff2, note2 = resolve_output_root(str(input_dir), str(other))
    check("输入!=输出时保持原样", is_same_path(eff2, other) and note2 is None, str(eff2))

    # ---------------- 2. 走真实接口 ----------------
    print("\n[2] 通过 /api/v1/process/start 走完整流程（输入=输出）")
    # 先把输入目录落到临时目录，避免用到开发机上残留的 config.ini
    client.post("/api/v1/config", json={"input_folder": str(input_dir),
                                        "output_folder": str(input_dir)})
    tree = client.get("/api/v1/file/tree").get_json()
    files = []

    def collect(nodes):
        for n in nodes:
            if n.get("is_file"):
                files.append(n["value"])
            else:
                collect(n.get("children") or [])
    collect(tree["input_files"])
    check("扫描到 3 张照片", len(files) == 3, f"实际 {len(files)}")
    check("扫描结果都在临时输入目录内",
          all(str(input_dir) in f for f in files), str(files))

    r = client.post("/api/v1/process/start", json={
        "files": files,
        "mode": "template",
        "template_name": "标准水印2",
        "input_folder": str(input_dir),
        "output_folder": str(input_dir),          # <- 用户就是这么设的
        "override_existed": False,
        "output_format": "original",
        "name_pattern": "{name}",
        "quality": 95,
        "long_edge": 0,
        "preserve_exif": True,
        "crop_preset": "none",
        "simple": {},
    })
    check("启动任务返回 200", r.status_code == 200, str(r.status_code))
    payload = r.get_json() or {}
    check("响应带 output_root", bool(payload.get("output_root")), str(payload.get("output_root")))
    check("响应带 note 提示", bool(payload.get("note")), str(payload.get("note")))

    snap = wait_job(client, payload["job_id"])
    print(f"      完成消息：{snap.get('message')}")
    check("任务正常结束", snap.get("status") == "done", str(snap.get("status")))
    check("成功数 == 3", snap.get("success") == 3, str(snap.get("success")))
    check("没有静默跳过", snap.get("skipped") == 0, str(snap.get("skipped")))
    check("没有失败", snap.get("failure") == 0, str(snap.get("failure")))

    # ---------------- 3. 文件真的落在子目录里 ----------------
    print("\n[3] 检查成片落盘位置")
    out_root = Path(snap.get("output_root") or "")
    check("output_root 指向 水印输出", out_root.name == "水印输出", str(out_root))
    produced = sorted(p.name for p in out_root.rglob("*.jpg")) if out_root.exists() else []
    check("子目录里出现 3 张成片", len(produced) == 3, str(produced))
    check("保留子目录结构", (out_root / "2024旅行" / "DSC0003.jpg").exists(), str(produced))
    check("原图未被改动/新增同名文件", not (input_dir / "DSC0001_wm.jpg").exists())

    # 完成消息要能告诉用户去哪儿找
    check("完成消息包含输出目录", "水印输出" in (snap.get("message") or ""),
          snap.get("message", ""))

    # ---------------- 4. 已存在 -> 跳过要说明原因 ----------------
    print("\n[4] 再次运行：应全部跳过并说明原因")
    client.post("/api/v1/config", json={"input_folder": str(input_dir),
                                        "output_folder": str(input_dir)})
    tree4 = client.get("/api/v1/file/tree").get_json()
    files4 = []

    def collect4(nodes):
        for n in nodes:
            if n.get("is_file"):
                files4.append(n["value"])
            else:
                collect4(n.get("children") or [])
    collect4(tree4["input_files"])
    check("第二次扫描到 3 张原图（输出目录被排除）", len(files4) == 3, f"实际 {len(files4)}")

    r2 = client.post("/api/v1/process/start", json={
        "files": files4,
        "mode": "template",
        "template_name": "标准水印2",
        "input_folder": str(input_dir),
        "output_folder": str(input_dir),
        "override_existed": False,
        "simple": {},
    })
    snap2 = wait_job(client, r2.get_json()["job_id"])
    print(f"      完成消息：{snap2.get('message')}")
    check("跳过数 == 3", snap2.get("skipped") == 3, str(snap2.get("skipped")))
    check("消息解释了跳过原因", "已存在" in (snap2.get("message") or ""),
          snap2.get("message", ""))

    # ---------------- 5. 报告导出 ----------------
    print("\n[5] 报告导出")
    rep = client.get(f"/api/v1/process/report/{snap2['id']}")
    text = rep.get_data(as_text=True)
    check("报告含输入目录", "输入目录" in text)
    check("报告含跳过清单", "跳过清单" in text)

    # ---------------- 6. 拖拽接口 ----------------
    print("\n[6] /api/v1/drop 拖拽路径解析")
    d1 = client.post("/api/v1/drop", json={"paths": [str(input_dir)]})
    j1 = d1.get_json() or {}
    check("拖文件夹 -> 设置输入目录", d1.status_code == 200 and is_same_path(j1.get("folder"), input_dir),
          str(j1))

    d2 = client.post("/api/v1/drop", json={
        "paths": [str(input_dir / "DSC0001.jpg"), str(input_dir / "DSC0002.jpg")]})
    j2 = d2.get_json() or {}
    check("拖文件 -> 目录取父级", is_same_path(j2.get("folder"), input_dir), str(j2.get("folder")))
    check("拖文件 -> 自动选中 2 张", len(j2.get("selected") or []) == 2, str(j2.get("selected")))

    d3 = client.post("/api/v1/drop", json={"paths": [str(root / "不存在.txt")]})
    check("无效路径 -> 400", d3.status_code == 400, str(d3.status_code))

    # ---------------- 7. 目录保护 ----------------
    print("\n[7] 输出目录在输入目录内部时，扫描要跳过它")
    tree2 = client.get("/api/v1/file/tree").get_json()
    names = []

    def collect2(nodes):
        for n in nodes:
            names.append(n["label"])
            if not n.get("is_file"):
                collect2(n.get("children") or [])
    collect2(tree2["input_files"])
    check("扫描结果里没有 水印输出 目录", "水印输出" not in names, str(names))

    # 清理：刻意不做。
    # 这里的临时目录是每次运行新建的（名字带随机后缀），删不删都不影响正确性；
    # 而删除操作在受管控环境里会撞配额、甚至让整个进程被杀掉，反而把用例搞挂。
    # 系统临时目录本身会被系统定期清理，交给它就好。
    print(f"\n（临时目录保留在：{root}）")

    print("\n" + "=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
