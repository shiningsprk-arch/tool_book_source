# -*- coding: utf-8 -*-
"""生成工具图标 `icon.png`（包根目录，宿主动态工具固定读这个名字）。

设计：圆角方底 + 蓝紫渐变 + 白色翻开的书 + 琥珀色放大镜 —— 一眼能认出"书 + 搜索/取源"。
全部用几何图元手绘，不引入任何第三方图标资源（外部工具包要能离线自包含）。4 倍超采样后
缩小，边缘不会有锯齿。

用法：
    python scripts/make_icon.py            # 生成 1024x1024 的 icon.png
    python scripts/make_icon.py --size 512 --preview
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)

TOP_COLOR = (42, 111, 224)      # #2A6FE0 顶部
BOTTOM_COLOR = (18, 60, 140)    # #123C8C 底部
ACCENT = (255, 197, 61)         # #FFC53D 放大镜
PAPER = (255, 255, 255)
PAGE_LINE = (168, 194, 235)     # 书页上的横线（浅蓝）

SS = 4  # 超采样倍数


def make_icon(size=1024):
    from PIL import Image, ImageDraw

    S = size * SS
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 圆角方底 + 垂直渐变
    gradient = Image.new("RGB", (1, S))
    px = gradient.load()
    for y in range(S):
        ratio = y / float(S - 1)
        px[0, y] = tuple(
            int(round(TOP_COLOR[i] + (BOTTOM_COLOR[i] - TOP_COLOR[i]) * ratio)) for i in range(3)
        )
    gradient = gradient.resize((S, S))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.219), fill=255)
    canvas.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    def s(v):
        return v * SS

    # ── 书：两页对开 + 书脊 ──────────────────────────────────────────────
    left_x0, right_x1 = s(150), s(770)
    spine_l, spine_r = s(451), s(469)
    top_y, bot_y = s(268), s(690)
    spine_dip = s(24)  # 书脊处纸张稍低，做出"翻开"的弧感

    left_page = [
        (left_x0, top_y + spine_dip),
        (spine_l, top_y),
        (spine_l, bot_y),
        (left_x0, bot_y - spine_dip),
    ]
    right_page = [
        (spine_r, top_y),
        (right_x1, top_y + spine_dip),
        (right_x1, bot_y - spine_dip),
        (spine_r, bot_y),
    ]
    draw.polygon(left_page, fill=PAPER)
    draw.polygon(right_page, fill=PAPER)

    # 书页横线（只画在页内，避免压到书脊）
    for i in range(1, 5):
        y = top_y + spine_dip + (bot_y - top_y - spine_dip * 2) * i / 5.0
        inset = s(34)
        draw.line([(left_x0 + inset, y), (spine_l - inset, y)], fill=PAGE_LINE, width=max(2, int(s(9))))
        draw.line([(spine_r + inset, y), (right_x1 - inset, y)], fill=PAGE_LINE, width=max(2, int(s(9))))

    # 书脊
    draw.rectangle([spine_l, top_y, spine_r, bot_y], fill=(226, 235, 250))

    # ── 放大镜：压在书的右下角 ──────────────────────────────────────────
    cx, cy, r = s(700), s(622), s(164)
    ring = s(52)
    draw.ellipse([cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring], fill=ACCENT)
    # 镜片：露出底色但压暗一点，做出玻璃感
    glass = gradient.crop((0, 0, S, S)).point(lambda v: int(v * 0.82))
    glass_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glass_mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=235)
    canvas.paste(glass, (0, 0), glass_mask)

    # 手柄
    hx0, hy0 = cx + (r + ring * 0.45) * 0.707, cy + (r + ring * 0.45) * 0.707
    hx1, hy1 = s(892), s(816)
    draw.line([(hx0, hy0), (hx1, hy1)], fill=ACCENT, width=int(s(74)))
    draw.ellipse([hx1 - s(37), hy1 - s(37), hx1 + s(37), hy1 + s(37)], fill=ACCENT)

    return canvas.resize((size, size), Image.LANCZOS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--out", default=os.path.join(PKG_ROOT, "icon.png"))
    parser.add_argument("--preview", action="store_true", help="另外写一份 256px 预览便于肉眼检查")
    args = parser.parse_args()

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise SystemExit("需要 Pillow：pip install Pillow（本机可用系统 Python 3.13，已带 PIL）")

    icon = make_icon(args.size)
    icon.save(args.out, "PNG", optimize=True)
    print("已写出 %s (%dx%d, %d B)" % (args.out, args.size, args.size, os.path.getsize(args.out)))

    if args.preview:
        preview_path = os.path.join(os.path.dirname(args.out), "icon_preview_128.png")
        icon.resize((128, 128), Image.LANCZOS).save(preview_path, "PNG")
        print("预览: %s" % preview_path)


if __name__ == "__main__":
    sys.exit(main())
