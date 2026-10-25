"""校准向导 / 设置对话框"""
import os, time
import numpy as np
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, GLib, Pango, GdkPixbuf, Gdk

from .constants import *
from .config import save_settings, CONFIG_PATH
from .dsp import Locator, EventDetector

class CalibDialog(Gtk.Dialog):
    """
    顺序式通道校准：建立「物理位置 -> 软件通道」的映射。

    为什么需要它：阵列的物理 1~8 号位置与 USB/声卡的 8 个通道
    不一定是同一顺序。定位算法按物理位置摆放麦克风坐标，
    所以必须知道每个物理位置对应哪个软件通道。

    流程：按物理顺序 1..8 挨个轻触，程序记录每次是哪个通道响，
    得到一个 8 个元素的置换表 channel_map[物理位置] = 软件通道。
    """

    KNOCK_RATIO = 3.5     # 包络 / 自身基线 倍数门限（调高=更不灵敏）
    KNOCK_FLOOR = 0.002
    DOMINANCE = 1.5       # 最响通道需比第二响高 50%
    DEBOUNCE_S = 0.35

    def __init__(self, app):
        super().__init__(title="通道校准（按物理顺序轻触）", transient_for=app.win,
                         modal=False)
        self.app = app
        self.set_default_size(580, 380)
        self.add_button("取消", Gtk.ResponseType.CANCEL)

        self.step = 0
        self.map = [-1] * AUDIO_CH
        self.peak = np.zeros(AUDIO_CH)
        self._last_hit = 0.0
        self._reject = 0
        self._dup = 0

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_margin_top(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self.lbl = Gtk.Label()
        self.lbl.set_xalign(0)
        self.lbl.set_line_wrap(True)
        box.pack_start(self.lbl, False, False, 0)

        self.bar = Gtk.ProgressBar()
        box.pack_start(self.bar, False, False, 0)

        self.lbl_map = Gtk.Label()
        self.lbl_map.set_xalign(0)
        self.lbl_map.set_use_markup(True)
        box.pack_start(self.lbl_map, False, False, 0)

        self.lbl_lv = Gtk.Label()
        self.lbl_lv.set_xalign(0)
        self.lbl_lv.set_use_markup(True)
        box.pack_start(self.lbl_lv, False, False, 0)

        self.lbl_res = Gtk.Label()
        self.lbl_res.set_xalign(0)
        self.lbl_res.set_line_wrap(True)
        box.pack_start(self.lbl_res, False, False, 0)

        sbox = Gtk.Box(spacing=8)
        sbox.pack_start(Gtk.Label(label="灵敏度门限"), False, False, 0)
        self.scale_sens = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 1.5, 8.0, 0.1)
        self.scale_sens.set_value(self.KNOCK_RATIO)
        self.scale_sens.set_hexpand(True)
        self.scale_sens.connect("value-changed", self._on_sens)
        sbox.pack_start(self.scale_sens, True, True, 0)
        box.pack_start(sbox, False, False, 0)

        hbox = Gtk.Box(spacing=8)
        self.btn_redo = Gtk.Button(label="重做本步")
        self.btn_redo.connect("clicked", self._redo)
        hbox.pack_start(self.btn_redo, False, False, 0)
        self.btn_reset = Gtk.Button(label="全部重来")
        self.btn_reset.connect("clicked", self._reset)
        hbox.pack_start(self.btn_reset, False, False, 0)
        box.pack_start(hbox, False, False, 0)

        self._update_labels()
        self.show_all()
        GLib.timeout_add(50, self._tick)

    def _on_sens(self, sc):
        self.KNOCK_RATIO = float(sc.get_value())

    def _redo(self, *_):
        if self.step > 0:
            self.step -= 1
            ch = self.map[self.step]
            if ch >= 0:
                self.peak[ch] = 0.0
            self.map[self.step] = -1
            self._update_labels()

    def _reset(self, *_):
        self.step = 0
        self.map = [-1] * AUDIO_CH
        self.peak[:] = 0
        self.lbl_res.set_text("")
        self._update_labels()

    def _update_labels(self):
        if self.step < AUDIO_CH:
            self.lbl.set_markup(
                f"<b>第 {self.step + 1}/{AUDIO_CH} 步：轻触物理位置 {self.step + 1} 的麦克风</b>\n"
                f"请按阵列上的实际位置 1 → 8 顺序敲（用于建立位置与声卡通道的对应关系）")
        else:
            self.lbl.set_markup("<b>采集完成</b>")
        self.bar.set_fraction(self.step / AUDIO_CH)

        parts = []
        for i in range(AUDIO_CH):
            if self.map[i] < 0:
                parts.append(f"位置{i+1}: ?")
            else:
                parts.append(f"位置{i+1}:<b>CH{self.map[i]+1}</b>")
        self.lbl_map.set_markup("<small>" + "　".join(parts) + "</small>")

    def _tick(self):
        app = self.app
        env = getattr(app, "_env", None)
        base = getattr(app, "_env_base", None)
        if env is None or base is None:
            return True

        lv = "  ".join(f"{i+1}:{20*np.log10(env[i]+1e-9):4.0f}"
                       for i in range(AUDIO_CH))
        order = np.argsort(env)[::-1]
        top, second = int(order[0]), int(order[1])
        dom = float(env[top] / (env[second] + 1e-9))
        self.lbl_lv.set_markup(
            f"<small><tt>dBFS {lv}</tt></small>\n"
            f"<small>最响 CH{top+1}，领先第二 {dom:.2f}×／门限 {self.DOMINANCE}"
            f"　触发倍数 {float(env[top]/(base[top]+1e-9)):.2f}／门限 "
            f"{self.KNOCK_RATIO:.1f}　忽略 {self._reject}　重复 {self._dup}</small>")

        if self.step >= AUDIO_CH:
            return True

        now = time.time()
        if now - self._last_hit < self.DEBOUNCE_S:
            return True

        ratio = float(env[top] / (base[top] + 1e-9))
        if ratio < self.KNOCK_RATIO or env[top] < self.KNOCK_FLOOR:
            return True
        if dom < self.DOMINANCE:
            self._reject += 1
            return True
        if top in self.map:
            # 这个通道已经对应过某个物理位置了
            self._dup += 1
            return True

        self.map[self.step] = top
        self.peak[top] = float(env[top])
        self._last_hit = now
        self.step += 1
        self._update_labels()
        if self.step >= AUDIO_CH:
            self._finish()
        return True

    def _finish(self):
        app = self.app
        cmap = [int(c) for c in self.map]

        # 增益校正（按通道）
        peaks = np.array([self.peak[c] if self.peak[c] > 0 else np.nan
                          for c in range(AUDIO_CH)])
        ref = float(np.nanmedian(peaks)) if not np.all(np.isnan(peaks)) else 1.0
        gain = np.ones(AUDIO_CH, dtype=np.float32)
        for i in range(AUDIO_CH):
            if not np.isnan(peaks[i]) and peaks[i] > 0 and ref > 0:
                gain[i] = np.clip(ref / peaks[i], 0.5, 2.0)

        app.locator.channel_map = cmap
        app.locator.gain_cal = gain
        app.settings["channel_map"] = cmap
        app.settings["calibrated_at"] = time.strftime("%m-%d %H:%M")
        app.settings["gain_cal"] = [round(float(g), 3) for g in gain]
        save_settings(app.settings)

        mapping = " ".join(f"{i+1}→CH{cmap[i]+1}" for i in range(AUDIO_CH))
        detail = " ".join(f"CH{i+1}:{gain[i]:.2f}" for i in range(AUDIO_CH))
        self.lbl_res.set_text(
            f"✓ 完成\n位置→通道: {mapping}\n增益校正: {detail}\n"
            f"（已保存并立即生效）")
        app._update_cal_label()
        app.set_status("✓ 通道校准成功：" + mapping)
        self.btn_redo.set_sensitive(False)
        self.btn_reset.set_sensitive(True)
        GLib.timeout_add(6000, lambda: (self.destroy(), False)[1])
        return False


