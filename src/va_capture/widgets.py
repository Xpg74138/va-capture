"""自绘控件：电平表 / 波形 / 罗盘 / 瀑布图（numpy -> GdkPixbuf）"""
import collections
import numpy as np
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, GLib, Pango, GdkPixbuf, Gdk

from .constants import *
from .config import dir_name, conf_word

_DIGIT_FONT = {
    1: ["010", "110", "010", "010", "111"],
    2: ["111", "001", "111", "100", "111"],
    3: ["111", "001", "111", "001", "111"],
    4: ["101", "101", "111", "001", "001"],
    5: ["111", "100", "111", "001", "111"],
    6: ["111", "100", "111", "101", "111"],
    7: ["111", "001", "010", "010", "010"],
    8: ["111", "101", "111", "101", "111"],
    9: ["111", "101", "111", "001", "111"],
    0: ["111", "101", "101", "101", "111"],
}


def draw_digit(img, cx, cy, d, color, scale=2):
    """在 (cx,cy) 居中画一个 3x5 点阵数字"""
    pat = _DIGIT_FONT.get(int(d) % 10)
    if pat is None:
        return
    h = len(pat) * scale
    w = 3 * scale
    x0 = int(round(cx - w / 2))
    y0 = int(round(cy - h / 2))
    H, W = img.shape[:2]
    for ry, row in enumerate(pat):
        for rx, ch in enumerate(row):
            if ch != "1":
                continue
            for sy in range(scale):
                for sx in range(scale):
                    x = x0 + rx * scale + sx
                    y = y0 + ry * scale + sy
                    if 0 <= x < W and 0 <= y < H:
                        img[y, x] = color


def np_to_pixbuf(arr):
    """HxWx3 uint8 -> GdkPixbuf"""
    h, w, _ = arr.shape
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(arr.tobytes()), GdkPixbuf.Colorspace.RGB,
        False, 8, w, h, w * 3)





class Levels(Gtk.Image):
    """8 通道电平表：numpy 绘制到 pixbuf，不依赖 cairo"""

    def __init__(self, floor=-60.0):
        super().__init__()
        self.floor = floor
        self.db = np.full(AUDIO_CH, -120.0)
        # 固定渲染尺寸：不能用 get_allocation()，
        # 否则 pixbuf 变大 -> Gtk.Image 自然尺寸变大 -> 分配更大 -> 无限增长
        self._W, self._H = PANEL_W, LEVELS_H
        self.set_size_request(self._W, self._H)
        self._dirty = True
        GLib.timeout_add(83, self._tick)

    def update_db(self, db):
        self.db = db
        self._dirty = True

    def _tick(self):
        if self._dirty:
            self._render()
            self._dirty = False
        return True

    def _render(self):
        W, H = self._W, self._H
        img = np.zeros((H, W, 3), np.uint8)
        img[:, :] = (26, 28, 33)

        top, bottom = 8, 8
        row = (H - top - bottom) / AUDIO_CH
        label_w, bar_x = 30, 34
        bar_w = W - bar_x - 10

        for i in range(AUDIO_CH):
            y = int(top + i * row)
            bh = max(6, int(row - 8))
            cy = y + bh // 2

            # 左侧 mic 编号（点阵字模）
            draw_digit(img, 14, cy, i + 1, (198, 205, 218), scale=2)

            # 槽
            img[y:y + bh, bar_x:bar_x + bar_w] = (44, 48, 56)
            db = float(self.db[i])
            fl = float(self.floor)
            norm = max(0.0, min(1.0, (db - fl) / (-fl)))
            if norm > 0:
                if db > -6:
                    c = (255, 90, 77)
                elif db > -18:
                    c = (255, 192, 61)
                else:
                    c = (61, 220, 132)
                w = max(1, int(bar_w * norm))
                img[y:y + bh, bar_x:bar_x + w] = c

        self.set_from_pixbuf(np_to_pixbuf(img))


class Wave(Gtk.Image):
    """8 通道波形：numpy 画线到 pixbuf"""

    def __init__(self, span=2400):
        super().__init__()
        self.span = span
        self.buf = np.zeros((AUDIO_CH, span), dtype=np.float32)
        self._W, self._H = WAVE_W, 200
        self.set_size_request(self._W, self._H)
        self._dirty = True
        GLib.timeout_add(83, self._tick)

    def reset(self):
        self.buf[:] = 0.0
        self._dirty = True

    def push(self, a):
        n = a.shape[1]
        if n >= self.span:
            self.buf = a[:, -self.span:].copy()
        else:
            self.buf = np.roll(self.buf, -n, axis=1)
            self.buf[:, -n:] = a
        self._dirty = True

    def _tick(self):
        if self._dirty:
            self._render()
            self._dirty = False
        return True

    def _render(self):
        W, H = self._W, self._H
        img = np.zeros((H, W, 3), np.uint8)
        img[:, :] = (18, 20, 23)

        lane = H / AUDIO_CH
        n = self.buf.shape[1]
        xs = (np.arange(n) * (W - 1) / max(n - 1, 1)).astype(np.int32)

        for i in range(AUDIO_CH):
            cy = i * lane + lane / 2
            scale = lane * 0.42
            ys = np.clip((cy - self.buf[i] * scale).astype(np.int32), 0, H - 1)
            col = tuple(int(c * 255) for c in CH_COLORS[i])
            img[ys, xs] = col
            # 加粗一点
            ys2 = np.clip(ys + 1, 0, H - 1)
            img[ys2, xs] = col

        self.set_from_pixbuf(np_to_pixbuf(img))


