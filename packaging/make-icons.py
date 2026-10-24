#!/usr/bin/env python3
"""生成多尺寸 PNG 图标。

构图：深色圆角渐变底
      · 正中间：摄像头（机身 + 镜头）
      · 周围一圈：8 颗麦克风，均匀分布成圆环
      · 麦克风与镜头中心之间画一圈淡淡的引导弧，视觉上连成一个阵列

用 4 倍超采样后缩小，任何尺寸边缘都平滑。
"""
import math
import os
from PIL import Image, ImageDraw

TOP = (36, 42, 56)
BOT = (15, 18, 25)
BODY = (44, 51, 68)
EDGE = (56, 189, 248)
LENS = (74, 222, 128)
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

    cx = cy = S // 2

    # ── 麦克风圆环的引导圈（很淡，只是把阵列"圈"起来）──
    ring_r = int(S * 0.355)
    d.ellipse((cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r),
              outline=(70, 80, 100), width=max(2, int(S * 0.006)))

    # ── 摄像头机身（水平放置，居中）──
    bw, bh = int(S * 0.185), int(S * 0.112)
    d.rounded_rectangle((cx - bw, cy - bh, cx + bw, cy + bh),
                        radius=int(S * 0.045), fill=BODY,
                        outline=EDGE, width=max(2, int(S * 0.016)))
    # 右侧取景小凸起
    d.rounded_rectangle((cx + bw - int(S * 0.010), cy - int(S * 0.048),
                         cx + bw + int(S * 0.065), cy + int(S * 0.048)),
                        radius=int(S * 0.020), fill=BODY, outline=EDGE,
                        width=max(2, int(S * 0.014)))

    # ── 镜头（同心圆，略偏机身左侧，看起来像相机）──
    lx = cx - int(S * 0.050)
    r1 = int(S * 0.072)
    d.ellipse((lx - r1, cy - r1, lx + r1, cy + r1),
              fill=(12, 15, 20), outline=LENS, width=max(3, int(S * 0.020)))
    r2 = int(S * 0.035)
    d.ellipse((lx - r2, cy - r2, lx + r2, cy + r2), fill=LENS)
    r3 = int(S * 0.013)
    d.ellipse((lx - r3, cy - r3, lx + r3, cy + r3), fill=(12, 15, 20))

    # ── 机身右上角的录制红点 ──
    rx, ry, rr = cx + int(S * 0.120), cy - int(S * 0.072), max(3, int(S * 0.021))
    d.ellipse((rx - rr, ry - rr, rx + rr, ry + rr), fill=RED)

    # ── 8 颗麦克风：均匀分布在圆环上 ──
    dot = max(3, int(S * 0.028))
    for i in range(8):
        a = math.radians(-90 + i * 45)
        px, py = cx + ring_r * math.cos(a), cy + ring_r * math.sin(a)
        # 深色描边让黄点在深底上更清晰
        d.ellipse((px - dot - 2, py - dot - 2, px + dot + 2, py + dot + 2),
                  fill=(22, 26, 34))
        d.ellipse((px - dot, py - dot, px + dot, py + dot), fill=MIC)

    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for s in (48, 64, 128, 256, 512):
        p = os.path.join(OUT, f"va-capture_{s}.png")
        render(s).save(p)
        print("✓", os.path.basename(p))


if __name__ == "__main__":
    main()
