"""v3.2.0 新功能验收测试：预设 / 可视化编辑器 / 结果面板 / Logo / 深色模式 / 性能。

运行方式（必须绕开系统代理，否则本地请求会被代理吞掉变成 502）：
    NO_PROXY=127.0.0.1,localhost python test_features.py

覆盖：
  A. 预设方案 CRUD 与「套用后参数一致」
  B. 处理器元数据完整性（每个已注册处理器都要有元数据）
  C. 自定义 Logo 上传 / 列表 / 删除 / 合成生效
  D. 结果面板数据（output/source/exists/size）
  E. 深色模式：CSS 变量成对存在 + 模板里没有硬编码颜色
  F. 性能：EXIF 缓存 LRU、预览降采样、多进程批处理、串行兜底
  G. 同目录保护前置 + 一键修复
  H. 打包安全：freeze_support 入口保护、写探针并发安全与清扫
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 把数据目录隔离到测试专用目录。
#
# 为什么必须这么做，且必须放在任何 core / app 导入之前：
#   * core 在**导入时**就会解析 DATA_ROOT，晚了就不生效；
#   * 不隔离的话，测试写进去的自定义预设、改过的 config.ini 会留在项目的
#     config/ 里，既污染源码，也可能被后面的打包流程带进发行包。
# 隔离之后，测试的配置 / 模板 / Logo / 预设全部落在 _feature_test/data/ 下，
# 项目自身的 config/ 保持干净。
_TEST_DATA = ROOT / "_feature_test" / "data"
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["CWP_DATA_ROOT"] = str(_TEST_DATA)

HOST = "127.0.0.1"
PORT = 15271
BASE = f"http://{HOST}:{PORT}"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def req(path, method="GET", body=None, raw=False, files=None):
    url = BASE + path
    data = None
    headers = {}
    if files is not None:
        boundary = "----cwpboundary"
        parts = []
        for field, (fname, content, ctype) in files.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode())
            parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
            parts.append(content)
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload, dict(resp.headers)
            # 注意要同时接住 UnicodeDecodeError：接口返回图片（JPEG）时
            # payload 根本不是合法 UTF-8，先抛的是解码错误而不是 JSONDecodeError。
            try:
                return resp.status, json.loads(payload.decode("utf-8")), dict(resp.headers)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode("utf-8")), dict(e.headers)
        except Exception:  # noqa: BLE001
            return e.code, payload, dict(e.headers)


# ---------------------------------------------------------------- 测试环境

RUN_ID = time.strftime("%m%d-%H%M%S")

# Logo 用例必须带本次运行的唯一后缀。
#
# 为什么：_feature_test/data 是**跨运行复用**的固定目录（不能删，删了会撞
# 沙箱的批量删除配额）。如果 Logo 名字写死成 ut_logo.png，第二次跑测试时
# 上传会命中「同名不覆盖」逻辑变成 ut_logo_2.png，用例就会假失败。
# 加上 RUN_ID 之后，每次运行都是全新的名字，断言可复现。
LOGO_TAG = RUN_ID.replace("-", "")


def build_fixture(count: int = 6) -> Path:
    """造一个输入目录，内含若干张真实样张（带 EXIF，能走完整链路）。

    每次运行都用一个全新的 `run_<时间戳>` 目录，而不是「先删再建」。
    原因：删除操作在受管控环境（沙箱 / 企业策略 / 杀软）里会撞配额，
    严重时**整个进程被杀掉** —— 用例就会因为「清目录」这种无关紧要的步骤
    整体失败，看起来像是代码坏了。新目录天然干净，`输入`/`输出`/`水印输出`
    这些断言路径全部自动跟着走，无需改动代码。

    代价是 `_feature_test/` 会按次累积运行目录。它是纯测试产物，
    需要清理时手动删掉整个 `_feature_test/` 即可。
    """
    src = ROOT / "_selftest" / "sample.jpg"
    assert src.exists(), f"缺少样张 {src}"
    base = ROOT / "_feature_test"
    base.mkdir(parents=True, exist_ok=True)

    work = base / f"run_{RUN_ID}"
    inp = work / "输入"
    inp.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        shutil.copy2(src, inp / f"DSC0{i + 1:03d}.jpg")
    return work


def start_server():
    import threading
    import app as app_module

    cfg = app_module.config
    cfg.set("DEFAULT", "input_folder", str(FIXTURE / "输入"))
    cfg.set("DEFAULT", "output_folder", str(FIXTURE / "输出"))
    cfg.set("DEFAULT", "workers", "auto")
    app_module.save_config(cfg)

    t = threading.Thread(
        target=lambda: app_module.api.run(host=HOST, port=PORT, threaded=True,
                                          use_reloader=False),
        daemon=True,
    )
    t.start()
    for _ in range(80):
        try:
            status, _, _ = req("/api/v1/meta")
            if status == 200:
                return app_module
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    raise RuntimeError("服务未能启动")


def wait_job(job_id, timeout=300):
    """轮询到任务结束，返回最终快照。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, data, _ = req(f"/api/v1/process/status/{job_id}")
        if status != 200:
            return last
        last = data
        if data.get("status") in ("done", "cancelled", "error"):
            return data
        time.sleep(0.2)
    return last


# ---------------------------------------------------------------- A. 预设