# --------------------------------------------------------------------------
class Compass(Gtk.Overlay):
    """
    方位罗盘。

    - 0° 在正上方，逆时针为正（90°=左）
    - 没有明确声源时**不画指针**，避免"保留旧方向"造成误导
    - 渲染做了两层优化：静态部分(网格/刻度/外环)缓存复用，
      动态部分全部用 numpy 向量化，避免逐像素 Python 循环导致掉帧
    """

    TRAIL_LEN = 48
    REDRAW_MS = 50          # 20fps 足够，降低重绘负担

    def __init__(self):
        super().__init__()
        self.az = 0.0
        self._disp_az = 0.0
        self.conf = 0.0
        self.peaks = [(0.0, 0.0)]
        self.mic_db = np.full(AUDIO_CH, -120.0)
        self.angle_off_disp = 0.0
        self.has_fix = False
        self.show_fix = False          # 是否画指针（无明确声源时为 False）
        self.trail = collections.deque(maxlen=self.TRAIL_LEN)
        self._S = COMPASS_S
        self.set_size_request(self._S, self._S)

        self._img = Gtk.Image()
        self.add(self._img)

        for text, halign, valign in (
            ("前", Gtk.Align.CENTER, Gtk.Align.START),
            ("后", Gtk.Align.CENTER, Gtk.Align.END),
            ("右", Gtk.Align.END, Gtk.Align.CENTER),
            ("左", Gtk.Align.START, Gtk.Align.CENTER),
        ):
            lbl = Gtk.Label()
            lbl.set_markup(f"<span size='small' color='#8b93a3'>{text}</span>")
            lbl.set_halign(halign)
            lbl.set_valign(valign)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            lbl.set_margin_start(8)
            lbl.set_margin_end(8)
            self.add_overlay(lbl)
            self.set_overlay_pass_through(lbl, True)

        self._static = None
        self._cache_sz = -1
        self._dirty = True
        GLib.timeout_add(self.REDRAW_MS, self._tick)

    # ---- 外部接口 ----
    def set_mic_levels(self, db_phys):
        self.mic_db = np.asarray(db_phys, dtype=np.float64)
        self._dirty = True

    def set_azimuth(self, az, conf, peaks=None, valid=True):
        self.az = az
        self.conf = conf
        self.peaks = peaks or [(az, conf)]
        self.has_fix = True
        self.show_fix = bool(valid)
        if valid:
            self.trail.append((az, conf))
        self._dirty = True

    def clear_fix(self):
        """没有明确声源：不画指针"""
        self.show_fix = False
        self.trail.clear()
        self._dirty = True

    @staticmethod
    def _conf_color(conf):
        if conf >= 3.0:
            return (74, 222, 128)
        if conf >= 2.0:
            return (250, 204, 21)
        if conf >= 1.4:
            return (251, 146, 60)
        return (148, 150, 158)

    @staticmethod
    def _conf_sector(conf):
        return max(14.0, min(130.0 - 32.0 * min(conf, 3.0), 110.0))

    # ---- 渲染 ----
    def _tick(self):
        if self.has_fix and self.show_fix:
            diff = (self.az - self._disp_az + 180.0) % 360.0 - 180.0
            if abs(diff) > 0.5:
                self._disp_az = (self._disp_az + diff * 0.4) % 360.0
                self._dirty = True
            elif self._disp_az != self.az:
                self._disp_az = self.az
                self._dirty = True
        if self._dirty:
            self._render()
            self._dirty = False
        return True

    @staticmethod
    def _blit_disc(img, cx, cy, rad, col):
        H, W = img.shape[:2]
        x0 = max(0, int(cx - rad - 1)); x1 = min(W, int(cx + rad + 2))
        y0 = max(0, int(cy - rad - 1)); y1 = min(H, int(cy + rad + 2))
        if x1 <= x0 or y1 <= y0:
            return
        ys, xs = np.ogrid[y0:y1, x0:x1]
        m = (xs - cx) ** 2 + (ys - cy) ** 2 <= rad * rad
        img[y0:y1, x0:x1][m] = col

    @staticmethod
    def _blit_ring(img, cx, cy, rad, tol, col):
        H, W = img.shape[:2]
        rr = rad + tol + 1
        x0 = max(0, int(cx - rr)); x1 = min(W, int(cx + rr + 1))
        y0 = max(0, int(cy - rr)); y1 = min(H, int(cy + rr + 1))
        if x1 <= x0 or y1 <= y0:
            return
        ys, xs = np.ogrid[y0:y1, x0:x1]
        dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        m = np.abs(dist - rad) <= tol
        img[y0:y1, x0:x1][m] = col

    def _grid(self, S):
        if self._cache_sz != S:
            yy, xx = np.mgrid[0:S, 0:S]
            c = (S - 1) / 2.0
            self._cache_sz = S
            self._cache_xx = xx
            self._cache_yy = yy
            self._cache_c = c
            self._cache_d = np.sqrt((xx - c) ** 2 + (yy - c) ** 2)
        return self._cache_xx, self._cache_yy, self._cache_c, self._cache_d

    def _build_static(self, S):
        img = np.zeros((S, S, 3), np.uint8)
        img[:, :] = (16, 18, 21)
        yy, xx = np.mgrid[0:S, 0:S]
        c = (S - 1) / 2.0
        d = np.sqrt((xx - c) ** 2 + (yy - c) ** 2)
        r = S * 0.39

        for f in (0.34, 0.67, 1.0):
            shade = 70 if f < 1.0 else 110
            img[np.abs(d - r * f) < max(0.8, S / 320.0)] = (shade, shade + 4, shade + 12)

        for k in range(12):
            ang = np.deg2rad(k * 30.0)
            long_tick = (k % 3 == 0)
            r0 = r * (0.80 if long_tick else 0.89)
            ts = np.arange(r0, r * 0.98, 0.5)
            xs = np.round(c + ts * np.cos(ang)).astype(int)
            ys = np.round(c - ts * np.sin(ang)).astype(int)
            ok = (xs >= 0) & (xs < S) & (ys >= 0) & (ys < S)
            img[ys[ok], xs[ok]] = (170, 176, 190) if long_tick else (96, 102, 114)

        ci = int(round(c))
        img[ci - 2:ci + 3, ci - 2:ci + 3] = (226, 232, 240)
        img[ci - 1:ci + 2, ci - 1:ci + 2] = (16, 18, 21)
        return img

    def _render(self):
        S = self._S
        if self._static is None or self._static.shape[0] != S:
            self._static = self._build_static(S)
        img = self._static.copy()

        xx, yy, c, d = self._grid(S)
        r = S * 0.42
        dx, dy = xx - c, yy - c

        # ---- 外圈响度弧 ----
        lv_max = float(np.max(self.mic_db))
        if lv_max > -70:
            frac = float(np.clip((lv_max + 70.0) / 50.0, 0.0, 1.0))
            a0 = np.deg2rad(90.0 + 270.0)
            a1 = a0 + 2 * np.pi * frac
            ang = np.linspace(a0, a1, max(2, int(400 * frac)))
            for w in (0, 1):
                xs = np.round(c + (r * 1.05 + w) * np.cos(ang)).astype(int)
                ys = np.round(c - (r * 1.05 + w) * np.sin(ang)).astype(int)
                ok = (xs >= 0) & (xs < S) & (ys >= 0) & (ys < S)
                img[ys[ok], xs[ok]] = (int(60 + 190 * frac),
                                       int(150 - 40 * frac),
                                       int(200 - 160 * frac))

        # ---- 麦克风位置 ----
        rel = (np.clip((self.mic_db - (lv_max - 24.0)) / 24.0, 0.0, 1.0)
               if lv_max > -90 else np.zeros(AUDIO_CH))
        loudest = int(np.argmax(self.mic_db)) if lv_max > -90 else -1
        for i in range(AUDIO_CH):
            m_ang = np.deg2rad(90.0 + (i * 360.0 / AUDIO_CH + self.angle_off_disp))
            mx = c + (r * 0.70) * np.cos(m_ang)
            my = c - (r * 0.70) * np.sin(m_ang)
            v = rel[i]
            if v < 0.4:
                k2 = v / 0.4
                colm = (int(40 + 60 * k2), int(90 + 90 * k2), int(110 + 20 * k2))
            elif v < 0.75:
                k2 = (v - 0.4) / 0.35
                colm = (int(100 + 150 * k2), int(180 + 40 * k2), int(130 - 110 * k2))
            else:
                k2 = (v - 0.75) / 0.25
                colm = (250, int(220 - 120 * k2), int(40 - 20 * k2))
            rad = 2.5 + 4.5 * v + (1.5 if i == loudest else 0.0)
            self._blit_disc(img, mx, my, rad, colm)
            if i == loudest:
                self._blit_ring(img, mx, my, rad + 1.8, 1.0, (235, 240, 245))
            lab_col = (250, 252, 255) if i == loudest else (150, 158, 172)
            draw_digit(img, c + (r * 0.70 + rad + 9) * np.cos(m_ang),
                       c - (r * 0.70 + rad + 9) * np.sin(m_ang),
                       i + 1, lab_col, scale=2)

        # ---- 指针 / 置信扇形 / 轨迹：只在有明确声源时画 ----
        if self.show_fix:
            az = np.deg2rad(90.0 + self._disp_az)
            col = self._conf_color(self.conf)

            half = np.deg2rad(self._conf_sector(self.conf)) / 2.0
            # 图像坐标 y 向下，而绘制用 (c + r cosφ, c - r sinφ)，
            # 所以图像角的 y 分量要取反，否则扇形会相对指针上下镜像
            px_ang = np.arctan2(-dy, dx)
            delta = np.abs(np.mod(px_ang - az + np.pi, 2 * np.pi) - np.pi)
            sector = (delta <= half) & (d < r * 0.97) & (d > r * 0.12)
            if sector.any():
                base = img[sector].astype(np.float32)
                img[sector] = (base * 0.72 + np.array(col, np.float32) * 0.28
                               ).astype(np.uint8)

            n = len(self.trail)
            for i2, (taz, tconf) in enumerate(self.trail):
                if i2 >= n - 1:
                    continue
                t = np.deg2rad(90.0 + taz)
                tx = c + r * 0.86 * np.cos(t)
                ty = c - r * 0.86 * np.sin(t)
                fade = 0.25 + 0.55 * (i2 + 1) / max(n - 1, 1)
                cc = np.array(self._conf_color(tconf), np.float32) * fade
                self._blit_disc(img, tx, ty, 1.6, cc.astype(np.uint8))

            lv_frac = float(np.clip((lv_max + 70.0) / 50.0, 0.0, 1.0))
            thick = 1 if lv_frac < 0.5 else 2
            ts = np.arange(0, r * 0.90, 0.35)
            cos_a, sin_a = np.cos(az), np.sin(az)
            for w in range(-thick, thick + 1):
                xs = np.round(c + ts * cos_a - w * sin_a).astype(int)
                ys = np.round(c - ts * sin_a - w * cos_a).astype(int)
                ok = (xs >= 0) & (xs < S) & (ys >= 0) & (ys < S)
                img[ys[ok], xs[ok]] = col

            tipx = c + r * 0.90 * cos_a
            tipy = c - r * 0.90 * sin_a
            self._blit_disc(img, tipx, tipy, 2.6, col)

            for saz, sconf in self.peaks[1:]:
                t = np.deg2rad(90.0 + saz)
                sx = c + (r * 0.80) * np.cos(t)
                sy = c - (r * 0.80) * np.sin(t)
                self._blit_ring(img, sx, sy, 5.0, 0.9,
                                (np.array(self._conf_color(sconf), np.float32) * 0.75
                                 ).astype(np.uint8))

        self._img.set_from_pixbuf(np_to_pixbuf(img))


