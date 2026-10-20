"""声源定位与事件检测（纯 numpy，不依赖 GTK）"""
import collections
import numpy as np

from .constants import AUDIO_CH, AUDIO_RATE

def band_level(block, rate=AUDIO_RATE, lo=300.0, hi=3000.0):
    """
    带通能量：只统计 300~3000Hz（犬吠/猪咳的主要能量区 + 起音宽带成分）。

    比全带 RMS 好在：房间噪声（空调、风扇、电路）多为宽带或极低频，
    滤掉带外分量能明显提高信噪比，从而在 3~4 米外也能触发。
    """
    n = block.shape[1]
    X = np.fft.rfft(block, axis=1)
    f = np.fft.rfftfreq(n, 1.0 / rate)
    m = (f >= lo) & (f <= hi)
    if not m.any():
        return np.zeros(block.shape[0], dtype=np.float32)
    return (np.sqrt((np.abs(X[:, m]) ** 2).sum(axis=1)) / n).astype(np.float32)


class EventDetector:
    """
    声音事件检测：用于咳嗽、犬吠这类短促事件。

    原理：维护一个慢速跟踪的噪声底，当瞬时电平突然高出噪声底
    若干倍时判定事件开始；电平回落或超时判定事件结束。
    起音瞬间是宽带的，正好适合小阵列做时延估计。
    """

    START_RATIO = 2.2      # 起音门限（相对慢速噪声底）
    END_RATIO = 1.55       # 结束门限（越接近起音门限，事件越短）
    MIN_LEVEL = 0.0008     # 绝对电平门限
    CONFIRM_BLOCKS = 2     # 需连续 2 块超门限
    RISE_RATIO = 2.2       # 50ms 内的陡升倍数 —— 区分"事件起音"与"环境缓慢起伏"
    RISE_LAG = 5           # 陡升比较的回溯块数（5 × 10ms = 50ms）
    MAX_DUR = 1.5          # 事件最长持续时间（秒）
    MIN_DUR = 0.06         # 事件最短时长（低于此视为咔哒声，丢弃）
    REFRACTORY = 1.2       # 事件结束后的静默期（抑制同一持续声源反复触发）

    def __init__(self):
        self._hist = collections.deque(maxlen=24)   # 最近 240ms 的电平
        self._hit = 0
        self.noise = None
        self.active = False
        self.t_start = 0.0
        self.peak = 0.0
        self._t_end = -99.0

    def feed(self, level, now):
        """level: 当前块的最大通道 RMS；返回 'start'/'end'/None"""
        if self.noise is None:
            self.noise = max(level, 1e-6)
            return None

        if not self.active:
            # 噪声底慢速跟踪（只在静默时更新，避免被事件抬高）
            self.noise = 0.985 * self.noise + 0.015 * level
            self._hist.append(level)
            ratio = level / (self.noise + 1e-9)
            # 陡升判据：咳嗽/犬吠起音在几十毫秒内急升，
            # 而空调/人声起伏是缓慢的，用它抑制误报
            rise = 0.0
            if len(self._hist) > self.RISE_LAG:
                prev = self._hist[-1 - self.RISE_LAG]
                rise = level / (prev + 1e-9)
            if (ratio > self.START_RATIO and level > self.MIN_LEVEL
                    and rise > self.RISE_RATIO):
                self._hit += 1
            else:
                self._hit = 0
            if (self._hit >= self.CONFIRM_BLOCKS
                    and now - self._t_end > self.REFRACTORY):
                self.active = True
                self._hit = 0
                self.t_start = now
                self.peak = level
                return "start"
            return None

        self.peak = max(self.peak, level)
        ratio = level / (self.noise + 1e-9)
        if ratio < self.END_RATIO or now - self.t_start > self.MAX_DUR:
            self.active = False
            self._t_end = now
            dur = now - self.t_start
            if dur < self.MIN_DUR:
                return None          # 太短，当作咔哒声丢弃
            return "end"
        return None