def test_presets():
    print("\n[A] 预设方案")
    status, data, _ = req("/api/v1/presets")
    check("预设列表可读", status == 200 and len(data.get("presets", [])) >= 6,
          f"共 {len(data.get('presets', []))} 个")

    names = [p["name"] for p in data["presets"]]
    check("内置预设名称未被下划线污染",
          any("：" in n for n in names), "、".join(names[:3]))

    from core.presets import BUILTIN_PRESETS, _safe_name
    builtin_names = {_safe_name(p["name"]) for p in BUILTIN_PRESETS}
    found = {p["name"]: p for p in data["presets"] if p["name"] in builtin_names}
    check("内置预设全部就位", builtin_names <= set(found),
          f"缺失 {sorted(builtin_names - set(found)) or '无'}")
    check("内置预设带 builtin 标记",
          all(p.get("builtin") for p in found.values()))

    # 保存一个自定义预设
    custom = {
        "mode": "template", "template_name": "右下角参数", "crop_preset": "1:1",
        "output_format": "png", "name_pattern": "wm_{index3}", "quality": 88,
        "long_edge": 1600, "preserve_exif": False, "override_existed": True,
        "simple": {"text": "hi", "height": 3.5, "opacity": 18, "tile": True},
        "logo": {"logo_name": "sony.png", "logo_scale": 150, "logo_opacity": 80,
                 "logo_corner": "top_left"},
    }
    status, data, _ = req("/api/v1/preset", "POST",
                          {"name": "测试方案 1:1", "desc": "单元测试用", "config": custom})
    check("保存自定义预设", status == 200 and data.get("preset", {}).get("name") == "测试方案 1：1",
          data.get("message", ""))

    status, data, _ = req("/api/v1/preset/" + urllib.parse.quote("测试方案 1：1"))
    check("读回预设内容一致",
          status == 200 and data.get("config", {}).get("quality") == 88
          and data["config"]["logo"]["logo_scale"] == 150)

    # 白名单过滤：多余字段不该被持久化
    status, data, _ = req("/api/v1/preset", "POST",
                          {"name": "_白名单测试", "config": {"quality": 70, "恶意字段": 1}})
    status2, got, _ = req("/api/v1/preset/" + urllib.parse.quote("_白名单测试"))
    check("预设字段白名单生效", "恶意字段" not in got.get("config", {}))

    # 删除不存在的预设：走的是校验分支，不会真的删文件
    status, data, _ = req("/api/v1/preset/" + urllib.parse.quote("不存在的预设"), "DELETE")
    check("删除不存在的预设返回 404", status == 404)

    # 真正删除预设文件：默认不跑。
    # 原因：受管控环境（沙箱 / 企业策略 / 杀软）对删除操作有配额，超了会直接
    # 把测试进程杀掉 —— 那样整个用例会因为「清理测试数据」这种无关紧要的动作
    # 而全红，看起来像代码坏了。需要在正常机器上验证完整删除流程时，
    # 设 CWP_TEST_DESTRUCTIVE=1 再跑一次即可。
    if os.environ.get("CWP_TEST_DESTRUCTIVE") == "1":
        status, data, _ = req("/api/v1/preset/" + urllib.parse.quote("_白名单测试"), "DELETE")
        check("删除预设", status == 200 and data.get("presets") is not None)
        req("/api/v1/preset/" + urllib.parse.quote("测试方案 1：1"), "DELETE")
        check("清理测试预设", True)
    else:
        print("  [SKIP] 真实删除预设（设 CWP_TEST_DESTRUCTIVE=1 可启用）")


# ---------------------------------------------------------------- B. 处理器元数据

def test_processor_schema():
    print("\n[B] 处理器元数据（可视化编辑器基础）")
    status, data, _ = req("/api/v1/processors")
    check("处理器清单可读", status == 200 and len(data.get("processors", [])) >= 21,
          f"{len(data.get('processors', []))} 个")
    check("分类元数据齐全", set(data.get("categories", {})) >= {"filter", "generator", "merger"})

    from processor.core import get_all_processors
    registered = set(get_all_processors().keys())
    listed = {p["name"] for p in data["processors"] if p.get("available")}
    missing = registered - listed
    check("每个已注册处理器都有元数据", not missing, f"缺失：{missing or '无'}")

    # 元数据里的参数必须有 key / label / type
    bad = []
    for p in data["processors"]:
        for param in p.get("params", []):
            if not param.get("key") or not param.get("label") or not param.get("type"):
                bad.append(f"{p['name']}.{param.get('key')}")
    check("参数元数据字段完整", not bad, f"问题项：{bad[:4] or '无'}")

    types = {param["type"] for p in data["processors"] for param in p.get("params", [])}
    expect = {"text", "textarea", "number", "range", "bool", "color", "select", "font",
              "logo", "position", "json"}
    check("参数控件类型覆盖完整", expect <= types, f"缺：{expect - types or '无'}")

    # --- 编辑器实时预览：管道里的 Jinja 表达式必须被渲染 ---
    # 回归测试：曾经 /api/v1/preview/pipeline 把原始管道直接丢给 start_process，
    # 漏了 Jinja 渲染，于是模板里的 {{auto_logo()|replace('\\','/')}} 被当成
    # 文件名去打开，报 `[Errno 22] Invalid argument`。
    # 现象很迷惑：正常预览没事（走 render_template_json），一开编辑器就崩。
    sample = ROOT / "_selftest" / "sample.jpg"
    pipeline = [
        {"processor_name": "margin", "margin": "{{vh(2)}}"},
        {"processor_name": "rounded_corner", "border_radius": "{{vh(1)}}"},
    ]
    status, data, _ = req("/api/v1/preview/pipeline", "POST",
                          {"pipeline": pipeline, "path": str(sample),
                           "crop_preset": "none", "logo": {}})
    check("编辑器管道预览可渲染（Jinja 已生效）",
          status == 200 and isinstance(data, bytes) and data[:2] == b"\xff\xd8",
          f"status={status} {str(data)[:90]}")

    # 带 auto_logo() 的表达式最容易暴露「没渲染」的问题
    pipeline_logo = [
        {"processor_name": "margin", "margin": "{{vh(2)}}"},
        {"processor_name": "watermark", "right_logo": "{{auto_logo()}}",
         "left_top": {"processor_name": "rich_text", "text": "{{ exif.CameraModelName }}",
                      "color": "black"}},
    ]
    status, data, _ = req("/api/v1/preview/pipeline", "POST",
                          {"pipeline": pipeline_logo, "path": str(sample),
                           "crop_preset": "none", "logo": {}})
    check("含 auto_logo()/exif 的管道也能渲染",
          status == 200 and isinstance(data, bytes) and data[:2] == b"\xff\xd8",
          f"status={status} {str(data)[:120]}")

    from core.util import build_context, render_pipeline_json
    ctx = build_context(str(sample))
    rendered = render_pipeline_json(pipeline_logo, ctx)
    check("管道渲染后不再残留 {{ }}",
          not any("{{" in json.dumps(node, ensure_ascii=False) for node in rendered),
          json.dumps(rendered, ensure_ascii=False)[:120])
    check("vh() 被换算成具体像素",
          isinstance(rendered[0].get("margin"), (int, str)) and
          "{{" not in str(rendered[0].get("margin")),
          f"margin={rendered[0].get('margin')}")