class Waterfall(Gtk.Image):
    """方位-时间瀑布图：纵轴方位(0°在前,朝上)，横轴时间，颜色=置信度"""

    def __init__(self, w=PANEL_W, h=WATERFALL_H):
        super().__init__()
        self.w, self.h = w, h
        self.buf = np.zeros((h, w, 3), np.uint8)
        self.buf[:, :] = (14, 16, 19)
        self.set_size_request(w, h)   # 固定尺寸，buf 也是这个尺寸
        self._dirty = True
        GLib.timeout_add(66, self._tick)

    @staticmethod
    def _color(conf):
        t = max(0.0, min(1.0, (conf - 1.0) / 3.0))
        # 暗蓝 -> 青 -> 黄
        r = int(20 + 235 * t * t)
        g = int(40 + 200 * t)
        b = int(90 + 120 * (1 - t))
        return (r, g, b)

    def clear(self):
        self.buf[:, :] = (14, 16, 19)
        self._dirty = True

    def push(self, az, conf):
        self.buf[:, :-1] = self.buf[:, 1:]
        y = int((az % 360.0) / 360.0 * (self.h - 1))
        col = self._color(conf)
        self.buf[y, -1] = col
        if 0 < y < self.h - 1:
            self.buf[y - 1, -1] = tuple(int(c * 0.5) for c in col)
            self.buf[y + 1, -1] = tuple(int(c * 0.5) for c in col)
        self._dirty = True

    def _tick(self):
        if self._dirty:
            self.set_from_pixbuf(np_to_pixbuf(self.buf))
            self._dirty = False
        return True