class Locator:
    """
    多算法声源定位器（8 麦 / 45mm 半径阵列）。

    支持四种方法，可在设置里切换：

    - **srp**    SRP-PHAT：成对互相关求和。最稳健、最省算力（实测 1.5°）。
    - **music**  宽带 MUSIC：8×8 协方差矩阵特征分解，用噪声子空间。
                 峰值更尖锐，但依赖模型精度。
    - **mvdr**   MVDR/Capon：最小方差无失真响应，理论上分辨率最高。
    - **fusion** 融合：SRP 与 MUSIC 的归一化谱相乘，兼顾稳健与锐度。

    所有方法共享的前端处理：

    1. **限带 80Hz~12kHz**，边缘余弦过渡。
       带宽不能太窄：时域分辨率 ≈ 1/带宽，而 9cm 孔径最大时延只有 0.26ms。
       实测（语音带宽 8kHz + 全带噪声）：
         不限带   SNR20:1.25°  SNR0:6.25°
         100-8000       1.88°       4.38°
         80-12000       1.67°       3.75°   <- 综合最优
         80-16000       1.46°       3.96°

    2. **亚采样时延**：互相关 4 倍零填充上采样，时延分辨率 0.25 采样点。

    3. **β 加权 PHAT**：|X|^-0.7，保留部分幅度信息，低信噪比更稳。

    4. **抛物线插值**：峰值附近拟合，输出精度从网格 1.25° 提升到 0.1°。
    """

    def __init__(self, n_mics=AUDIO_CH, radius=0.045, rate=AUDIO_RATE,
                 nfft=1024, speed=343.0, step_deg=2.5, angle_offset=0.0,
                 upsample=4, fmin=80.0, fmax=12000.0, beta=0.7,
                 method="srp", n_sources=1, sub_bins=64):
        self.n = n_mics
        self.nfft = nfft
        self.rate = rate
        self.speed = speed
        self.step = step_deg
        self.angle_offset = angle_offset
        self.upsample = upsample
        self.npad = nfft * upsample
        self.beta = beta
        self.method = method
        self.n_sources = n_sources
        self.sub_bins = sub_bins

        ang = np.deg2rad(np.arange(0.0, 360.0, step_deg))
        self.angles = ang

        th = 2 * np.pi * np.arange(n_mics) / n_mics
        self.pos = np.stack([radius * np.cos(th), radius * np.sin(th)], axis=1)
        pos = self.pos

        self.pairs = [(i, j) for i in range(n_mics) for j in range(i + 1, n_mics)]
        n_pairs = len(self.pairs)

        # ---- SRP 用：每对麦克风在各候选角度下的时延索引 ----
        proj = np.zeros((n_pairs, ang.size), dtype=np.float64)
        u = np.stack([np.cos(ang), np.sin(ang)], axis=1)
        for k, (i, j) in enumerate(self.pairs):
            proj[k] = (pos[i] - pos[j]) @ u.T
        delay = -(proj / speed) * rate
        self.lag_idx = (np.round(delay * upsample).astype(np.int32)) % self.npad
        self._ii = np.array([q[0] for q in self.pairs], dtype=np.intp)
        self._jj = np.array([q[1] for q in self.pairs], dtype=np.intp)

        # ---- 子空间方法用：导向矢量 (A, F, M) ----
        freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
        band = np.where((freqs >= fmin) & (freqs <= fmax))[0]
        idx = np.linspace(0, band.size - 1, min(sub_bins, band.size)).astype(int)
        self.sub_freqs = freqs[band[idx]]
        usize = np.stack([np.cos(ang), np.sin(ang)], axis=1)          # (A,2)
        tau = -(pos[None, :, :] @ usize[:, :, None])[:, :, 0] / speed  # (A,M)
        self.A = np.exp(-2j * np.pi * self.sub_freqs[None, :, None]
                        * tau[:, None, :])                             # (A,F,M)

        # 限带权重（SRP 用）
        w = np.zeros_like(freqs)
        w[(freqs >= fmin) & (freqs <= fmax)] = 1.0
        taper = 200.0
        lo = (freqs > fmin) & (freqs < fmin + taper)
        w[lo] = 0.5 - 0.5 * np.cos(np.pi * (freqs[lo] - fmin) / taper)
        hi = (freqs > fmax - taper) & (freqs < fmax)
        w[hi] = 0.5 + 0.5 * np.cos(np.pi * (freqs[hi] - (fmax - taper)) / taper)
        self.band = w.astype(np.float32)

        self.win = np.hanning(nfft).astype(np.float32)
        self.srp = np.zeros(ang.size, dtype=np.float32)
        self.gain_cal = np.ones(n_mics, dtype=np.float32)
        self.channel_map = list(range(n_mics))
        self.peaks = [(0.0, 0.0)]
        self.valid = False

    # ---------------- 前端 ----------------
    def _prep(self, block):
        ordered = block[self.channel_map, :self.nfft].astype(np.float32)
        return ordered * self.gain_cal[:, None] * self.win

    # ---------------- 各类空间谱 ----------------
    def _spec_srp(self, frames):
        n_pairs = len(self.pairs)
        R = np.zeros((n_pairs, self.npad), dtype=np.float32)
        for x in frames:
            X = np.fft.rfft(x, axis=1) * self.band[None, :]
            cs = X[self._ii] * np.conj(X[self._jj])         # (P, F)
            mag = np.abs(cs) ** self.beta + 1e-10
            # 一次批量 irfft，比 28 次单独调用快 4 倍以上
            R += np.fft.irfft(cs / mag, n=self.npad, axis=1)
        vals = R[np.arange(n_pairs)[:, None], self.lag_idx]
        return vals.sum(axis=0)

    def _cov(self, frames):
        R = np.zeros((self.sub_freqs.size, self.n, self.n), dtype=complex)
        for x in frames:
            X = np.fft.rfft(x, axis=1)
            # 在全带上做一次限带（子空间只用抽稀频点）
            Xb = X * self.band[None, :]
            Xs = Xb[:, np.searchsorted(
                np.fft.rfftfreq(self.nfft, 1.0 / self.rate), self.sub_freqs)]
            Xt = Xs.T                                  # (F, M)
            R += Xt[:, :, None] * np.conj(Xt[:, None, :])
        R /= max(1, len(frames))
        tr = np.trace(R, axis1=1, axis2=2).real
        R += np.eye(self.n)[None] * (1e-3 * tr / self.n)[:, None, None]
        return R

    def _spec_music(self, frames):
        R = self._cov(frames)
        _, V = np.linalg.eigh(R)                       # (F,M), (F,M,M)
        En = V[:, :, :self.n - self.n_sources]         # 噪声子空间
        proj = np.einsum("afm,fmk->afk", np.conj(self.A), En)
        P = 1.0 / (np.sum(np.abs(proj) ** 2, axis=2) + 1e-12)
        return P.sum(axis=1)

    def _spec_mvdr(self, frames):
        R = self._cov(frames)
        Rinv = np.linalg.inv(R)
        # q = a^H R^-1 a
        tmp = np.einsum("fnm,afm->afn", Rinv, self.A)
        q = np.einsum("afn,afn->af", np.conj(self.A), tmp).real
        return (1.0 / np.maximum(q, 1e-12)).sum(axis=1)

    @staticmethod
    def _norm(p):
        p = np.asarray(p, dtype=np.float64)
        lo, hi = np.percentile(p, 5), np.percentile(p, 95)
        if hi - lo < 1e-12:
            return np.zeros_like(p)
        return np.clip((p - lo) / (hi - lo), 0.0, None)

    def spectrum(self, frames):
        """按当前 method 计算空间谱 (A,)"""
        if self.method == "music":
            return self._spec_music(frames)
        if self.method == "mvdr":
            return self._spec_mvdr(frames)
        if self.method == "fusion":
            return self._norm(self._spec_srp(frames)) * self._norm(
                self._spec_music(frames))
        return self._spec_srp(frames)

    # ---------------- 峰值提取 ----------------
    def _peaks_from(self, spec):
        base = float(np.mean(np.abs(spec))) + 1e-9
        work0 = np.asarray(spec, dtype=np.float64)
        work = work0.copy()
        suppress = max(1, int(round(45.0 / self.step)))
        peaks = []
        for _ in range(3):
            k = int(np.argmax(work))
            conf = float(work0[k] / base)
            if conf < 1.15:
                break
            km1, kp1 = (k - 1) % work0.size, (k + 1) % work0.size
            y0, y1, y2 = float(work0[km1]), float(work0[k]), float(work0[kp1])
            denom = (y0 - 2 * y1 + y2)
            delta = 0.0
            if abs(denom) > 1e-12:
                delta = max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom))
            az = (float(np.rad2deg(self.angles[k])) + delta * self.step
                  + self.angle_offset) % 360.0
            peaks.append((az, conf))
            n = work.size
            lo_i, hi_i = (k - suppress) % n, (k + suppress) % n
            if lo_i <= hi_i:
                work[lo_i:hi_i + 1] = -1e9
            else:
                work[lo_i:] = -1e9
                work[:hi_i + 1] = -1e9
        if not peaks:
            peaks = [(self.angle_offset % 360.0, 0.0)]
        return peaks

    # ---------------- 对外接口 ----------------
    def process(self, block):
        """单帧路径（连续模式）：返回 [(az, conf), ...]"""
        if block.shape[1] < self.nfft:
            return None
        x = self._prep(block)
        spec = self.spectrum([x])
        if self.method == "srp":
            self.srp = 0.75 * self.srp + 0.25 * spec
            use = self.srp
        else:
            use = spec
        self.valid = True
        self.peaks = self._peaks_from(use)
        return self.peaks

    def process_window(self, frames):
        """多帧路径（事件模式）：在整段窗口上估计，返回 [(az, conf), ...]"""
        if not frames:
            return None
        prepped = [self._prep(f) for f in frames]
        spec = self.spectrum(prepped)
        self.valid = True
        self.peaks = self._peaks_from(spec)
        return self.peaks