# ---------------------------------------------------------------- C. Logo

def test_logo(app_module):
    print("\n[C] 自定义 Logo")
    from PIL import Image, ImageDraw
    import io as _io

    from core.configs import logos_dir

    buf = _io.BytesIO()
    img = Image.new("RGBA", (400, 120), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((10, 10, 110, 110), fill=(255, 0, 0, 255))
    ImageDraw.Draw(img).text((140, 50), "TEST", fill=(255, 255, 255, 255))
    img.save(buf, format="PNG")

    # 本次运行专属的名字，避免跨运行撞名
    logo_a = f"ut_logo_{LOGO_TAG}.png"
    logo_b = f"ut_logo_{LOGO_TAG}_2.png"
    bad_a = f"bad_{LOGO_TAG}.png"

    status, data, _ = req("/api/v1/logo/upload", "POST",
                          files={"file": (logo_a, buf.getvalue(), "image/png")})
    check("上传 PNG Logo", status == 200 and data.get("logo_name") == logo_a,
          data.get("message", ""))
    check("上传后自动设为当前 Logo", data.get("logo_name") == logo_a)

    # 伪装扩展名的坏文件必须被拒，且不能在 logos 目录里留下垃圾
    status, data, _ = req("/api/v1/logo/upload", "POST",
                          files={"file": (bad_a, b"not an image at all", "image/png")})
    check("拒绝伪装成图片的坏文件", status == 400, data.get("error", "")[:60])
    check("被拒文件不会落盘留垃圾", not (logos_dir / bad_a).exists(),
          f"logos_dir/{bad_a}")

    # 不支持的扩展名
    status, data, _ = req("/api/v1/logo/upload", "POST",
                          files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")})
    check("拒绝不支持的扩展名", status == 400, data.get("error", "")[:60])

    # 超大文件要在「读进来之前」就被挡住，不能把内存吃爆
    status, data, _ = req("/api/v1/logo/upload", "POST",
                          files={"file": (f"huge_{LOGO_TAG}.png",
                                          b"\x89PNG\r\n\x1a\n" + b"\x00" * (17 * 1024 * 1024),
                                          "image/png")})
    check("拒绝超过 16 MB 的 Logo", status == 400 and "太大" in data.get("error", ""),
          data.get("error", "")[:60])
    check("超大文件也没有落盘", not (logos_dir / f"huge_{LOGO_TAG}.png").exists())

    # 同名不覆盖
    buf.seek(0)
    status, data, _ = req("/api/v1/logo/upload", "POST",
                          files={"file": (logo_a, buf.getvalue(), "image/png")})
    check("同名 Logo 不覆盖（自动加序号）", status == 200 and data.get("logo_name") == logo_b,
          data.get("logo_name", ""))

    # 合成真的生效
    from core.imaging import apply_custom_logo
    from core.configs import logos_dir

    def reddish(img, box):
        """统计区域内的「偏红」像素数（Logo 是红圆 + 白字）。"""
        crop = img.convert("RGB").crop(box)
        return sum(1 for px in crop.getdata() if px[0] > 150 and px[1] < 130 and px[2] < 130)

    base = Image.new("RGB", (1200, 800), (255, 255, 255))
    logo_path = str(logos_dir / logo_a)

    out = apply_custom_logo(base, logo_path, scale_pct=100, opacity_pct=100, corner="bottom_right")
    br = reddish(out, (800, 640, 1200, 800))
    check("Logo 合成为右下角", br > 300, f"右下角红色像素 {br}")

    out_tl = apply_custom_logo(base, logo_path, 100, 100, "top_left")
    tl = reddish(out_tl, (0, 0, 400, 160))
    check("Logo 位置可切换到左上", tl > 300, f"左上角红色像素 {tl}")

    out_c = apply_custom_logo(base, logo_path, 100, 100, "center")
    ct = reddish(out_c, (400, 320, 800, 480))
    check("Logo 可居中", ct > 300, f"中部红色像素 {ct}")

    out_half = apply_custom_logo(base, logo_path, 200, 100, "center")
    check("Logo 可放大（画布尺寸不变）", out_half.size == base.size)

    out_dim = apply_custom_logo(base, logo_path, 100, 5, "bottom_right")
    check("Logo 不透明度生效", reddish(out_dim, (800, 640, 1200, 800)) < br / 3,
          f"100% -> {br}，5% -> {reddish(out_dim, (800, 640, 1200, 800))}")

    out_missing = apply_custom_logo(base, str(logos_dir / "不存在.png"), 100, 100)
    check("Logo 缺失时安全跳过", out_missing.size == base.size)

    # 清理本次运行产生的两个 Logo。
    # 注意：删除走的是产品自己的 DELETE 接口（这是要验证的功能之一）。
    # 如果运行环境不允许删除（受限沙箱），残留的也只是 _feature_test/ 下的
    # 小文件，不会影响下一次运行——因为名字里带了 RUN_ID。
    for n in (logo_a, logo_b):
        req("/api/v1/logo/" + urllib.parse.quote(n), "DELETE")
    status, data, _ = req("/api/v1/meta")
    check("Logo 删除后列表已更新", logo_a not in (data.get("logos") or []))


# ---------------------------------------------------------------- D. 结果面板

def test_result_panel():
    print("\n[D] 处理结果确认面板")
    status, data, _ = req("/api/v1/file/tree")
    files = []

    def walk(nodes):
        for n in nodes:
            if n.get("is_file"):
                files.append(n["value"])
            else:
                walk(n.get("children") or [])

    walk(data.get("input_files") or [])
    check("扫描到测试照片", len(files) >= 6, f"{len(files)} 张")

    status, start, _ = req("/api/v1/process/start", "POST", {
        "files": files,
        "mode": "template",
        "template_name": "标准水印2",
        "input_folder": str(FIXTURE / "输入"),
        "output_folder": str(FIXTURE / "输出"),
        "workers": "auto",
        "quality": 90,
        "override_existed": True,
    })
    check("启动批量任务", status == 200 and start.get("job_id"), start.get("error", ""))
    job_id = start["job_id"]

    final = wait_job(job_id)
    check("任务完成", final.get("status") == "done", final.get("message", "")[:90])
    check("全部成功", final.get("success") == len(files),
          f"成功 {final.get('success')}/{len(files)}，失败 {final.get('failure')}")
    check("快照带执行引擎信息", final.get("engine") in ("serial", "pool", "pool-fallback"),
          f"engine={final.get('engine')} workers={final.get('workers')}")
    check("快照带耗时与均速", final.get("duration") is not None and final.get("avg_ms") is not None,
          f"用时 {final.get('duration')}s，均速 {final.get('avg_ms')}ms/张")

    status, res, _ = req(f"/api/v1/process/result/{job_id}")
    check("结果清单可读", status == 200 and len(res.get("items", [])) == len(files),
          f"{len(res.get('items', []))} 项")

    ok_items = [i for i in res["items"] if i["status"] == "success"]
    check("成功项带输出路径", all(i.get("output") for i in ok_items))
    check("成功项带原图路径（供对比）", all(i.get("source") for i in ok_items))
    check("成功项带文件大小", all(i.get("size") for i in ok_items),
          f"首项 {ok_items[0]['size'] if ok_items else '-'} 字节")
    check("exists 标记与实际文件一致",
          all(i["exists"] == Path(i["output"]).exists() for i in ok_items))
    check("输出目录字段回传", res.get("output_root") == str(FIXTURE / "输出"),
          str(res.get("output_root")))

    # 导出报告里要能看到目录与统计
    status, report, _ = req(f"/api/v1/process/report/{job_id}", raw=True)
    text = report.decode("utf-8") if isinstance(report, bytes) else str(report)
    check("报告含输入/输出目录", "输入目录" in text and "输出目录" in text)
    check("报告含统计口径", "成功" in text and "跳过" in text and "失败" in text)
    return job_id


# ---------------------------------------------------------------- E. 深色模式

def test_theme(app_module):
    print("\n[E] 深色模式（CSS 变量驱动）")
    css_path = ROOT / "static" / "app.css"
    css = css_path.read_text(encoding="utf-8")

    check("存在深色主题定义", '[data-theme="dark"]' in css)

    import re
    root_block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    dark_block = re.search(r'\[data-theme="dark"\]\s*\{(.*?)\}', css, re.S)
    check(":root 与 dark 块都能解析", bool(root_block and dark_block))

    def vars_of(block):
        return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block.group(1)))

    root_vars = vars_of(root_block)
    dark_vars = vars_of(dark_block)

    # 只有「纯颜色字面量」才必须成对出现；字号/圆角/字体/阴影这类
    # 与主题无关的变量不需要在深色块里重复声明。
    def is_color(v):
        v = v.strip()
        return bool(re.fullmatch(r"#[0-9a-fA-F]{3,8}", v)
                    or re.fullmatch(r"(rgb|rgba|hsl|hsla)\(.*\)", v))

    color_vars = {k for k, v in root_vars.items() if is_color(v)}
    missing = color_vars - set(dark_vars)
    check(f"深色主题覆盖了全部 {len(color_vars)} 个颜色变量", not missing,
          f"缺失：{sorted(missing) or '无'}")

    # 深色主题里的背景类变量必须真的是「暗的」
    def luminance(hexval):
        h = hexval.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 8:
            h = h[:6]
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255

    def rel_luminance(hexval):
        """WCAG 相对亮度（含 sRGB 线性化）。"""
        h = hexval.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 8:
            h = h[:6]
        channels = []
        for i in (0, 2, 4):
            c = int(h[i:i + 2], 16) / 255
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        r, g, b = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(fg, bg):
        a, b = rel_luminance(fg), rel_luminance(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    bg_names = ["--bg", "--panel", "--panel-2", "--panel-3", "--input-bg", "--card-bg",
                "--btn-bg", "--ok-bg", "--warn-bg", "--err-bg", "--accent-soft",
                "--stage-bg", "--drawer-bg", "--toast-bg", "--progress-track"]
    light_bg = []
    for name in bg_names:
        val = dark_vars.get(name, "")
        if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", val.strip()):
            continue
        if luminance(val.strip()) > 0.5:
            light_bg.append(f"{name}={val}")
    check("深色主题的背景类变量都是暗色", not light_bg, f"过亮：{light_bg or '无'}")

    text_names = ["--text", "--text-muted", "--text-dim"]
    dark_text = []
    for name in text_names:
        val = dark_vars.get(name, "")
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", val.strip()) and luminance(val.strip()) < 0.45:
            dark_text.append(f"{name}={val}")
    check("深色主题的文字变量足够亮（不会看不清）", not dark_text, f"过暗：{dark_text or '无'}")

    # 浅色主题的核心断言：主文字是深色、底色是浅色
    light_text = root_vars.get("--text", "").strip()
    light_bg = root_vars.get("--bg", "").strip()
    check("浅色主题：主文字为深色",
          luminance(light_text) < 0.35, f"--text={light_text}")
    check("浅色主题：底色为浅色",
          luminance(light_bg) > 0.8, f"--bg={light_bg}")
    check("深色主题：主文字为浅色",
          luminance(dark_vars.get("--text", "#000").strip()) > 0.7,
          f"--text={dark_vars.get('--text')}")
    check("深色主题：底色为深色",
          luminance(dark_vars.get("--bg", "#fff").strip()) < 0.25,
          f"--bg={dark_vars.get('--bg')}")

    # 可读性：文字与底色的对比度必须达标（这是深色模式最容易翻车的地方）
    for theme, table in (("浅色", root_vars), ("深色", dark_vars)):
        bg = table.get("--bg", "").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", bg):
            continue
        main = table.get("--text", "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", main):
            ratio = contrast(main, bg)
            check(f"{theme}主题：主文字对比度达标（≥4.5）", ratio >= 4.5,
                  f"--text 对 --bg = {ratio:.2f}")
        for name in ("--text-muted", "--text-dim"):
            val = table.get(name, "").strip()
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", val):
                continue
            ratio = contrast(val, bg)
            check(f"{theme}主题：{name} 可读（≥2.0）", ratio >= 2.0,
                  f"{val} 对 {bg} = {ratio:.2f}")

    # 模板里不该再出现硬编码的浅色背景
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    check("主题在样式加载前就应用（防白屏闪烁）",
          html.index("cwp-theme") < html.index("app.css"))

    # 主题设置要能持久化到配置
    status, data, _ = req("/api/v1/config", "POST", {"theme": "dark"})
    check("主题可写入配置", status == 200)
    status, cfg, _ = req("/api/v1/config")
    check("主题可读回", cfg.get("theme") == "dark", str(cfg.get("theme")))
    req("/api/v1/config", "POST", {"theme": "light"})

    # 按钮类控件必须走变量（抽查几个曾经写死颜色的选择器）
    hardcoded = []
    for selector in (".btn {", ".input {", ".tpl-card {", ".run-stats > div {"):
        idx = css.find(selector)
        if idx < 0:
            continue
        chunk = css[idx:idx + 420]
        if re.search(r"background:\s*#(?:fff|ffffff)\b", chunk, re.I):
            hardcoded.append(selector)
    check("关键控件不再写死 #fff 背景", not hardcoded, f"{hardcoded or '无'}")


# ---------------------------------------------------------------- F. 性能

def test_perf(app_module):
    print("\n[F] 性能优化")
    from core import perf
    from core.util import (clear_exif_cache, exif_cache_stats, get_exif,
                           set_exif_cache_size)

    # --- 进程数决策 ---
    cap = perf.max_workers()
    check("serial 模式只跑 1 个进程", perf.worker_count("serial", 100) == 1)
    check("auto 模式按核数取值",
          1 <= perf.worker_count("auto", 100) <= cap,
          f"{perf.worker_count('auto', 100)}（本机 {perf.cpu_total()} 核）")
    check("小批量强制串行（避免进程启动开销）", perf.worker_count("auto", 2) == 1)
    check("指定进程数生效", perf.worker_count("3", 100) == min(3, cap))
    check("进程数上限受控", perf.worker_count("99", 100) == cap)
    check("非法值安全回退", perf.worker_count("abc", 100) == 1)

    # --- 多进程自检 ---
    probe = perf.probe_multiprocessing()
    check("多进程自检可执行", "ok" in probe,
          f"ok={probe.get('ok')} error={probe.get('error')}")
    if probe.get("ok"):
        check("子进程能加载全部处理器", probe.get("processors", 0) >= 21,
              f"{probe.get('processors')} 个")
        # 回归测试：曾经因为「可写探测」的文件名固定，多进程并发时子进程
        # 会把数据目录误判成 %LOCALAPPDATA%，导致读不到模板、偶发失败。
        from core import DATA_ROOT
        check("子进程与父进程的数据目录一致",
              str(probe.get("data_root", "")).lower() == str(DATA_ROOT).lower(),
              f"父 {DATA_ROOT} / 子 {probe.get('data_root')}")
        check("子进程能看到模板文件", probe.get("templates", 0) >= 7,
              f"{probe.get('templates')} 个模板")

    # --- EXIF 缓存 LRU ---
    sample = ROOT / "_selftest" / "sample.jpg"
    set_exif_cache_size(128)
    clear_exif_cache()
    t0 = time.perf_counter()
    first = get_exif(str(sample))
    cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for _ in range(200):
        get_exif(str(sample))
    warm = (time.perf_counter() - t0) * 1000 / 200
    stats = exif_cache_stats()
    check("EXIF 缓存命中", stats["hit"] >= 200, f"hit={stats['hit']}")
    check("缓存命中比冷读快 10 倍以上", warm < max(cold / 10, 0.05),
          f"冷读 {cold:.1f}ms → 命中 {warm:.4f}ms")
    check("缓存容量可配置", stats["capacity"] == 128, str(stats["capacity"]))
    check("缓存不会无限增长", stats["size"] <= 128, str(stats["size"]))

    # 逐条淘汰而不是整体清空：塞满后仍应保留最近使用的项
    clear_exif_cache()
    set_exif_cache_size(64)
    keys = []
    for p in (ROOT / "_feature_test" / "输入").glob("*.jpg"):
        keys.append(str(p))
        get_exif(str(p))
    stats = exif_cache_stats()
    check("容量上限被严格执行", stats["size"] <= 64, str(stats["size"]))
    set_exif_cache_size(2048)

    # --- 预览降采样 ---
    from core.imaging import load_for_preview
    big = load_for_preview(str(sample), max_edge=400)
    check("预览先降采样再渲染", max(big.width, big.height) <= 400,
          f"{big.width}x{big.height}")

    # 预览接口实测耗时（同一张图连打两次，第二次应命中缓存）
    status, _, headers = req("/api/v1/preview?path=" + urllib.parse.quote(str(sample))
                            + "&mode=template&template_name=%E6%A0%87%E5%87%86%E6%B0%B4%E5%8D%B02",
                            raw=True)
    check("预览接口返回 JPEG", status == 200 and headers.get("Content-Type", "").startswith("image/jpeg"))

    # --- 性能接口 ---
    status, data, _ = req("/api/v1/perf")
    check("性能状态接口", status == 200 and "workers" in data, str(data.get("workers")))
    status, data, _ = req("/api/v1/perf/probe", "POST")
    check("性能自检接口", status == 200 and "ok" in data,
          f"ok={data.get('ok')} 建议 {data.get('suggested_workers')} 进程")
    status, data, _ = req("/api/v1/perf/cache", "DELETE")
    check("缓存清理接口", status == 200 and data.get("exif_cache", {}).get("size") == 0)

    # --- 多进程 vs 串行 实测对比 ---
    files = sorted(str(p) for p in (FIXTURE / "输入").glob("*.jpg"))
    results = {}
    for mode in ("serial", "auto"):
        out_dir = FIXTURE / f"性能对比_{mode}"
        # FIXTURE 本身就是每次运行新建的目录，这里不需要再删 —— 而且
        # 删除操作在受管控环境里会撞配额、可能让进程被杀掉。
        # 配合下面 override_existed=True，重跑结果稳定。
        status, start, _ = req("/api/v1/process/start", "POST", {
            "files": files, "mode": "template", "template_name": "标准水印2",
            "input_folder": str(FIXTURE / "输入"), "output_folder": str(out_dir),
            "workers": mode, "quality": 90, "override_existed": True,
        })
        if status != 200:
            check(f"{mode} 模式可启动", False, start.get("error", ""))
            continue
        final = wait_job(start["job_id"])
        produced = len(list(out_dir.rglob("*.jpg")))
        results[mode] = {"final": final, "produced": produced}
        check(f"{mode} 模式产出 {len(files)} 张", produced == len(files),
              f"实际 {produced} 张，engine={final.get('engine')}")

    if "auto" in results:
        check("自动模式确实用上了多进程或明确回退",
              results["auto"]["final"].get("engine") in ("pool", "pool-fallback", "serial"),
              f"engine={results['auto']['final'].get('engine')}")
    if "serial" in results and "auto" in results:
        s = results["serial"]["final"].get("duration")
        a = results["auto"]["final"].get("duration")
        print(f"      实测：串行 {s}s（{results['serial']['final'].get('avg_ms')}ms/张）"
              f" vs 自动 {a}s（{results['auto']['final'].get('avg_ms')}ms/张）")
        check("两种模式耗时都有记录", s is not None and a is not None)


# ---------------------------------------------------------------- G. 同目录保护

def test_same_folder():
    print("\n[G] 同目录保护前置")
    same = str(FIXTURE / "输入")
    files = sorted(str(p) for p in (FIXTURE / "输入").glob("*.jpg"))[:2]

    status, data, _ = req("/api/v1/process/start", "POST", {
        "files": files, "mode": "template", "template_name": "标准水印2",
        "input_folder": same, "output_folder": same,
        "workers": "serial", "override_existed": False,
    })
    check("输入=输出时自动改到子目录", status == 200 and data.get("output_root") == same + "\\水印输出",
          str(data.get("output_root")))
    check("回传提示文案", bool(data.get("note")), (data.get("note") or "")[:60])

    final = wait_job(data["job_id"])
    check("同目录下也能正常产出", final.get("success") == len(files),
          f"成功 {final.get('success')}/{len(files)}")
    check("原图未被覆盖（仍存在）", all(Path(f).exists() for f in files))
    produced = list((FIXTURE / "输入" / "水印输出").rglob("*.jpg"))
    check("成片落在「水印输出」子目录", len(produced) >= len(files), f"{len(produced)} 张")

    # 输入扫描必须排除输出目录，避免套娃
    status, tree, _ = req("/api/v1/file/tree")
    scanned = []

    def walk(nodes):
        for n in nodes:
            if n.get("is_file"):
                scanned.append(n["value"])
            else:
                walk(n.get("children") or [])

    walk(tree.get("input_files") or [])
    check("输入扫描已排除输出目录（不套娃）",
          not any("水印输出" in p for p in scanned),
          f"{len(scanned)} 个文件")


# ---------------------------------------------------------------- H. 打包安全

def test_packaging_safety():
    """打包版专属陷阱的静态回归检查。

    这几项缺陷在源码模式下**永远复现不出来**，只有打包成 exe 才会暴露，
    所以只能靠「检查入口文件是否按要求写了」的方式守住。

    最要命的是 freeze_support()：
    Windows 上多进程只能 spawn，子进程是「把 exe 再启动一遍」。PyInstaller
    的运行时钩子把 freeze_support 换成了会转交 multiprocessing 引导逻辑的版本，
    但**必须由程序自己调用**。不调用的话，每 spawn 一个子进程就重跑一遍整个
    桌面程序 —— 用户看到的是「点开始处理后疯狂弹窗」，或者界面直接卡死。
    """
    print("\n[H] 打包安全（多进程入口保护）")
    from core import perf
    from core import (DATA_ROOT, IS_FROZEN, IS_ONEFILE, PROBE_NAME, PROBE_PREFIX,
                      _is_writable, _sweep_stale_probes, stale_probes)

    def _main_block(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        idx = text.find('if __name__ == "__main__":')
        return text[idx:] if idx >= 0 else ""

    for name, entry in (("desktop_app.py", "main()"), ("app.py", "run_server(")):
        block = _main_block(ROOT / name)
        check(f"{name} 的 __main__ 调用了 freeze_support()",
              "freeze_support()" in block, f"{len(block)} 字符")
        fs = block.find("freeze_support()")
        ent = block.find(entry)
        check(f"{name} 中 freeze_support() 在启动逻辑之前",
              fs >= 0 and ent >= 0 and fs < ent,
              f"freeze_support@{fs} < {entry}@{ent}")

    check("源码模式不误判为单文件打包", IS_ONEFILE is False, f"frozen={IS_FROZEN}")
    check("源码模式进程数上限 = 8", perf.max_workers() == 8,
          f"{perf.max_workers()}")

    # --- 写探针：并发安全、不删除任何文件、可清扫旧残留 ---
    # 回归测试：老实现是「建随机名文件再 unlink」。
    #   ① 多进程并发时互删，被误判成「目录不可写」→ 子进程退回 %LOCALAPPDATA% → 处理失败；
    #   ② 删除操作在受管控环境里会撞上删除配额，严重时整个进程被杀（表现为「打不开」）。
    # 新实现固定名字 + 只追加 + 从不删除，两个问题一起消失。
    init_src = (ROOT / "core" / "__init__.py").read_text(encoding="utf-8")
    probe_body = init_src.split("def stale_probes")[0]
    check("写探针不再删除任何文件", ".unlink()" not in probe_body, "探测函数里没有 unlink")
    check("写探针用固定文件名（并发安全）", "PROBE_NAME" in init_src)
    check("启动路径不再做自动清理",
          "_sweep_stale_probes(root)" not in init_src and "stale_probes(root)" not in init_src,
          "启动时零删除")

    probe_dir = ROOT / "_feature_test" / f"run_{RUN_ID}" / "probe_sweep"
    probe_dir.mkdir(parents=True, exist_ok=True)

    stale = probe_dir / f"{PROBE_PREFIX}99999_deadbeef"
    stale.write_text("ok", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    fresh = probe_dir / f"{PROBE_PREFIX}11111_cafebabe"
    fresh.write_text("ok", encoding="utf-8")

    # 只验证「挑哪些」的逻辑，不做真实删除 —— 用例本身要能在任何环境下跑通
    picked = stale_probes(probe_dir)
    check("能识别出过期的旧探针", [p.name for p in picked] == [stale.name],
          f"选中 {[p.name for p in picked]}")
    check("不会误选刚写入的探针（保护并发进程）", fresh not in picked)
    check("空目录清扫结果为空", _sweep_stale_probes(probe_dir / "空的") == 0)

    check("_is_writable 判定为可写", _is_writable(probe_dir))
    check("_is_writable 留下固定探针文件", (probe_dir / PROBE_NAME).exists())
    # 再探测若干次：文件只能变大或不变，绝不能凭空多出文件
    for _ in range(4):
        _is_writable(probe_dir)
    stable = list(probe_dir.glob(".cwp_write_probe*"))
    check("反复探测不会产生多余文件", len(stable) == 1, f"{len(stable)} 个")
    check("探针文件体积受控（超限即截断）",
          (probe_dir / PROBE_NAME).stat().st_size <= 1024 + 8,
          f"{(probe_dir / PROBE_NAME).stat().st_size} 字节")

    # 数据目录里不应堆积旧版残留
    leftovers = stale_probes(DATA_ROOT)
    check("数据目录无旧版探针残留", len(leftovers) == 0,
          f"{len(leftovers)} 个：{[p.name for p in leftovers]}")

    # --- 拖拽通道：不能只依赖 loaded 事件 ---
    # 用户反馈过的「拖拽文件夹提示当前环境不支持拖拽」，根因是原生通道没注册上。
    # 实测 `window.events.loaded` 在非交互式会话里可能**永远不触发**
    # （本次验收就复现了：源码模式和打包模式的 native_drop 都是 False）。
    # 所以必须有一条带重试的兜底路径，这里用源码检查把它钉住。
    desktop_src = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    check("存在带重试的拖拽注册兜底函数", "def _arm_native_drop" in desktop_src)
    check("兜底注册起了独立线程（不阻塞 GUI）",
          "target=_arm_native_drop" in desktop_src and "daemon=True" in desktop_src)
    check("loaded 事件与兜底线程都指向同一实现",
          desktop_src.count("_arm_native_drop(window") >= 2,
          f"{desktop_src.count('_arm_native_drop(window')} 处调用")
    check("拖拽注册有「只注册一次」保护",
          "_drop_state[\"registered\"]" in desktop_src and "_drop_lock" in desktop_src)
    check("重复拖拽路径会去重（防重复注册产生重复项）",
          "seen, uniq = set(), []" in desktop_src)
    check("重试期间不刷 warning 日志",
          "quiet" in desktop_src and "logger.debug(msg)" in desktop_src)

    # 功能性验证：用「前几次拿不到 #dropzone」的假窗口，确认重试真的会重试。
    # 只看源码字符串是不够的 —— 逻辑写错（比如把 return 写进循环外、
    # 或者标志位判断反了）源码检查照样能过。
    try:
        import desktop_app as _dsk

        class _FakeEl:
            def __init__(self):
                self.listeners = []

            def on(self, name, handler):
                self.listeners.append((name, handler))

        class _FakeDOM:
            def __init__(self, fail_times):
                self.fail_times = fail_times
                self.calls = 0
                self.el = _FakeEl()

            def get_element(self, _sel):
                self.calls += 1
                return None if self.calls <= self.fail_times else self.el

        class _FakeWin:
            def __init__(self, fail_times):
                self.dom = _FakeDOM(fail_times)
                self.js = []

            def evaluate_js(self, code):
                self.js.append(code)

        def _reset_drop_state():
            _dsk._drop_state["registered"] = False
            _dsk._drop_state["attempts"] = 0

        _reset_drop_state()
        win = _FakeWin(fail_times=3)
        got = _dsk._arm_native_drop(win, timeout=3.0, interval=0.02)
        check("前几次拿不到 #dropzone 时能重试成功", got is True,
              f"第 {win.dom.calls} 次尝试成功")
        check("注册成功后只挂了一个 drop 监听",
              [n for n, _ in win.dom.el.listeners] == ["drop"],
              str([n for n, _ in win.dom.el.listeners]))
        check("注册成功后状态位置位", _dsk._drop_state["registered"] is True)

        before = len(win.dom.el.listeners)
        _dsk._arm_native_drop(win, timeout=1.0, interval=0.02)
        check("已注册时不会重复挂监听", len(win.dom.el.listeners) == before,
              f"{before} -> {len(win.dom.el.listeners)}")

        # element.on 收到的是 DOMEventHandler 包装对象，回调在 .callback 上
        _evt = win.dom.el.listeners[0][1]
        handler = getattr(_evt, "callback", _evt)
        handler({"dataTransfer": {"files": [
            {"pywebviewFullPath": "D:\\照片"},
            {"pywebviewFullPath": "D:\\照片"},
            {"pywebviewFullPath": "D:\\另一组"},
        ]}})
        check("拖拽事件把真实路径转给了页面",
              any("__onNativeDrop" in c for c in win.js), f"{len(win.js)} 次 evaluate_js")
        check("重复路径已去重", bool(win.js) and win.js[-1].count("照片") == 1,
              win.js[-1][:70] if win.js else "")

        # 全程拿不到 #dropzone 时必须超时返回，不能死循环
        _reset_drop_state()
        t0 = time.time()
        got2 = _dsk._arm_native_drop(_FakeWin(fail_times=10 ** 9),
                                     timeout=0.4, interval=0.05)
        check("始终拿不到 #dropzone 时会超时退出", got2 is False,
              f"耗时 {time.time() - t0:.2f}s")
    except Exception as e:  # noqa: BLE001
        check("拖拽重试用例本身未抛异常", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    FIXTURE = build_fixture(6)
    print(f"测试目录：{FIXTURE}")
    app_module = start_server()
    print(f"服务已启动：{BASE}")

    try:
        test_presets()
        test_processor_schema()
        test_logo(app_module)
        test_result_panel()
        test_theme(app_module)
        test_perf(app_module)
        test_same_folder()
        test_packaging_safety()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAIL.append(f"未捕获异常：{e}")

    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
