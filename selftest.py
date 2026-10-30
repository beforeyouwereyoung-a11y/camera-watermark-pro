"""开发自检脚本：生成带 EXIF 的测试图，跑通全部模板与新增处理器。"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw  # noqa: E402

from core.configs import templates_dir  # noqa: E402
from core.util import EXIFTOOL_PATH, get_exif, get_template_content  # noqa: E402
from core import imaging  # noqa: E402
from processor.core import get_all_processors, start_process  # noqa: E402

OUT = Path("_selftest")
OUT.mkdir(exist_ok=True)


def make_test_photo(path: Path, size=(1600, 1067)):
    img = Image.new("RGB", size, (30, 60, 110))
    draw = ImageDraw.Draw(img)
    for y in range(size[1]):
        t = y / size[1]
        draw.line([(0, y), (size[0], y)],
                  fill=(int(30 + 120 * t), int(60 + 80 * t), int(110 + 60 * (1 - t))))
    img.save(path, quality=95)


def write_exif(path: Path):
    args = [
        "-overwrite_original", "-quiet",
        "-Make=SONY", "-Model=ILCE-7M4",
        "-LensModel=FE 24-70mm F2.8 GM II",
        "-FNumber=2.8", "-ApertureValue=2.8",
        "-ISO=400", "-FocalLength=35", "-FocalLengthIn35mmFormat=35 mm",
        "-DateTimeOriginal=2026:09:15 18:32:07",
        "-CreateDate=2026:09:15 18:32:07",
        str(path),
    ]
    subprocess.check_output([str(EXIFTOOL_PATH), *args], stderr=subprocess.DEVNULL)
    # 曝光时间用 ExposureTime 写入（ShutterSpeed 是只读派生标签）
    subprocess.check_output([str(EXIFTOOL_PATH), "-overwrite_original", "-quiet",
                             "-ExposureTime=1/500", str(path)], stderr=subprocess.DEVNULL)


def main():
    print("=" * 60)
    print("处理器注册情况")
    print("=" * 60)
    procs = get_all_processors()
    for key in sorted(procs):
        print(f"  {key:<26} -> {procs[key].__name__}")
    print(f"  共 {len(procs)} 个")

    photo = OUT / "sample.jpg"
    make_test_photo(photo)
    write_exif(photo)

    exif = get_exif(str(photo))
    print("\nEXIF 读取结果（关键字段）:")
    for k in ("Make", "Model", "CameraModelName", "LensModel", "FNumber",
              "ISO", "FocalLengthIn35mmFormat", "DateTimeOriginal",
              "ImageWidth", "ImageHeight", "ShutterSpeed"):
        print(f"  {k:<26} = {exif.get(k)}")

    print("\n" + "=" * 60)
    print("模板渲染 + 处理测试")
    print("=" * 60)
    from core.util import build_context, render_template_json
    ok, fail = 0, 0
    for tpl in sorted(templates_dir.glob("*.json")):
        name = tpl.stem
        try:
            ctx = build_context(str(photo))
            pipeline = render_template_json(name, ctx)
            out = OUT / f"out_{name}.jpg"
            result = start_process(pipeline, input_path=str(photo), output_path=None, exif=ctx["exif"])
            imaging.save_output(result, out, source_path=str(photo), preserve_exif=True,
                                quality=95, subsampling=0)
            print(f"  [OK]   {name:<20} -> {result.size}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {name:<20} {type(e).__name__}: {e}")
            fail += 1

    print("\n" + "=" * 60)
    print("新增处理器测试")
    print("=" * 60)
    extras = [
        ("简单文字水印", [{"processor_name": "simple_text_watermark", "text": "© 2026 张三",
                           "position": "bottom_right", "color": "white", "opacity": 0.9,
                           "height": 60, "stroke_color": "black", "stroke_width": 3}]),
        ("平铺水印", [{"processor_name": "tile_watermark", "text": "SAMPLE ONLY",
                       "angle": -30, "opacity": 0.2, "color": "white"}]),
        ("不透明度+旋转", [{"processor_name": "rotate", "angle": 8, "background": "#00000000"},
                           {"processor_name": "opacity", "opacity": 0.5}]),
    ]
    for label, pipeline in extras:
        try:
            result = start_process(pipeline, input_path=str(photo), output_path=None)
            out = OUT / f"extra_{label}.jpg"
            imaging.save_output(result, out, source_path=str(photo), preserve_exif=False)
            print(f"  [OK]   {label:<16} -> {result.size}")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {label:<16} {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print("EXIF 保留验证")
    print("=" * 60)
    sample_out = OUT / "out_标准水印2.jpg"
    if sample_out.exists():
        out_exif = get_exif(str(sample_out))
        print(f"  输出文件: {sample_out}")
        print(f"  Make={out_exif.get('Make')} Model={out_exif.get('Model')} "
              f"ISO={out_exif.get('ISO')} Lens={out_exif.get('LensModel')}")

    print(f"\n模板结果: 成功 {ok} / 失败 {fail}")


if __name__ == "__main__":
    main()
