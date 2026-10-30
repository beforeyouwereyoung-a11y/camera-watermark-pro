"""对打包后的 exe 做端到端验证（纯标准库，走真实 HTTP）。

用法：把本文件放到 exe 同目录，先启动 exe，再执行：
    python e2e_packaged_test.py
若本机设置了 HTTP_PROXY，需要先排除回环地址：
    set NO_PROXY=127.0.0.1,localhost
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:15050"
HERE = Path(__file__).resolve().parent

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label} {extra}")
    else:
        fail += 1
        print(f"  [FAIL] {label} {extra}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.status, r.read()


def get_json(path):
    status, body = get(path)
    return status, json.loads(body)


def post_json(path, payload, method="POST"):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.loads(r.read())


print("=" * 62)
print("打包 exe 端到端验证")
print("=" * 62)

status, meta = get_json("/api/v1/meta")
check("GET /api/v1/meta", status == 200)
print(f"         版本={meta['version']} 便携={meta['portable']} 打包={meta['frozen']}")
print(f"         数据目录={meta['data_root']}")
check("便携模式生效", meta["portable"] is True)
check("打包模式生效", meta["frozen"] is True)
check("exiftool 可用", meta["exiftool_available"] is True, str(meta.get("exiftool_path")))

# 准备输入照片
#
# 每次运行都用带时间戳的新目录，而不是固定的 input/ output/。
# 原因：打包后 exe 所在目录就是 DATA_ROOT，固定目录会在第二次运行时
# 残留上一次的照片和成片，导致「扫描到 6 张而不是 3 张」「全部被跳过」
# 这类假失败。用新目录天然干净，也完全不需要删除任何东西。
RUN = time.strftime("%m%d-%H%M%S")
src = Path(os.environ.get("WM_SAMPLE", str(HERE / "_selftest" / "sample.jpg")))
if not src.exists():
    print(f"缺少样张 {src}，请把 _selftest/sample.jpg 一起放到 exe 目录")
    raise SystemExit(2)

data_root = Path(meta["data_root"])
input_dir = data_root / f"e2e_in_{RUN}"
output_dir = data_root / f"e2e_out_{RUN}"
input_dir.mkdir(parents=True, exist_ok=True)
output_dir.mkdir(parents=True, exist_ok=True)
for i in range(3):
    shutil.copy2(src, input_dir / f"photo_{i + 1}.jpg")
print(f"\n已放入 3 张测试照片 -> {input_dir}")

post_json("/api/v1/config", {"input_folder": str(input_dir), "output_folder": str(output_dir)})

status, tree = get_json("/api/v1/file/tree")
files = [f for f in tree["input_files"][0]["children"] if f.get("is_file")]
check("文件扫描", len(files) == 3, f"({len(files)} 张)")
paths = [f["value"] for f in files]

print("\n--- 缩略图 / 预览 ---")
status, body = get(f"/api/v1/thumb?path={urllib.parse.quote(paths[0])}&size=200")
check("缩略图", status == 200 and body[:2] == b"\xff\xd8", f"({len(body)} bytes)")
status, body = get(f"/api/v1/preview?path={urllib.parse.quote(paths[0])}&template_name="
                   + urllib.parse.quote("标准水印2") + "&mode=template")
check("模板预览", status == 200 and body[:2] == b"\xff\xd8", f"({len(body)} bytes)")
status, body = get(f"/api/v1/preview?path={urllib.parse.quote(paths[0])}&mode=simple"
                   "&text=" + urllib.parse.quote("© 2026 测试") + "&position=bottom_right")
check("自定义水印预览", status == 200 and body[:2] == b"\xff\xd8", f"({len(body)} bytes)")

print("\n--- 批量处理 ---")
payload = {
    "files": paths,
    "mode": "template",
    "template_name": "标准水印2",
    "output_format": "jpg",
    "name_pattern": "{name}_wm",
    "quality": 95,
    "long_edge": 1600,
    "preserve_exif": True,
    "override_existed": True,
    "crop_preset": "none",
}
status, job = post_json("/api/v1/process/start", payload)
check("启动任务", status == 200 and "job_id" in job, str(job))
job_id = job.get("job_id")

final = None
for _ in range(120):
    _, st = get_json(f"/api/v1/process/status/{job_id}")
    if st["status"] in ("done", "error", "cancelled"):
        final = st
        break
    time.sleep(0.3)

check("任务完成", final and final["status"] == "done", f"状态={final['status'] if final else '?'}")
if final:
    check("全部成功", final["success"] == 3 and final["failure"] == 0,
          f"成功={final['success']} 跳过={final['skipped']} 失败={final['failure']}")
    print(f"         耗时 {final['elapsed']} 秒")

outs = sorted(output_dir.glob("*_wm.jpg"))
check("输出文件生成", len(outs) == 3, f"({len(outs)} 个)")
for o in outs:
    print(f"         {o.name}  {o.stat().st_size / 1024:.0f} KB")

status, body = get(f"/api/v1/process/report/{job_id}")
check("失败报告接口", status == 200)

print("\n--- EXIF 保留验证 ---")
import subprocess
exiftool = Path(meta["exiftool_path"])
for o in outs[:2]:
    out = subprocess.check_output([str(exiftool), "-s", "-Make", "-Model", "-ISO",
                                   "-LensModel", "-Orientation", str(o)],
                                  stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
    print("         " + o.name + " -> " + " | ".join(out.split()))
    check(f"{o.name} 含 EXIF", "SONY" in out)
    check(f"{o.name} 方向正确", "Orientation" not in out or "Horizontal" in out or ": 1" in out)

# ---------------------------------------------------------------- 回归：用户反馈的两个问题
print("\n--- 回归：拖拽能力上报 ---")
check("meta 上报 native_drop 字段", "native_drop" in meta, str(meta.get("native_drop")))

print("\n--- 回归：输入目录 == 输出目录（用户实际踩到的坑）---")
same_dir = data_root / f"e2e_same_{RUN}"
same_dir.mkdir(parents=True, exist_ok=True)
for i in range(2):
    shutil.copy2(src, same_dir / f"same_{i + 1}.jpg")

post_json("/api/v1/config", {"input_folder": str(same_dir), "output_folder": str(same_dir)})
_, tree2 = get_json("/api/v1/file/tree")
same_files = [f["value"] for f in tree2["input_files"][0]["children"] if f.get("is_file")]
check("同目录场景扫描到 2 张", len(same_files) == 2, f"({len(same_files)} 张)")

status, job2 = post_json("/api/v1/process/start", {
    "files": same_files,
    "mode": "template",
    "template_name": "标准水印2",
    "input_folder": str(same_dir),
    "output_folder": str(same_dir),          # 和输入完全一样
    "override_existed": False,
    "output_format": "original",
    "name_pattern": "{name}",
    "preserve_exif": True,
    "crop_preset": "none",
    "simple": {},
})
check("启动同目录任务", status == 200 and bool(job2.get("output_root")), str(job2)[:120])
check("自动改到 水印输出 子目录",
      str(job2.get("output_root", "")).endswith("水印输出"), str(job2.get("output_root")))
check("返回了提示文案", bool(job2.get("note")), str(job2.get("note"))[:60])

final2 = None
for _ in range(120):
    _, st = get_json(f"/api/v1/process/status/{job2['job_id']}")
    if st["status"] in ("done", "error", "cancelled"):
        final2 = st
        break
    time.sleep(0.3)

if final2:
    print(f"         完成消息：{final2.get('message')}")
    check("同目录任务成功 2 张", final2["success"] == 2, f"成功={final2['success']}")
    check("没有静默跳过", final2["skipped"] == 0, f"跳过={final2['skipped']}")
    check("完成消息含输出目录", "水印输出" in (final2.get("message") or ""))
    sub_out = Path(final2["output_root"])
    produced = sorted(p.name for p in sub_out.glob("*.jpg")) if sub_out.exists() else []
    check("成片落在 水印输出 里", len(produced) == 2, str(produced))
    check("原图未被覆盖", not (same_dir / "same_1_wm.jpg").exists())
else:
    check("同目录任务完成", False, "超时")

print("\n--- 回归：/api/v1/drop 拖拽路径解析 ---")
status, dropped = post_json("/api/v1/drop", {"paths": [str(same_dir)]})
check("拖文件夹", status == 200 and dropped.get("folder") == str(same_dir), str(dropped)[:100])
status, dropped2 = post_json("/api/v1/drop", {
    "paths": [str(same_dir / "same_1.jpg"), str(same_dir / "same_2.jpg")]})
check("拖文件自动选中", status == 200 and len(dropped2.get("selected") or []) == 2,
      str(dropped2.get("selected"))[:100])
try:
    post_json("/api/v1/drop", {"paths": [str(same_dir / "nope.txt")]})
    check("无效路径返回 400", False)
except urllib.error.HTTPError as e:
    check("无效路径返回 400", e.code == 400, str(e.code))

# ================================================================ v3.2.0 新增
print("\n--- v3.2.0：多进程在打包环境下必须真的可用（最关键的一项）---")
# 这一项专门守住 PyInstaller + multiprocessing 的经典陷阱：
# 冻结环境下 spawn 子进程是「把 exe 再启动一遍」，如果入口没调用
# multiprocessing.freeze_support()，子进程会把整个程序重跑一遍 —— 表现是
# 疯狂弹窗或直接卡死。源码模式下永远测不出来，只有在这里才拦得住。
status, probe = post_json("/api/v1/perf/probe", {})
check("多进程自检接口可调用", status == 200, str(probe)[:140])
check("打包后子进程可正常启动", probe.get("ok") is True,
      f"ok={probe.get('ok')} error={str(probe.get('error'))[:120]}")
if probe.get("ok"):
    check("子进程数据目录与主进程一致",
          str(probe.get("data_root", "")).lower() == str(meta["data_root"]).lower(),
          f"主={meta['data_root']} 子={probe.get('data_root')}")
    check("子进程能加载全部处理器", probe.get("processors", 0) >= 21,
          f"{probe.get('processors')} 个")
    check("子进程能看到模板", probe.get("templates", 0) >= 7,
          f"{probe.get('templates')} 个")
else:
    check("多进程不可用时给出可读原因", bool(probe.get("error")),
          str(probe.get("error"))[:80])

print("\n--- v3.2.0：并行批处理确实走了多进程 ---")
multi_dir = data_root / f"e2e_multi_{RUN}"
multi_out = data_root / f"e2e_multi_out_{RUN}"
multi_dir.mkdir(parents=True, exist_ok=True)
multi_out.mkdir(parents=True, exist_ok=True)
multi_files = []
for i in range(6):
    p = multi_dir / f"m_{i + 1}.jpg"
    shutil.copy2(src, p)
    multi_files.append(str(p))
post_json("/api/v1/config", {"input_folder": str(multi_dir),
                             "output_folder": str(multi_out)})
status, job3 = post_json("/api/v1/process/start", {
    "files": multi_files, "mode": "template", "template_name": "标准水印2",
    "input_folder": str(multi_dir), "output_folder": str(multi_out),
    "workers": "auto", "output_format": "jpg", "name_pattern": "{name}_p",
    "override_existed": True, "preserve_exif": True, "crop_preset": "none",
})
check("启动并行任务", status == 200 and "job_id" in job3, str(job3)[:120])
final3 = None
for _ in range(300):
    _, st = get_json(f"/api/v1/process/status/{job3['job_id']}")
    if st["status"] in ("done", "error", "cancelled"):
        final3 = st
        break
    time.sleep(0.3)
if final3:
    check("并行任务全部成功", final3["success"] == 6 and final3["failure"] == 0,
          f"成功={final3['success']} 跳过={final3['skipped']} 失败={final3['failure']}")
    print(f"         引擎={final3.get('engine')} 进程数={final3.get('workers')} "
          f"耗时={final3.get('elapsed')}s")
    check("回报了执行引擎", final3.get("engine") in ("serial", "pool", "pool-fallback"),
          str(final3.get("engine")))
    if probe.get("ok") and probe.get("onefile") is False:
        # 文件夹便携版没有「每个子进程重新解包」的开销，应该真的并行起来
        check("文件夹版走了多进程", final3.get("engine") == "pool",
              f"engine={final3.get('engine')}")
    produced3 = sorted(multi_out.glob("m_*_p.jpg"))
    check("并行产出 6 张", len(produced3) == 6, f"{len(produced3)} 张")
else:
    check("并行任务完成", False, "超时")

print("\n--- v3.2.0：预设方案 ---")
status, presets = get_json("/api/v1/presets")
check("预设列表可读", status == 200 and len(presets.get("presets", [])) >= 6,
      f"{len(presets.get('presets', []))} 个")
names = [p["name"] for p in presets.get("presets", [])]
check("内置预设齐全", any("朋友圈" in n for n in names) and any("小红书" in n for n in names),
      str(names[:3]))
check("meta 里也带了预设", len(meta.get("presets", [])) >= 6)

print("\n--- v3.2.0：处理器元数据（可视化编辑器数据源）---")
status, procs = get_json("/api/v1/processors")
check("处理器元数据可读", status == 200)
plist = procs.get("processors", [])
check("元数据覆盖全部处理器", len(plist) >= 21, f"{len(plist)} 个")
check("每个处理器都有参数定义", all("params" in p and "label" in p for p in plist))
check("分类信息齐全", set(procs.get("categories", {}).keys()) >= {"filter", "generator", "merger"},
      str(list(procs.get("categories", {}).keys())))

print("\n--- v3.2.0：结果确认面板数据 ---")
status, items = get_json(f"/api/v1/process/result/{job_id}")
check("结果明细可读", status == 200)
rows = items.get("items", [])
check("结果条数与任务一致", len(rows) == 3, f"{len(rows)} 条")
check("每条都带输出路径", all(r.get("output") for r in rows))
check("每条都带原图路径", all(r.get("source") for r in rows))
check("回传了存在性与文件大小",
      all(r.get("exists") is True and r.get("size", 0) > 0 for r in rows))
check("缩略图可生成",
      get(f"/api/v1/thumb?path={urllib.parse.quote(rows[0]['output'])}&size=200")[0] == 200)

print("\n--- v3.2.0：自定义 Logo ---")
# 注意：Logo 列表不是独立接口，是跟着 /api/v1/meta 一起下发的
logo_names = meta.get("logos") or []
check("meta 里带出 Logo 列表", len(logo_names) >= 1, f"{len(logo_names)} 个")
if logo_names:
    status, body = get("/api/v1/logo/" + urllib.parse.quote(logo_names[0]))
    check("Logo 文件可下载", status == 200 and len(body) > 100, f"{len(body)} bytes")
    status, preview = get(
        f"/api/v1/preview?path={urllib.parse.quote(paths[0])}&mode=template"
        "&template_name=" + urllib.parse.quote("标准水印2")
        + "&logo_name=" + urllib.parse.quote(logo_names[0]))
    check("带 Logo 的预览可渲染", status == 200 and preview[:2] == b"\xff\xd8",
          f"{len(preview)} bytes")
else:
    check("Logo 文件可下载", False, "meta 未返回任何 Logo")

print("\n--- v3.2.0：深色模式变量成对 ---")
status, css = get("/static/app.css")
css_text = css.decode("utf-8", "ignore")
check("CSS 有浅色变量根", ":root" in css_text)
check("CSS 有深色覆盖块", '[data-theme="dark"]' in css_text)
status, html = get("/")
check("页面内联了主题预设脚本（防闪白）", b"cwp-theme" in html)

print("\n--- v3.2.0：性能设置与缓存 ---")
status, perf = get_json("/api/v1/perf")
check("性能设置可读", status == 200)
check("上报进程上限", perf.get("max_workers", 0) >= 1, str(perf.get("max_workers")))
check("上报 EXIF 缓存统计", "exif_cache" in perf, str(perf.get("exif_cache"))[:80])
check("单文件/文件夹形态已上报", "onefile" in perf, f"onefile={perf.get('onefile')}")
status, cleared = post_json("/api/v1/perf/cache", {}, method="DELETE")
check("EXIF 缓存可清空", status == 200)

print("\n--- v3.2.0：预览降采样（大图不再拖慢界面）---")
status, body = get(f"/api/v1/preview?path={urllib.parse.quote(paths[0])}"
                   "&mode=simple&text=" + urllib.parse.quote("降采样测试"))
check("降采样预览可用", status == 200 and body[:2] == b"\xff\xd8", f"{len(body)} bytes")

print("\n" + "=" * 62)
print(f"结果：通过 {ok} / 失败 {fail}")
print("=" * 62)
raise SystemExit(1 if fail else 0)
