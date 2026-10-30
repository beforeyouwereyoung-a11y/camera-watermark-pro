"""API 冒烟测试：走一遍真实 HTTP 层（用 Flask test_client）。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as server  # noqa: E402
from core.configs import load_config, save_config  # noqa: E402

ROOT = Path("_selftest").resolve()
(ROOT / "in").mkdir(parents=True, exist_ok=True)
(ROOT / "out").mkdir(parents=True, exist_ok=True)

# 把测试图片放进输入目录
sample = ROOT / "sample.jpg"
if sample.exists():
    (ROOT / "in" / "sample.jpg").write_bytes(sample.read_bytes())

cfg = load_config()
cfg.set("DEFAULT", "input_folder", str(ROOT / "in"))
cfg.set("DEFAULT", "output_folder", str(ROOT / "out"))
cfg.set("DEFAULT", "name_pattern", "{name}_wm")
cfg.set("DEFAULT", "override_existed", "True")
save_config(cfg)
server.config = load_config()

client = server.api.test_client()
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label} {extra}")
    else:
        fail += 1
        print(f"  [FAIL] {label} {extra}")


print("=" * 60)
print("基础接口")
print("=" * 60)
r = client.get("/")
check("GET /", r.status_code == 200, f"({len(r.data)} bytes)")
check("首页含标题", "相机水印" in r.data.decode("utf-8", "ignore"))

r = client.get("/api/v1/meta")
meta = r.get_json()
check("GET /api/v1/meta", r.status_code == 200)
print(f"         版本={meta.get('version')} 便携={meta.get('portable')} "
      f"exiftool={meta.get('exiftool_available')} 模板数={len(meta.get('templates', []))}")
check("模板已列出", len(meta.get("templates", [])) >= 7)

r = client.get("/api/v1/config")
cfg_json = r.get_json()
check("GET /api/v1/config", r.status_code == 200)
check("配置含模板内容", bool(cfg_json.get("template")))

r = client.get("/api/v1/file/tree")
tree = r.get_json()
check("GET /api/v1/file/tree", r.status_code == 200)
files = tree["input_files"][0]["children"]
check("扫描到输入文件", len(files) >= 1, f"({len(files)} 个)")

print("\n" + "=" * 60)
print("缩略图 / 原图 / 预览")
print("=" * 60)
p = str(ROOT / "in" / "sample.jpg")
r = client.get(f"/api/v1/thumb?path={p}&size=200")
check("GET /api/v1/thumb", r.status_code == 200 and r.data[:2] == b"\xff\xd8")

r = client.get(f"/api/v1/file?path={p}")
check("GET /api/v1/file", r.status_code == 200)

for tpl in ["标准水印2", "背景模糊", "右下角参数"]:
    r = client.get(f"/api/v1/preview?path={p}&template_name={tpl}&mode=template")
    check(f"预览[{tpl}]", r.status_code == 200 and r.data[:2] == b"\xff\xd8",
          f"({len(r.data)} bytes)")

r = client.get(f"/api/v1/preview?path={p}&mode=simple&text=%C2%A9%202026%20Test&position=bottom_right")
check("预览[简单水印]", r.status_code == 200 and r.data[:2] == b"\xff\xd8")

r = client.get(f"/api/v1/preview?path={p}&mode=simple&text=DEMO&tile=true")
check("预览[平铺水印]", r.status_code == 200 and r.data[:2] == b"\xff\xd8")

r = client.get(f"/api/v1/preview?path={p}&template_name=标准水印2&crop_preset=1:1")
check("预览[1:1 裁切]", r.status_code == 200 and r.data[:2] == b"\xff\xd8")

print("\n" + "=" * 60)
print("批量处理 + 进度 + 取消 + 报告")
print("=" * 60)
payload = {
    "files": [p],
    "mode": "template",
    "template_name": "标准水印2",
    "output_format": "jpg",
    "name_pattern": "{name}_wm",
    "quality": 95,
    "preserve_exif": True,
    "override_existed": True,
    "crop_preset": "none",
}
r = client.post("/api/v1/process/start", json=payload)
data = r.get_json()
check("POST /api/v1/process/start", r.status_code == 200 and "job_id" in data, str(data))
job_id = data.get("job_id")

if job_id:
    for _ in range(60):
        st = client.get(f"/api/v1/process/status/{job_id}").get_json()
        if st["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.2)
    check("任务完成", st["status"] == "done", f"状态={st['status']} {st['message']}")
    check("成功计数", st["success"] == 1, f"success={st['success']}")

    out_file = ROOT / "out" / "sample_wm.jpg"
    check("输出文件已生成", out_file.exists(), str(out_file))

    r = client.get(f"/api/v1/process/report/{job_id}")
    check("GET 失败报告", r.status_code == 200)

print("\n" + "=" * 60)
print("模板管理")
print("=" * 60)
r = client.post("/api/v1/template", json={"template_name": "_tmp_test", "content": "[]"})
check("新建模板", r.status_code == 200)
r = client.get("/api/v1/template/_tmp_test")
check("读取模板", r.status_code == 200)
r = client.post("/api/v1/template/validate", json={"content": "[{}]"})
check("校验合法 JSON", r.get_json().get("valid") is True)
r = client.post("/api/v1/template/validate", json={"content": "[{]"})
check("校验非法 JSON", r.get_json().get("valid") is False)
r = client.delete("/api/v1/template/_tmp_test")
check("删除模板", r.status_code == 200)

print("\n" + "=" * 60)
print(f"结果：通过 {ok} / 失败 {fail}")
print("=" * 60)
sys.exit(1 if fail else 0)
