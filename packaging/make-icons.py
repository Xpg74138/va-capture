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
    ring_r = int(S * 0.360)
    d.ellipse((cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r),
              outline=(70, 80, 100), width=max(2, int(S * 0.006)))

    # ── 摄像头：同心圆（圆形机身 + 镜头 + 瞳孔）──
    r_body = int(S * 0.158)      # 机身外圈
    d.ellipse((cx - r_body, cy - r_body, cx + r_body, cy + r_body),
              fill=BODY, outline=EDGE, width=max(3, int(S * 0.020)))

    r_lens = int(S * 0.103)      # 镜头环
    d.ellipse((cx - r_lens, cy - r_lens, cx + r_lens, cy + r_lens),
              fill=(12, 15, 20), outline=LENS, width=max(3, int(S * 0.022)))

    r_pup = int(S * 0.048)       # 瞳
    d.ellipse((cx - r_pup, cy - r_pup, cx + r_pup, cy + r_pup), fill=LENS)

    r_hi = int(S * 0.018)        # 中心暗点（高光反光）
    d.ellipse((cx - r_hi, cy - r_hi, cx + r_hi, cy + r_hi), fill=(12, 15, 20))

    # ── 机身右上角的录制红点 ──
    ang = math.radians(-45)
    rr_dot = max(3, int(S * 0.024))
    rx = cx + (r_body - int(S * 0.010)) * math.cos(ang)
    ry = cy + (r_body - int(S * 0.010)) * math.sin(ang)
    d.ellipse((rx - rr_dot, ry - rr_dot, rx + rr_dot, ry + rr_dot), fill=RED)

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
