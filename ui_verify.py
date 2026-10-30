"""界面验收：用真实 Chromium 打开页面，走一遍新功能并截图。

不只是截图 —— 同时收集浏览器控制台错误、断言关键 DOM 存在，
避免出现「看起来正常、其实 JS 早就报错」的假绿。

用例自带数据准备：会先通过接口把输入目录指向 _uidemo/照片，
不依赖机器上残留的 config.ini（否则会出现「用例红了但代码没问题」的假失败）。

运行：
    PLAYWRIGHT_BROWSERS_PATH=... python ui_verify.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

BASE = "http://127.0.0.1:15380"
ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "_uidemo" / "照片"
SHOTS = ROOT / "_uidemo" / "screenshots"


def _find_chrome() -> str:
    """按环境变量与常见安装位置自动定位 Chrome；可用 CHROME_PATH 覆盖。"""
    candidates = (
        os.environ.get("CHROME_PATH"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    )
    for p in candidates:
        if p and Path(p).exists():
            return p
    return ""


CHROME = _find_chrome()

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def safe_click(page, selector, timeout=8000) -> bool:
    """点击元素；元素不可见或超时就返回 False，而不是抛异常。

    验收脚本最怕「一个交互炸了，后面的用例全不跑、连总结都看不到」——
    那样只能看到一个 traceback，反而看不出到底哪几项坏了。
    """
    try:
        page.click(selector, timeout=timeout)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 点击 {selector} 失败：{str(e).splitlines()[0][:100]}")
        return False


def section(name, fn):
    """跑一个用例段，把异常收敛成 FAIL，保证后面的段落照常执行。"""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{name} 段落未抛异常", False, f"{type(e).__name__}: {e}")


def post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _input_photos() -> list[Path]:
    """demo 目录里真正会被界面列出的照片（排除输出子目录）。"""
    return [p for p in DEMO.rglob("*.jpg") if "水印输出" not in p.parts]


def prepare_demo() -> int:
    """准备一批 demo 照片，并把配置指向它。

    刻意**一次删除都不做**：
    1. `screenshots/` 要留着当验收证据，不能被清掉；
    2. 受管控环境对单次批量删除有配额，删几十个文件会被整进程拦下，
       用例就会因为「清目录」这种无关紧要的步骤整体失败。

    做法：目录原地复用 + 开启「覆盖已存在文件」。
    每次运行都重新拷一遍样张（覆盖同名文件，不算删除），输出也直接覆盖，
    所以重跑结果完全稳定。输出落在 `水印输出/` 子目录，界面扫描会排除它，
    不会出现「成片被当素材」的套娃问题。
    """
    DEMO.mkdir(parents=True, exist_ok=True)

    src = ROOT / "_selftest" / "sample.jpg"
    names = ["DSC_4821", "DSC_4822", "DSC_4823", "DSC_4824",
             "DSC_4825", "DSC_4826", "DSC_4827", "DSC_4828"]
    for n in names:
        shutil.copy2(src, DEMO / f"{n}.jpg")
    sub = DEMO / "2024 川西"
    sub.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, sub / "DSC_4901.jpg")

    post_json("/api/v1/config", {
        "input_folder": str(DEMO.resolve()),
        "output_folder": str((DEMO / "水印输出").resolve()),
        "name_pattern": "{name}",
        "override_existed": True,
        "output_format": "original",
        "workers": "auto",
        "theme": "light",
        "logo_name": "",
    })
    return len(_input_photos())


def main():
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    total_photos = prepare_demo()
    print(f"demo 照片：{total_photos} 张 → {DEMO}")

    errors: list[str] = []
    dialogs: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--force-device-scale-factor=1", "--hide-scrollbars"],
        )
        page = browser.new_page(viewport={"width": 1680, "height": 1000},
                                device_scale_factor=1.5)
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                if m.type in ("error",) else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # 确认类弹窗一律自动确认，避免脚本卡死
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept("UI 验收方案")))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)

        # ---------------- 1. 基础结构 ----------------
        print("\n[1] 页面结构")
        check("顶栏版本号已填充", page.inner_text("#appVersion").strip() not in ("", "-"),
              page.inner_text("#appVersion").strip())
        check("EXIF 引擎状态已显示", "exiftool" in page.inner_text("#chipExif"),
              page.inner_text("#chipExif"))
        check("性能芯片已显示", "并行" in page.inner_text("#chipPerf"),
              page.inner_text("#chipPerf"))
        check("主题切换器存在", page.locator("#themeSwitch .seg-btn").count() == 3)
        check("预设方案卡片已渲染", page.locator("#presetStrip .preset-card").count() >= 6,
              f"{page.locator('#presetStrip .preset-card').count()} 个")
        check("Logo 下拉已渲染", page.locator("#logoSelect option").count() >= 2,
              f"{page.locator('#logoSelect option').count()} 项")
        check("可视化编辑按钮存在", page.locator("#btnOpenEditor").is_visible())
        check("性能设置面板可展开", page.locator("#perfToggle").is_visible())
        check(f"扫描到 demo 照片（{total_photos} 张）",
              page.locator("#fileGrid .file-card").count() == total_photos,
              f"{page.locator('#fileGrid .file-card').count()} 张")
        check("预览图已渲染", page.locator("#imgSingle").get_attribute("src") not in (None, ""))
        page.screenshot(path=str(SHOTS / "01-浅色主题.png"))

        # ---------------- 2. 深色模式 ----------------
        print("\n[2] 深色模式")
        page.click('#themeSwitch .seg-btn[data-theme="dark"]')
        page.wait_for_timeout(700)
        theme = page.get_attribute("html", "data-theme")
        check("切换到深色主题", theme == "dark", str(theme))
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check("深色模式背景确实变暗", bg not in ("rgb(238, 241, 245)", "rgba(0, 0, 0, 0)"), bg)
        text_color = page.evaluate("getComputedStyle(document.body).color")
        check("深色模式文字为浅色", "255" in text_color or "230" in text_color, text_color)
        # 关键：深色下不该出现「白底白字」
        panel_bg = page.evaluate(
            "getComputedStyle(document.querySelector('.panel')).backgroundColor")
        check("面板底色跟随主题", panel_bg != "rgb(255, 255, 255)", panel_bg)
        check("主题写入 localStorage",
              page.evaluate("localStorage.getItem('cwp-theme')") == "dark")
        page.screenshot(path=str(SHOTS / "02-深色主题.png"))

        # 刷新后主题要保持（不能闪回浅色）
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("刷新后仍为深色主题",
              page.get_attribute("html", "data-theme") == "dark")
        page.click('#themeSwitch .seg-btn[data-theme="light"]')
        page.wait_for_timeout(600)

        # ---------------- 3. 预设方案 ----------------
        print("\n[3] 预设方案一键切换")
        before = page.input_value("#outFormat")
        page.click("#presetStrip .preset-card:has-text('网页轻量版')")
        page.wait_for_timeout(1400)
        check("套用预设后输出格式已切换",
              page.input_value("#outFormat") == "webp",
              f"{before} -> {page.input_value('#outFormat')}")
        check("套用预设后长边已切换", page.input_value("#outLongEdge") == "1600",
              page.input_value("#outLongEdge"))
        check("预设卡片显示选中态",
              page.locator("#presetStrip .preset-card.is-active").count() == 1)
        page.screenshot(path=str(SHOTS / "03-预设方案.png"))
        # 切回默认，避免影响后续截图
        page.click("#presetStrip .preset-card:has-text('朋友圈竖版')")
        page.wait_for_timeout(1200)

        # ---------------- 4. 同目录保护前置 ----------------
        print("\n[4] 同目录保护前置提示")
        folder = page.evaluate("document.getElementById('inFolder').value")
        page.fill("#outFolder", folder)
        page.dispatch_event("#outFolder", "change")
        page.wait_for_timeout(900)
        check("输入=输出时立刻弹出警告", page.locator("#outFolderNote").is_visible())
        check("警告文案说明了自动重定向",
              "水印输出" in page.inner_text("#outFolderNoteText"),
              page.inner_text("#outFolderNoteText")[:40])
        check("一键修复按钮可见", page.locator("#btnFixOutput").is_visible())
        page.screenshot(path=str(SHOTS / "04-同目录提示.png"))
        page.click("#btnFixOutput")
        page.wait_for_timeout(1200)
        check("一键修复把输出改到子目录",
              page.input_value("#outFolder").endswith("水印输出"),
              page.input_value("#outFolder"))
        check("修复后警告消失", not page.locator("#outFolderNote").is_visible())

        # ---------------- 5. Logo 面板 ----------------
        print("\n[5] 自定义 Logo")
        # v3.4.0 起「自定义 Logo」默认收起，先展开再操作
        page.click("#logoToggle")
        page.wait_for_timeout(400)
        check("Logo 面板可展开", page.locator("#logoBody").is_visible())
        page.select_option("#logoSelect", "sony.png")
        page.wait_for_timeout(400)
        check("选中 Logo 后提示更新",
              "sony.png" in page.inner_text("#logoHint"), page.inner_text("#logoHint")[:40])
        page.fill("#logoScale", "180")
        page.dispatch_event("#logoScale", "input")
        page.wait_for_timeout(900)
        check("大小滑杆联动显示", page.inner_text("#logoScaleVal") == "180%",
              page.inner_text("#logoScaleVal"))
        page.click('#logoCorner .chip-btn[data-corner="top_left"]')
        page.wait_for_timeout(1200)
        check("位置切换生效",
              page.locator('#logoCorner .chip-btn.is-active').inner_text() == "左上")
        check("预览信息带出 Logo 名称",
              "sony.png" in page.inner_text("#previewInfo"), page.inner_text("#previewInfo"))
        page.screenshot(path=str(SHOTS / "05-Logo面板.png"))
        page.select_option("#logoSelect", "")
        page.wait_for_timeout(800)
        # 收起，避免影响后续截图
        page.click("#logoToggle")
        page.wait_for_timeout(300)

        # ---------------- 6. 性能面板 ----------------
        print("\n[6] 性能面板与自检")
        page.click("#perfToggle")
        page.wait_for_timeout(500)
        check("性能面板展开", page.locator("#perfBody").is_visible())
        page.click("#btnPerfProbe")
        # 子进程 spawn + import 处理链在机械盘上可能要 10 秒以上
        try:
            page.wait_for_function(
                "() => !document.getElementById('perfStatus').textContent.includes('正在启动')",
                timeout=60000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)
        status = page.inner_text("#perfStatus")
        check("性能自检给出结论", "多进程" in status, status[:70])
        check("自检提示了建议进程数", "进程并行" in status or "串行" in status, status[:70])
        page.screenshot(path=str(SHOTS / "06-性能面板.png"))

        # ---------------- 7. 可视化编辑器 ----------------
        print("\n[7] 模板可视化编辑器")
        page.click("#btnOpenEditor")
        page.wait_for_timeout(2500)
        check("编辑器已打开", page.locator("#editorModal").is_visible())
        lib_count = page.locator("#editorLibList .editor-lib-item").count()
        check("处理器库已渲染", lib_count >= 21, f"{lib_count} 个")
        step_count = page.locator("#editorPipeline .step-card").count()
        check("当前模板步骤已载入", step_count >= 3, f"{step_count} 步")
        check("步骤数标签正确", page.inner_text("#editorStepCount") == f"{step_count} 步",
              page.inner_text("#editorStepCount"))
        # 参数面板：点第一个步骤
        page.click("#editorPipeline .step-card >> nth=0")
        page.wait_for_timeout(600)
        fields = page.locator("#editorParamBody .field").count()
        check("参数表单已按元数据渲染", fields >= 1, f"{fields} 个字段")
        check("参数面板标题是步骤号",
              "步骤 1" in page.inner_text("#editorParamTitle"),
              page.inner_text("#editorParamTitle"))
        page.screenshot(path=str(SHOTS / "07-可视化编辑器.png"))

        # 添加一个处理器
        page.fill("#editorSearch", "margin")
        page.wait_for_timeout(400)
        page.click("#editorLibList .editor-lib-item >> nth=0")
        page.wait_for_timeout(2200)
        check("可以添加处理步骤",
              page.locator("#editorPipeline .step-card").count() == step_count + 1,
              f"{page.locator('#editorPipeline .step-card').count()} 步")
        check("编辑器预览已渲染图片",
              page.locator("#editorPreview img").count() == 1)
        check("编辑器状态栏有耗时信息", "ms" in page.inner_text("#editorStatus"),
              page.inner_text("#editorStatus")[:60])

        # 上移 / 删除
        safe_click(page, "#editorPipeline .step-card >> nth=1 >> .step-btn[data-act='up']")
        page.wait_for_timeout(1800)
        safe_click(page, "#editorPipeline .step-card >> nth=0 >> .step-btn[data-act='del']")
        page.wait_for_timeout(1800)
        check("删除步骤后数量回落",
              page.locator("#editorPipeline .step-card").count() == step_count,
              f"{page.locator('#editorPipeline .step-card').count()} 步")
        page.screenshot(path=str(SHOTS / "08-编辑器改参数.png"))
        safe_click(page, "#btnEditorClose")
        page.wait_for_timeout(500)
        check("编辑器可关闭", not page.locator("#editorModal").is_visible())

        # ---------------- 8. 批量处理 + 结果面板 ----------------
        print("\n[8] 批量处理与结果确认面板")
        # 上一次运行已经在输出目录留下成片，而步骤 3 套用的预设会把
        # 「覆盖已存在的输出文件」重置为关闭 —— 那样这一批会被全部跳过，
        # 后面的缩略图 / 大图用例就全红了。这里显式勾上，保证用例可重复运行。
        # 「输出规则」默认收起，先展开。
        if not page.locator("#outBody").is_visible():
            page.click("#outToggle")
            page.wait_for_timeout(400)
        check("输出规则面板可展开", page.locator("#outBody").is_visible())
        if not page.is_checked("#outOverride"):
            page.check("#outOverride")
            page.wait_for_timeout(500)
        check("已开启「覆盖已存在的输出文件」", page.is_checked("#outOverride"))

        page.click("#btnSelectAll")
        page.wait_for_timeout(500)
        selected = page.inner_text("#selCounter")
        check("全选生效", f"已选 {total_photos} / {total_photos}" in selected, selected)
        page.click("#btnStart")
        # 等结果面板自动弹出
        try:
            page.wait_for_selector("#resultModal:not(.hidden)", timeout=150000)
        except Exception as e:  # noqa: BLE001
            check("结果面板自动弹出", False, str(e)[:80])
        page.wait_for_timeout(2000)
        check("结果面板已打开", page.locator("#resultModal").is_visible())
        summary = page.inner_text("#resultSummary")
        check("结果摘要含统计", "成功" in summary and "失败" in summary, summary[:80])
        check("结果摘要标注了执行引擎", "进程" in summary or "串行" in summary, summary[:80])
        cards = page.locator("#resultGrid .result-card").count()
        check(f"结果卡片已渲染（{total_photos} 张）", cards == total_photos, f"{cards} 张")
        thumbs = page.locator("#resultGrid .result-thumb").count()
        check("结果缩略图已加载", thumbs == total_photos, f"{thumbs} 张缩略图")
        check("全部标记为成功",
              page.locator("#resultGrid .result-status.success").count() == total_photos,
              f"{page.locator('#resultGrid .result-status.success').count()} 个成功标记")
        page.screenshot(path=str(SHOTS / "09-结果面板.png"))

        # 只看成功
        safe_click(page, "#btnResultOnlyOk")
        page.wait_for_timeout(700)
        check("「只看成功」筛选可切换",
              "is-active" in (page.locator("#btnResultOnlyOk").get_attribute("class") or ""))
        safe_click(page, "#btnResultOnlyOk")
        page.wait_for_timeout(500)

        # ---------------- 9. 大图查看 ----------------
        print("\n[9] 大图查看与单张操作")
        safe_click(page, "#resultGrid .result-card >> nth=0")
        page.wait_for_timeout(1800)
        check("大图查看已打开", page.locator("#lightbox").is_visible())
        check("大图已加载", page.locator("#lightboxImg").get_attribute("src") not in (None, ""))
        check("标题带出原图名", "原图" in page.inner_text("#lightboxTitle"),
              page.inner_text("#lightboxTitle")[:70])
        # 对比按钮只在「成片存在且有原图可对比」时可用
        check("对比按钮可用", not page.locator("#btnLbCompare").is_disabled())
        if safe_click(page, "#btnLbCompare"):
            page.wait_for_timeout(1200)
            check("对比原图可显示", page.locator("#lightboxBefore").is_visible())
            page.screenshot(path=str(SHOTS / "10-大图对比.png"))
        else:
            check("对比原图可显示", False, "对比按钮点击失败")
        safe_click(page, "#btnLbClose")
        page.wait_for_timeout(500)
        check("大图查看可关闭", not page.locator("#lightbox").is_visible())

        safe_click(page, "#btnResultClose")
        page.wait_for_timeout(500)

        # ---------------- 10. 折叠面板 ----------------
        # 先清掉记忆键再刷新，否则上一轮留下的「展开」状态会让「默认折叠」用例假红
        print("\n[10] 折叠面板（默认收起 + 状态记忆）")
        page.evaluate(
            "() => { ['logoToggle','cropToggle','outToggle'].forEach(k => "
            "localStorage.removeItem('cwp-collapse-' + k)); "
            "localStorage.removeItem('cwp-layout-cols'); }")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)

        for hid, bid, label in (("logoToggle", "logoBody", "自定义 Logo"),
                                ("cropToggle", "cropBody", "画幅裁切"),
                                ("outToggle", "outBody", "输出规则")):
            check(f"{label} 面板默认收起", page.locator(f"#{bid}").is_hidden())
            check(f"{label} 标题可点击", page.locator(f"#{hid}").is_visible())

        page.click("#logoToggle")
        page.wait_for_timeout(300)
        check("点击标题可展开面板", page.locator("#logoBody").is_visible())
        check("展开状态写入 localStorage",
              page.evaluate("localStorage.getItem('cwp-collapse-logoToggle')") == "open")

        # 刷新后要保持展开（这才叫「记忆」）
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("刷新后仍保持展开", page.locator("#logoBody").is_visible())
        if page.locator("#logoBody").is_visible():
            page.click("#logoToggle")
            page.wait_for_timeout(300)
        check("再次点击可收起", page.locator("#logoBody").is_hidden())

        page.click("#cropToggle")
        page.wait_for_timeout(250)
        check("画幅裁切面板可独立展开", page.locator("#cropBody").is_visible())
        page.click("#cropToggle")
        page.wait_for_timeout(250)
        page.screenshot(path=str(SHOTS / "11-折叠面板.png"))

        # ---------------- 11. 分隔条拖动 ----------------
        print("\n[11] 分隔条拖动调栏宽")
        check("两条分隔条均已渲染",
              page.locator(".splitter").count() == 2)
        check("分隔条带 keyboard 可达性",
              page.get_attribute("#splitLeft", "tabindex") == "0")

        def col_var():
            return page.evaluate(
                "document.querySelector('.layout').style.getPropertyValue('--col-settings')")

        def panel_w(sel):
            return page.evaluate(
                f"document.querySelector('{sel}').getBoundingClientRect().width")

        check("默认左栏宽 350px", col_var() == "350px", str(col_var()))
        w0 = panel_w(".panel-settings")
        box = page.locator("#splitLeft").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 90, cy, steps=10)
        page.mouse.up()
        page.wait_for_timeout(400)
        w1 = panel_w(".panel-settings")
        check("拖动左分隔条加宽左栏", w1 - w0 > 60, f"{w0:.0f}px -> {w1:.0f}px")

        # 右分隔条：往左拖是加宽预览栏
        p0 = panel_w(".panel-preview")
        box = page.locator("#splitRight").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx - 80, cy, steps=10)
        page.mouse.up()
        page.wait_for_timeout(400)
        p1 = panel_w(".panel-preview")
        check("拖动右分隔条加宽预览栏", p1 - p0 > 50, f"{p0:.0f}px -> {p1:.0f}px")

        cols = page.evaluate("JSON.parse(localStorage.getItem('cwp-layout-cols') || 'null')")
        check("栏宽已写入 localStorage",
              bool(cols) and abs(cols.get("settings", 0) - w1) < 6, str(cols))

        # 键盘也能调
        page.focus("#splitLeft")
        k0 = panel_w(".panel-settings")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(300)
        check("方向键可微调栏宽", panel_w(".panel-settings") - k0 > 6,
              f"{k0:.0f}px -> {panel_w('.panel-settings'):.0f}px")

        # 双击复位
        page.dblclick("#splitLeft")
        page.wait_for_timeout(400)
        check("双击分隔条复位默认宽", abs(panel_w(".panel-settings") - 350) < 8,
              f"{panel_w('.panel-settings'):.0f}px")
        page.dblclick("#splitRight")
        page.wait_for_timeout(400)
        check("右分隔条同样可复位", abs(panel_w(".panel-preview") - 420) < 8,
              f"{panel_w('.panel-preview'):.0f}px")
        page.screenshot(path=str(SHOTS / "12-分隔条.png"))

        # ---------------- 12. 对比模式对齐 ----------------
        print("\n[12] 对比模式：原图对齐到照片矩形")
        page.click('#previewMode .seg-btn[data-pview="compare"]')
        page.wait_for_timeout(1500)
        check("对比容器可见", page.locator("#compareBox").is_visible())
        check("效果图已渲染", page.locator("#imgAfter").get_attribute("src") not in (None, ""))
        check("原图已渲染", page.locator("#imgBefore").get_attribute("src") not in (None, ""))
        check("对比模式下单图预览被隐藏", page.locator("#imgSingle").is_hidden())

        box = page.evaluate(
            "() => { const el = document.getElementById('compareBox');"
            " return {w: el.clientWidth, h: el.clientHeight}; }")
        check("对比容器尺寸已按图片写入", box["w"] > 40 and box["h"] > 40,
              f"{box['w']}x{box['h']}")

        before_style = page.evaluate(
            "() => { const el = document.getElementById('imgBefore');"
            " return {left: el.style.left, top: el.style.top,"
            " width: el.style.width, height: el.style.height, fit: el.style.objectFit}; }")

        meta = page.evaluate(
            """async () => {
                 const url = window.buildPreviewUrl();
                 const r = await fetch(url.replace('/api/v1/preview?', '/api/v1/preview/meta?'));
                 return r.ok ? await r.json() : null;
               }""")
        check("几何接口有返回", bool(meta and meta.get("width")), str(meta)[:60])

        if meta and meta.get("photo_rect"):
            rect, W, H = meta["photo_rect"], meta["width"], meta["height"]

            def pct_val(text):
                """浏览器会把 '0.0000%' 归一化成 '0%'、'100.0310%' 归一化成
                '100.031%'，所以必须按数值比，不能比字符串。"""
                try:
                    return float(str(text).rstrip("%"))
                except (TypeError, ValueError):
                    return None

            exp = [rect[0] / W * 100, rect[1] / H * 100,
                   rect[2] / W * 100, rect[3] / H * 100]
            got = [pct_val(before_style["left"]), pct_val(before_style["top"]),
                   pct_val(before_style["width"]), pct_val(before_style["height"])]
            for i, axis in enumerate(("left", "top", "width", "height")):
                ok = got[i] is not None and abs(got[i] - exp[i]) < 0.01
                check(f"原图 {axis} 对齐照片矩形", ok,
                      f"{got[i]} vs {exp[i]:.4f}")
            check("原图用 cover 摆放（裁切模板也能对齐）",
                  before_style["fit"] == "cover", str(before_style["fit"]))
            check("对比容器保持效果图宽高比",
                  abs(box["w"] / box["h"] - W / H) / (W / H) < 0.03,
                  f"{box['w'] / box['h']:.3f} vs {W / H:.3f}")
        else:
            # 探针算不出照片位置（文字水印/拼接类模板）时退回整幅摆放，
            # 至少保证「分割线左右分布」，不能出现一张盖住另一张
            check("无几何信息时退回整幅 contain",
                  before_style["fit"] == "contain" and before_style["width"] == "100%",
                  str(before_style))

        # 分割线定位（点击位置决定分割比例）
        cbox = page.locator("#compareBox").bounding_box()
        page.mouse.click(cbox["x"] + cbox["width"] * 0.3, cbox["y"] + cbox["height"] * 0.5)
        page.wait_for_timeout(400)
        handle_left = page.evaluate(
            "parseFloat(document.getElementById('compareHandle').style.left)")
        check("点击可移动分割线", abs(handle_left - 30) < 4, f"{handle_left:.1f}%")
        clip = page.evaluate(
            "getComputedStyle(document.getElementById('compareBefore')).clipPath")
        check("左侧原图按分割线裁剪", "70" in clip, clip)

        # 拖动分割线
        hbox = page.locator("#compareHandle").bounding_box()
        hx, hy = hbox["x"] + hbox["width"] / 2, hbox["y"] + hbox["height"] / 2
        page.mouse.move(hx, hy)
        page.mouse.down()
        page.mouse.move(hx + cbox["width"] * 0.3, hy, steps=10)
        page.mouse.up()
        page.wait_for_timeout(400)
        moved = page.evaluate(
            "parseFloat(document.getElementById('compareHandle').style.left)")
        check("拖动分割线改变比例", moved > handle_left + 10,
              f"{handle_left:.1f}% -> {moved:.1f}%")
        page.screenshot(path=str(SHOTS / "13-对比对齐.png"))

        # ---------------- 13. 双击放大预览 ----------------
        print("\n[13] 双击放大预览")
        # 切回「效果」再双击：对比模式下舞台点击会移动分割线，容易误触
        page.click('#previewMode .seg-btn[data-pview="after"]')
        page.wait_for_timeout(600)
        check("默认未处于放大态",
              not page.evaluate("document.body.classList.contains('preview-zoom')"))
        page.dblclick("#previewStage", position={"x": 60, "y": 60})
        page.wait_for_timeout(600)
        check("双击进入放大态",
              page.evaluate("document.body.classList.contains('preview-zoom')"))
        zoom = page.evaluate(
            "() => { const el = document.querySelector('.panel-preview');"
            " const r = el.getBoundingClientRect();"
            " return {pos: getComputedStyle(el).position, w: r.width, h: r.height, top: r.top}; }")
        check("放大后预览栏铺满视口",
              zoom["pos"] == "fixed" and zoom["w"] >= 1600 and abs(zoom["top"]) < 2,
              str(zoom))
        page.screenshot(path=str(SHOTS / "14-预览放大.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        check("Esc 退出放大态",
              not page.evaluate("document.body.classList.contains('preview-zoom')"))
        check("退出后恢复三栏布局",
              page.evaluate("getComputedStyle(document.querySelector('.panel-preview')).position")
              != "fixed")

        # ---------------- 14. 清空全部内容 ----------------
        print("\n[14] 清空全部内容")
        check(f"清空前有 {total_photos} 张照片",
              page.locator("#fileGrid .file-card").count() == total_photos,
              f"{page.locator('#fileGrid .file-card').count()} 张")
        # 刷新之后文件是「未勾选」状态（预览用的是 current，与 selected 无关），
        # 先全选，才能验证「清空全部内容」确实把勾选也一起清了
        page.click("#btnSelectAll")
        page.wait_for_timeout(500)
        check("全选后可清空勾选状态",
              f"已选 {total_photos} / {total_photos}" in page.inner_text("#selCounter"),
              page.inner_text("#selCounter"))
        page.click("#btnClearAll")
        page.wait_for_timeout(900)
        check("清空不弹二次确认（按决策直接清）",
              not [d for d in dialogs if "清空" in d or "确认" in d], str(dialogs))
        check("照片列表已清空", page.locator("#fileGrid .file-card").count() == 0,
              f"{page.locator('#fileGrid .file-card').count()} 张")
        check("选择计数归零", "已选 0 / 0" in page.inner_text("#selCounter"),
              page.inner_text("#selCounter"))
        check("预览回到空态", page.locator("#previewEmpty").is_visible())
        check("对比容器已隐藏", page.locator("#compareBox").is_hidden())
        check("效果图已移除",
              page.locator("#imgSingle").is_hidden()
              and page.locator("#imgSingle").get_attribute("src") in (None, ""))
        check("进度统计归零", page.inner_text("#statDone").strip() == "0",
              page.inner_text("#statDone"))
        check("就绪提示已复位", page.inner_text("#runMessage").strip() == "就绪",
              page.inner_text("#runMessage"))
        check("结果面板已关闭", page.locator("#resultModal").is_hidden())
        page.screenshot(path=str(SHOTS / "15-清空全部内容.png"))

        # ---------------- 15. 控制台干净 ----------------
        print("\n[15] 浏览器控制台")
        real_errors = [e for e in errors if "favicon" not in e.lower()]
        check("没有 JS 报错", not real_errors, "；".join(real_errors[:3]) or "无")

        browser.close()

    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print(f"截图目录：{SHOTS}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
