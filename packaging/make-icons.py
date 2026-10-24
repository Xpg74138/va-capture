#!/usr/bin/env python3
"""用 PIL 生成多尺寸 PNG 图标。

设计：深色圆角渐变底 + 摄像头（镜头绿/蓝描边）+ 下方 8 点麦克风阵列环 + 中心红点。
超采样 4x 再缩小，边缘平滑。
"""
import math
import os
from PIL import Image, ImageDraw

TOP = (36, 42, 56)
BOT = (16, 19, 26)
LENS_RING = (74, 222, 128)
BODY_EDGE = (56, 189, 248)
BODY_FILL = (44, 51, 68)
MIC = (250, 204, 21)
RED = (248, 113, 113)

OUT = os.path.join(os.path.dirname(__file__), "icons")


def render(size):
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ── 圆角渐变底 ──
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / max(1, S - 1)
        gd.line([(0, y), (S, y)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOT)) + (255,))
    mask = Image.new("L", (S, S), 0)
    pad = int(S * 0.045)
    ImageDraw.Draw(mask).rounded_rectangle(
        (pad, pad, S - pad, S - pad), radius=int(S * 0.215), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # ── 摄像头机身 ──
    bx0, by0, bx1, by1 = int(S * .175), int(S * .215), int(S * .825), int(S * .525)
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=int(S * .085),
                        fill=BODY_FILL, outline=BODY_EDGE,
                        width=max(2, int(S * .018)))

    # ── 镜头（同心圆）──
    cx, cy = (bx0 + bx1) // 2, (by0 + by1) // 2
    r1 = int(S * .105)
    d.ellipse((cx - r1, cy - r1, cx + r1, cy + r1),
              fill=(13, 16, 22), outline=LENS_RING, width=max(3, int(S * .02)))
    r2 = int(S * .052)
    d.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), fill=LENS_RING)
    r3 = int(S * .020)
    d.ellipse((cx - r3, cy - r3, cx + r3, cy + r3), fill=(13, 16, 22))

    # ── 机身右上角的录制红点 ──
    rx, ry, rr = int(S * .762), int(S * .285), max(3, int(S * .026))
    d.ellipse((rx - rr, ry - rr, rx + rr, ry + rr), fill=RED)

    # ── 8 点麦克风阵列 ──
    mx, my = S // 2, int(S * .755)
    ring = int(S * .150)
    dot = max(3, int(S * .024))
    for i in range(8):
        a = math.radians(-90 + i * 45)
        px, py = mx + ring * math.cos(a), my + ring * math.sin(a)
        d.ellipse((px - dot, py - dot, px + dot, py + dot), fill=MIC)
    cr = max(3, int(S * .030))
    d.ellipse((mx - cr, my - cr, mx + cr, my + cr), fill=RED)

    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for s in (48, 64, 128, 256, 512):
        p = os.path.join(OUT, f"va-capture_{s}.png")
        render(s).save(p)
        print("✓", os.path.basename(p))


if __name__ == "__main__":
    main()