class SettingsDialog(Gtk.Dialog):
    """设置：保存路径、截图分辨率、显示尺寸、电平表量程"""

    def __init__(self, parent, settings):
        super().__init__(title="设置", transient_for=parent, modal=True)
        self.set_default_size(520, 300)
        self.add_button("取消", Gtk.ResponseType.CANCEL)
        self.add_button("保存", Gtk.ResponseType.OK)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(16)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        self.get_content_area().add(grid)

        r = 0
        # 保存路径
        grid.attach(Gtk.Label(label="保存路径", xalign=0), 0, r, 1, 1)
        self.ent_path = Gtk.Entry()
        self.ent_path.set_text(settings.get("out_root") or OUT_ROOT)
        self.ent_path.set_hexpand(True)
        grid.attach(self.ent_path, 1, r, 1, 1)
        btn = Gtk.Button(label="浏览…")
        btn.connect("clicked", self._pick_dir)
        grid.attach(btn, 2, r, 1, 1)
        r += 1

        # 截图分辨率
        grid.attach(Gtk.Label(label="截图分辨率", xalign=0), 0, r, 1, 1)
        self.cmb_snap = Gtk.ComboBoxText()
        self.cmb_snap.append("full", "原始 1920×1080（推荐）")
        self.cmb_snap.append("window", "当前窗口画面")
        self.cmb_snap.set_active_id("full" if settings.get("snap_full_res", True) else "window")
        grid.attach(self.cmb_snap, 1, r, 2, 1)
        r += 1

        # 显示尺寸
        grid.attach(Gtk.Label(label="视频显示尺寸", xalign=0), 0, r, 1, 1)
        self.cmb_scale = Gtk.ComboBoxText()
        self.cmb_scale.append("fit", "跟随窗口")
        self.cmb_scale.append("960x540", "960 × 540")
        self.cmb_scale.append("1280x720", "1280 × 720")
        self.cmb_scale.set_active_id(str(settings.get("display_scale", "fit")))
        grid.attach(self.cmb_scale, 1, r, 2, 1)
        r += 1

        # 电平表量程
        grid.attach(Gtk.Label(label="电平表下限", xalign=0), 0, r, 1, 1)
        self.cmb_floor = Gtk.ComboBoxText()
        self.cmb_floor.append("-60", "-60 dBFS")
        self.cmb_floor.append("-80", "-80 dBFS")
        self.cmb_floor.append("-40", "-40 dBFS")
        self.cmb_floor.set_active_id(str(settings.get("meter_floor", -60)))
        grid.attach(self.cmb_floor, 1, r, 2, 1)
        r += 1

        # 录制分段
        grid.attach(Gtk.Label(label="录制分段", xalign=0), 0, r, 1, 1)
        self.spin_seg = Gtk.SpinButton.new_with_range(1, 1440, 1)
        self.spin_seg.set_value(int(settings.get("rec_segment_min", 60)))
        grid.attach(self.spin_seg, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="分钟/段"), 2, r, 1, 1)
        r += 1

        grid.attach(Gtk.Label(label="空间不足即停", xalign=0), 0, r, 1, 1)
        self.spin_free = Gtk.SpinButton.new_with_range(0, 200, 1)
        self.spin_free.set_value(int(settings.get("rec_min_free_gb", 2) or 0))
        grid.attach(self.spin_free, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="GB（低于此值自动停止录制）"), 2, r, 1, 1)
        r += 1

        grid.attach(Gtk.Label(label="录制总时长", xalign=0), 0, r, 1, 1)
        self.spin_total = Gtk.SpinButton.new_with_range(0, 720, 1)
        self.spin_total.set_value(int(settings.get("rec_total_hours", 0) or 0))
        grid.attach(self.spin_total, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="小时（0=不限）"), 2, r, 1, 1)
        r += 1

        # 视频编码
        grid.attach(Gtk.Label(label="录制视频", xalign=0), 0, r, 1, 1)
        self.cmb_vcodec = Gtk.ComboBoxText()
        self.cmb_vcodec.append("copy", "直接复制（画质无损，4.4GB/小时）")
        self.cmb_vcodec.append("4M", "压缩 4Mbps（1.6GB/小时）")
        self.cmb_vcodec.append("2M", "压缩 2Mbps（0.9GB/小时）")
        self.cmb_vcodec.append("1M", "压缩 1Mbps（0.4GB/小时，画质一般）")
        self.cmb_vcodec.set_active_id(str(settings.get("rec_video_codec", "copy")))
        grid.attach(self.cmb_vcodec, 1, r, 2, 1)
        r += 1

        # 音频编码
        grid.attach(Gtk.Label(label="音频编码", xalign=0), 0, r, 1, 1)
        self.cmb_acodec = Gtk.ComboBoxText()
        # 音频不做有损压缩：8 轨 FLAC 无损，保留完整相位/强度信息供定位分析
        self.cmb_acodec.append("flac", "FLAC 无损 8 声道（1.7GB/小时，推荐）")
        self.cmb_acodec.append("wav", "WAV 未压缩（3.5GB/小时）")
        _ac = str(settings.get("rec_audio_codec", "flac"))
        self.cmb_acodec.set_active_id(_ac if _ac in ("flac", "wav") else "flac")
        grid.attach(self.cmb_acodec, 1, r, 2, 1)
        r += 1

        # 磁盘容量估算
        _ac = EST_AUDIO_MBPS * (0.51 if settings.get("rec_audio_codec", "flac") == "flac" else 1.0)
        gb_h = (EST_VIDEO_MBPS + _ac) * 3600 / 8 / 1000
        free_gb = 0.0
        try:
            st = os.statvfs(OUT_ROOT)
            free_gb = st.f_bavail * st.f_frsize / (1024 ** 3)
        except Exception:
            pass
        hours = (free_gb / gb_h) if gb_h > 0 else 0
        lbl_cap = Gtk.Label(xalign=0)
        lbl_cap.set_markup(
            f"<span size='small' color='#8b93a3'>"
            f"容量估算：约 <b>{gb_h:.1f} GB/小时</b>"
            f"（视频 {EST_VIDEO_MBPS:.1f} + 音频 {_ac:.1f} Mbps）\n"
            f"当前可用 <b>{free_gb:.1f} GB</b>"
            f"，可连续录制约 <b>{hours:.1f} 小时</b>"
            f"（24 小时需 {gb_h*24:.0f} GB）</span>")
        grid.attach(lbl_cap, 0, r, 3, 1)
        r += 1

        # 传输协议
        grid.attach(Gtk.Label(label="传输协议", xalign=0), 0, r, 1, 1)
        self.cmb_tp = Gtk.ComboBoxText()
        self.cmb_tp.append("udp", "UDP（低延迟，偶发丢包会花屏）")
        self.cmb_tp.append("tcp", "TCP（不丢包，延迟略高）")
        self.cmb_tp.set_active_id(str(settings.get("transport", "udp")))
        grid.attach(self.cmb_tp, 1, r, 2, 1)
        r += 1

        # 定位算法
        grid.attach(Gtk.Label(label="定位算法", xalign=0), 0, r, 1, 1)
        self.cmb_alg = Gtk.ComboBoxText()
        self.cmb_alg.append("srp", "SRP-PHAT（宽频最佳 · 默认）")
        self.cmb_alg.append("music", "宽带 MUSIC（窄带最佳 · 咳嗽/犬吠）")
        self.cmb_alg.append("mvdr", "MVDR/Capon（窄带最佳 · 最快）")
        self.cmb_alg.append("fusion", "融合 SRP+MUSIC（兼顾）")
        self.cmb_alg.set_active_id(str(settings.get("algorithm", "srp")))
        grid.attach(self.cmb_alg, 1, r, 2, 1)
        r += 1

        # 事件灵敏度
        grid.attach(Gtk.Label(label="事件灵敏度", xalign=0), 0, r, 1, 1)
        self.cmb_esens = Gtk.ComboBoxText()
        self.cmb_esens.append("low", "低（环境吵，少误报）")
        self.cmb_esens.append("medium", "中（推荐）")
        self.cmb_esens.append("high", "高（3~4 米也能触发，误报多）")
        self.cmb_esens.set_active_id(str(settings.get("event_sens", "medium")))
        grid.attach(self.cmb_esens, 1, r, 2, 1)
        r += 1

        # 定位模式
        grid.attach(Gtk.Label(label="定位模式", xalign=0), 0, r, 1, 1)
        self.cmb_mode = Gtk.ComboBoxText()
        self.cmb_mode.append("event", "事件触发（咳嗽/犬吠等短促声音，推荐）")
        self.cmb_mode.append("continuous", "连续跟踪（持续声源）")
        self.cmb_mode.set_active_id(str(settings.get("mode", "event")))
        grid.attach(self.cmb_mode, 1, r, 2, 1)
        r += 1

        # 视频解码
        grid.attach(Gtk.Label(label="视频解码", xalign=0), 0, r, 1, 1)
        self.cmb_hw = Gtk.ComboBoxText()
        self.cmb_hw.append("auto", "自动（支持则用 VAAPI 硬解）")
        self.cmb_hw.append("vaapi", "强制硬件解码（失败会自动回退）")
        self.cmb_hw.append("off", "强制软件解码")
        self.cmb_hw.set_active_id(str(settings.get("hwaccel", "auto")))
        grid.attach(self.cmb_hw, 1, r, 2, 1)
        r += 1

        # 定位灵敏度
        grid.attach(Gtk.Label(label="定位灵敏度", xalign=0), 0, r, 1, 1)
        self.cmb_sens = Gtk.ComboBoxText()
        self.cmb_sens.append("low", "低（抗噪，需较大声音）")
        self.cmb_sens.append("medium", "中（推荐）")
        self.cmb_sens.append("high", "高（远距离也响应，但更易抖）")
        self.cmb_sens.set_active_id(str(settings.get("sensitivity", "medium")))
        grid.attach(self.cmb_sens, 1, r, 2, 1)
        r += 1

        # CSV 记录
        grid.attach(Gtk.Label(label="方位记录", xalign=0), 0, r, 1, 1)
        self.chk_csv = Gtk.CheckButton(label="写入 CSV（logs/ 目录，每 0.5 秒一行）")
        self.chk_csv.set_active(bool(settings.get("log_csv")))
        grid.attach(self.chk_csv, 1, r, 2, 1)
        r += 1

        # 角度偏移
        grid.attach(Gtk.Label(label="角度偏移", xalign=0), 0, r, 1, 1)
        box = Gtk.Box(spacing=6)
        self.spin_off = Gtk.SpinButton.new_with_range(-180, 180, 5)
        self.spin_off.set_value(float(settings.get("angle_offset", 0.0)))
        box.pack_start(self.spin_off, False, False, 0)
        box.pack_start(Gtk.Label(label="度（让 0° 对准设备正前方）"), False, False, 0)
        grid.attach(box, 1, r, 2, 1)
        r += 1

        hint = Gtk.Label()
        hint.set_markup("<small>修改显示尺寸后需要重新点「连接」生效</small>")
        hint.set_xalign(0)
        grid.attach(hint, 0, r, 3, 1)

        self.show_all()

    def _pick_dir(self):
        d = Gtk.FileChooserDialog(title="选择保存目录", parent=self,
                                  action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons("取消", Gtk.ResponseType.CANCEL, "确定", Gtk.ResponseType.OK)
        d.set_current_folder(self.ent_path.get_text() or os.path.expanduser("~"))
        if d.run() == Gtk.ResponseType.OK:
            self.ent_path.set_text(d.get_filename())
        d.destroy()

    def result(self):
        return {
            "out_root": self.ent_path.get_text().strip(),
            "snap_full_res": self.cmb_snap.get_active_id() == "full",
            "display_scale": self.cmb_scale.get_active_id() or "fit",
            "meter_floor": int(self.cmb_floor.get_active_id() or -60),
            "angle_offset": float(self.spin_off.get_value()),
            "log_csv": self.chk_csv.get_active(),
            "sensitivity": self.cmb_sens.get_active_id() or "medium",
            "hwaccel": self.cmb_hw.get_active_id() or "auto",
            "mode": self.cmb_mode.get_active_id() or "event",
            "event_sens": self.cmb_esens.get_active_id() or "medium",
            "algorithm": self.cmb_alg.get_active_id() or "srp",
            "transport": self.cmb_tp.get_active_id() or "udp",
            "rec_segment_min": int(self.spin_seg.get_value()),
            "rec_total_hours": int(self.spin_total.get_value()),
            "rec_min_free_gb": int(self.spin_free.get_value()),
            "rec_video_codec": self.cmb_vcodec.get_active_id() or "copy",
            "rec_audio_bitrate": "512k",
            "rec_audio_codec": self.cmb_acodec.get_active_id() or "flac",
        }

