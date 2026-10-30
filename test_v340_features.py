"""v3.4.0 新增能力的专项测试。

覆盖：
  1. 命名规则新增 {template} —— 同一张图换模板不再被判「已存在」而跳过
  2. safe_filename_token —— 模板名里的非法字符要清洗，不能写出非法路径
  3. lens_short —— 镜头型号截断规则
  4. /api/v1/preview/meta —— 对比模式对齐所需的「照片矩形」要与成片尺寸自洽
  5. 几何追踪 —— 会改变画布的处理器要同步矩形，无法追踪的类型要退回 None

运行：python test_v340_features.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    from core import imaging
    from core.jinja2renders import lens_short
    from processor.core import start_process
    from core.util import build_context, get_exif, render_pipeline_json

    root = os.path.dirname(os.path.abspath(__file__))
    sample = os.path.join(root, "_selftest", "in", "sample.jpg")

    print("=" * 68)
    print("[1] 命名规则 {template}")
    print("=" * 68)
    tmp_out = tempfile.mkdtemp(prefix="wm_name_")
    try:
        p1 = imaging.resolve_output_name(sample, os.path.dirname(sample), tmp_out,
                                        name_pattern="{name}_{template}", template_name="标准水印2")
        p2 = imaging.resolve_output_name(sample, os.path.dirname(sample), tmp_out,
                                        name_pattern="{name}_{template}", template_name="画框Pro-中")
        check("同图换模板 -> 输出路径不同", p1 != p2, f"{os.path.basename(p1)} vs {os.path.basename(p2)}")
        check("旧规则 {name} -> 路径与模板无关",
              imaging.resolve_output_name(sample, os.path.dirname(sample), tmp_out,
                                          name_pattern="{name}", template_name="A")
              == imaging.resolve_output_name(sample, os.path.dirname(sample), tmp_out,
                                             name_pattern="{name}", template_name="B"))

        print()
        print("=" * 68)
        print("[2] safe_filename_token 清洗非法字符")
        print("=" * 68)
        bad = imaging.safe_filename_token('A/B:C*D?E"F<G>H|I')
        check("非法字符全部替换", "/" not in bad and ":" not in bad and "*" not in bad, bad)
        check("空值有兜底", imaging.safe_filename_token("") == "未命名")
        check("正常中文名不变", imaging.safe_filename_token("画框Pro-中") == "画框Pro-中")

        print()
        print("=" * 68)
        print("[3] lens_short 截断规则")
        print("=" * 68)
        cases = [
            ("NIKKOR Z DX 50-250mm f/4.5-6.3 VR", "NIKKOR Z DX 50-250mm"),
            ("FE 24-70mm F2.8 GM II", "FE 24-70mm"),
            ("RF24-105mm F4 L IS USM", "RF24-105mm"),
            ("TAMRON 28-75mm F/2.8 Di III RXD", "TAMRON 28-75mm"),
            ("NIKKOR Z DX 50-250mm", "NIKKOR Z DX 50-250mm"),   # 无光圈标记 -> 原样
            ("EF-S 18-55mm", "EF-S 18-55mm"),                    # 不能把 EF-S 里的 F 当光圈
        ]
        for raw, want in cases:
            got = lens_short(raw)
            check(f"{raw[:30]} -> {want}", got == want, f"实际 {got}")
        check("空值 -> '-'", lens_short(None) == "-" and lens_short("") == "-")

        print()
        print("=" * 68)
        print("[4] 几何追踪与成片自洽")
        print("=" * 68)
        source = imaging.load_for_preview(sample, max_edge=1400)
        exif = get_exif(sample)
        tpl_dir = os.path.join(root, "config", "templates")

        expect_rect = {
            "标准水印2": True, "标准水印Pro": True, "画框Pro-中": True,
            "标准水印": True, "logo居中": True, "文件夹名+右下角参数": True,
            # 文字/拼接类模板重排画布，无法用平移表达，必须退回 None（前端用保守排布）
            "右下角参数": False, "背景模糊": False, "尼康专用背景模糊": False,
        }
        for name, want in expect_rect.items():
            path = os.path.join(tpl_dir, name + ".json")
            pipeline = render_pipeline_json(json.load(open(path, encoding="utf-8")),
                                            build_context(sample))
            out, meta = start_process(pipeline, input_path=sample, output_path=None,
                                      exif=exif, initial_buffer=[source.copy()],
                                      return_meta=True)
            rect = meta["photo_rect"]
            gt = os.path.join(tempfile.gettempdir(), f"wm_geo_{abs(hash(name))}.png")
            out.save(gt)
            from PIL import Image
            with Image.open(gt) as chk:
                size_ok = chk.size == (out.width, out.height)
            os.remove(gt)

            if want:
                ok = bool(rect) and size_ok and rect[0] >= 0 and rect[1] >= 0 \
                     and rect[0] + rect[2] <= out.width + 1 and rect[1] + rect[3] <= out.height + 1
                detail = f"画布 {out.size} 矩形 {tuple(round(v) for v in rect)}" if rect else "矩形缺失"
            else:
                ok = rect is None and size_ok
                detail = "按预期退回 None"
            check(f"{name} 追踪{'有效' if want else '退回 None'}", ok, detail)

        # 矩形宽高比必须等于原图宽高比（否则前端 cover 摆放会裁掉内容）
        for name in ("标准水印2", "标准水印Pro", "画框Pro-中"):
            pipeline = render_pipeline_json(
                json.load(open(os.path.join(tpl_dir, name + ".json"), encoding="utf-8")),
                build_context(sample))
            _, meta = start_process(pipeline, input_path=sample, output_path=None,
                                    exif=exif, initial_buffer=[source.copy()], return_meta=True)
            r = meta["photo_rect"]
            want = source.width / source.height
            got = r[2] / r[3]
            check(f"{name} 矩形比例 == 原图比例", abs(got - want) < 0.005,
                  f"{got:.4f} vs {want:.4f}")
    finally:
        shutil.rmtree(tmp_out, ignore_errors=True)

    print()
    print("=" * 68)
    print("[5] 预览接口：效果图尺寸与 photo_rect 自洽")
    print("=" * 68)
    import app as app_module
    app_module.api.config["TESTING"] = True
    client = app_module.api.test_client()

    for name in ("标准水印2", "标准水印Pro", "画框Pro-中", "标准水印"):
        resp = client.get("/api/v1/preview/meta", query_string={
            "mode": "template", "template_name": name,
            "path": sample, "crop_preset": "none",
        })
        ok = resp.status_code == 200
        data = resp.get_json() if ok else {}
        rect = (data or {}).get("photo_rect")
        w, h = (data or {}).get("width"), (data or {}).get("height")
        ok = ok and bool(rect) and bool(w) and bool(h)
        if ok:
            ok = all(0 <= v <= 1.001 for v in (rect[0] / w, rect[1] / h)) and \
                 rect[0] + rect[2] <= w + 1 and rect[1] + rect[3] <= h + 1
        check(f"/api/v1/preview/meta [{name}]", ok,
              f"画布 {w}x{h} 矩形 {[round(v) for v in rect] if rect else None}")

    # 同一份渲染缓存：先取图再取几何，几何信息不应缺失
    up = client.get("/api/v1/preview", query_string={
        "mode": "template", "template_name": "标准水印2", "path": sample})
    mp = client.get("/api/v1/preview/meta", query_string={
        "mode": "template", "template_name": "标准水印2", "path": sample})
    check("缓存命中时几何信息仍完整（图片接口在前）",
          up.status_code == 200 and mp.status_code == 200 and bool(mp.get_json().get("photo_rect")))

    print()
    print("=" * 68)
    print("[6] 模板清单：画框Pro 只剩「中」")
    print("=" * 68)
    templates = app_module.list_templates()
    names = [t if isinstance(t, str) else t.get("name") for t in templates]
    check("画框Pro-中 存在", "画框Pro-中" in names, str(sorted(names)))
    check("画框Pro-细 已移除", "画框Pro-细" not in names)
    check("画框Pro-粗 已移除", "画框Pro-粗" not in names)

    print()
    print("=" * 68)
    print(f"结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
    print("=" * 68)
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
