"""主窗口与业务逻辑"""
import os, sys, json, time, signal, subprocess, threading, collections
import numpy as np
import fcntl
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango

from .constants import *
from .config import *
from .config import _apply_css, _pick_out_root, _config_path
from .readers import (VideoReader, AudioReader, probe_vaapi, probe_video,
                      kill_orphan_streams, kill_all_orphan_streams)
from .dsp import Locator, EventDetector, band_level
from .widgets import Levels, Wave, Compass, Waterfall, np_to_pixbuf, draw_digit
from .dialogs import CalibDialog, SettingsDialog
from . import logging_util as flog

class App:
    def __init__(self, host):
        self.host = host
        # 多设备：host -> {host,connected,rec,rec_dir,rec_active,status}
        self.devices = {}
        self._epoch = 0          # 每次连接/切换自增，用于丢弃旧设备残留数据
        self.vreader = None
        self.areader = None
        self.recs = []
        self._rec_active = False
        self.fps = 0.0
        flog.init()
        self.settings = load_settings()
        flog.log("=" * 60)
        flog.log(f"启动 va-capture {__import__('va_capture').__version__}")
        flog.log(f"日志文件: {flog.log_path()}")
        # 清扫上一次实例遗留的拉流进程（否则会占着设备端客户端槽位）
        n_orphan = kill_all_orphan_streams()
        if n_orphan:
            flog.log(f"清理了 {n_orphan} 个上次遗留的拉流进程", level="WARN")
        flog.log(f"设备: {host}  解码: {self.settings.get('hwaccel')}  "
                 f"传输: {self.settings.get('transport')}  "
                 f"算法: {self.settings.get('algorithm')}  "
                 f"模式: {self.settings.get('mode')}")
        self.locator = Locator(angle_offset=float(self.settings.get("angle_offset", 0.0)))
        self.detector = EventDetector()
        self._ring = collections.deque(maxlen=200)    # 约 2 秒音频（容纳长事件起音）
        self._event_no = 0
        self._last_event = None                        # (t, az, conf, peak_db)
        self._event_t0 = 0.0
        self._apply_sensitivity()
        cm = self.settings.get("channel_map")
        if cm and len(cm) == AUDIO_CH and sorted(cm) == list(range(AUDIO_CH)):
            self.locator.channel_map = [int(c) for c in cm]
        gc = self.settings.get("gain_cal")
        if gc and len(gc) == AUDIO_CH:
            self.locator.gain_cal = np.array(gc, dtype=np.float32)
        self._lbuf = None
        self._last_frame_t = 0.0
        self._stalled = False
        self._video_lock = threading.Lock()
        self._newest_frame = None
        self._last_pb = None
        self._age = 0.0
        self._audio_q = collections.deque(maxlen=60)
        GLib.timeout_add(33, self._display_tick)
        GLib.timeout_add(1000, self._rec_tick)   # 录制时长每秒刷新
        GLib.timeout_add(20, self._audio_tick)
        self.src_size = ""

        self.win = Gtk.Window(title="TinyVision 监看台 — 视频 + 8 轨音频")
        # 固定窗口尺寸：布局不再随内容变化而跳动
        self.win.set_default_size(1300, 930)
        self.win.set_resizable(False)
        _apply_css()
        # 监看窗口置顶，避免被其它窗口遮住
        self.win.set_keep_above(True)
        self.win.connect("destroy", self.quit)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.win.add(vbox)

        # ---- 工具栏 ----
        bar = Gtk.Box(spacing=6)
        bar.set_margin_top(6)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        bar.pack_start(Gtk.Label(label="设备 IP:"), False, False, 0)
        self.ip = Gtk.Entry()
        # 输入框显示"当前设备"。以前这里用 settings 里的 last_host，
        # 而 self.host 来自命令行参数，两者可能不一致 —— 于是点「连接」
        # 会连到输入框里的旧地址而不是当前设备。现在统一以 host 为准。
        self.ip.set_text(host)
        self.ip.set_width_chars(13)
        bar.pack_start(self.ip, False, False, 0)

        # 发现到的设备列表（多设备时从这里挑）
        self.cmb_dev = Gtk.ComboBoxText()
        self.cmb_dev.set_size_request(118, -1)
        self.cmb_dev.connect("changed", self._on_pick_device)
        bar.pack_start(self.cmb_dev, False, False, 0)

        self.btn_scan = Gtk.Button(label="发现")
        self.btn_scan.connect("clicked", lambda *_: self.scan_devices())
        bar.pack_start(self.btn_scan, False, False, 0)

        self.btn_conn = Gtk.Button(label="连接")
        self.btn_conn.connect("clicked", lambda *_: self.connect())
        bar.pack_start(self.btn_conn, False, False, 0)

        self.btn_disc = Gtk.Button(label="断开")
        self.btn_disc.set_sensitive(False)
        self.btn_disc.connect("clicked", lambda *_: self.disconnect())
        bar.pack_start(self.btn_disc, False, False, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 6)

        self.btn_recv = Gtk.ToggleButton(label="录制音视频")
        self.btn_recv.connect("toggled", self.toggle_rec_video)
        bar.pack_start(self.btn_recv, False, False, 0)



        self.btn_alias = Gtk.Button(label="别名")
        self.btn_alias.set_tooltip_text("给当前设备设置一个可读别名（显示与录制目录都用它）")
        self.btn_alias.connect("clicked", self.edit_alias)
        bar.pack_start(self.btn_alias, False, False, 0)

        self.btn_snap = Gtk.Button(label="截帧")
        self.btn_snap.connect("clicked", self.snapshot)
        bar.pack_start(self.btn_snap, False, False, 0)

        self.btn_cal = Gtk.Button(label="校准")
        self.btn_cal.set_tooltip_text("点一下，然后在设备正前方拍手 3 秒，自动把该方向设为 0°")
        self.btn_cal.connect("clicked", self.open_calibration)
        bar.pack_start(self.btn_cal, False, False, 0)

        self.btn_set = Gtk.Button(label="设置")
        self.btn_set.connect("clicked", self.open_settings)
        bar.pack_start(self.btn_set, False, False, 0)

        vbox.pack_start(bar, False, False, 0)

        # ---- 主区 ----
        # 用 Gtk.Grid 分两列：
        #   第 0 列 = 视频 / （下行）设备列表 + 波形
        #   第 1 列 = 右栏 / （下行）方位罗盘
        # 同一列里的两个组件共享列宽（取两者自然宽度的大者），
        # 所以罗盘和上面的面板必然同宽、右边缘对齐 —— 不靠写死像素去凑。
        # 这是 GTK 里做对齐的标准做法（同列 = 同宽）。
        grid = Gtk.Grid()
        grid.set_column_spacing(10)
        grid.set_row_spacing(8)
        grid.set_margin_start(10)
        grid.set_margin_end(8)
        grid.set_margin_top(6)

        self.video = Gtk.Image()
        self.video.set_size_request(960, 540)
        self.video.set_halign(Gtk.Align.CENTER)
        self.video.set_valign(Gtk.Align.CENTER)
        self.video.set_hexpand(True)

        # 连接期间占位提示，让用户立刻看到"正在干活"
        self.video_hint = Gtk.Label()
        self.video_hint.set_markup(
            "<span size='x-large' color='#8b93a3'>正在连接…</span>")
        self.video_hint.set_halign(Gtk.Align.CENTER)
        self.video_hint.set_valign(Gtk.Align.CENTER)
        _ov = Gtk.Overlay()
        _ov.add(self.video)
        _ov.add_overlay(self.video_hint)
        self._video_overlay = _ov
        # 提示标签创建时就是可见的，标志必须先置 True，
        # 否则显示循环里"收到第一帧就隐藏"的逻辑永远不会执行（旧版遗留 bug）
        self._hint_visible = True
        _ov.set_hexpand(True)
        _ov.set_vexpand(True)
        grid.attach(_ov, 0, 0, 1, 1)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_size_request(PANEL_W, -1)       # 只定最小宽度，实际列宽由 Grid 计算
        right.set_valign(Gtk.Align.START)
        self.levels = Levels(self.settings.get("meter_floor", -60))
        right.pack_start(titled("电平表", self.levels), False, False, 0)

        self.waterfall = Waterfall()
        right.pack_start(titled("瀑布图", self.waterfall), False, False, 0)

        self.lbl_az = Gtk.Label(label="方位角  --")
        self.lbl_az.set_markup(
            "<span font_family='Monospace' weight='bold' size='large'>--.-°</span>"
            " 待事件　<span size='small' color='#8b93a3'>等待声音触发</span>")
        # 单行 + 省略号：内容变化时高度不变，避免布局跳动
        self.lbl_az.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_az.set_single_line_mode(True)
        self.lbl_az.set_width_chars(26)
        self.lbl_az.set_max_width_chars(26)
        self.lbl_az.set_size_request(PANEL_W - 12, 22)
        azbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        azbox.pack_start(self.lbl_az, False, False, 0)
        self._azbox = azbox

        # ---- 事件记录（文字输出）----
        frame = Gtk.Frame(label=None)
        _lbl = Gtk.Label()
        _lbl.set_markup("<span size='small' color='#9aa3b2'>事件记录</span>")
        _lbl.set_xalign(0.0)
        _lbl.set_margin_start(4)
        frame.set_label_widget(_lbl)
        frame.set_size_request(PANEL_W, -1)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.log_view.set_left_margin(4)
        self.log_view.set_right_margin(4)
        self.log_buf = self.log_view.get_buffer()
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_size_request(PANEL_W - 8, LOGBOX_H)
        sw.add(self.log_view)
        frame.add(sw)
        right.pack_start(frame, False, False, 0)

        self.lbl_cal = Gtk.Label()
        self.lbl_cal.set_xalign(0.5)
        self.lbl_cal.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_cal.set_single_line_mode(True)
        self.lbl_cal.set_width_chars(30)
        self.lbl_cal.set_size_request(PANEL_W - 12, 18)
        azbox.pack_start(self.lbl_cal, False, False, 0)
        right.pack_start(titled("方位", azbox), False, False, 0)
        right.set_hexpand(False)
        grid.attach(right, 1, 0, 1, 1)

        vbox.pack_start(grid, True, True, 0)

        # ---- 底部一行：设备列表 | 波形（第 0 列）----
        bottom = Gtk.Box(spacing=10)

        # 设备列表：和波形同一行，放在最左边
        devbox = Gtk.Frame()
        _dl = Gtk.Label()
        _dl.set_markup("<span size='small' color='#9aa3b2'>设备列表（点击切换）</span>")
        _dl.set_xalign(0.0)
        _dl.set_margin_start(4)
        devbox.set_label_widget(_dl)
        self.dev_rows = Gtk.ListBox()
        self.dev_rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.dev_rows.connect("row-activated", self._on_row_activated)
        _sw = Gtk.ScrolledWindow()
        _sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        _sw.set_size_request(DEV_W - 8, 190)
        _sw.add(self.dev_rows)
        devbox.add(_sw)
        devbox.set_size_request(DEV_W, -1)
        devbox.set_valign(Gtk.Align.CENTER)
        bottom.pack_start(devbox, False, False, 0)

        self.wave = Wave()
        self.wave.set_valign(Gtk.Align.CENTER)
        wf = titled("8 通道波形", self.wave)
        wf.set_size_request(-1, -1)
        bottom.pack_start(wf, True, True, 0)
        grid.attach(bottom, 0, 1, 1, 1)

        # 罗盘放在第 1 列（右栏正下方）—— 宽度由 Grid 与右栏统一，右边缘自动对齐
        self.compass = Compass()
        self.compass.set_halign(Gtk.Align.CENTER)
        self.compass.set_valign(Gtk.Align.CENTER)
        _cf = titled("方位罗盘", self.compass)
        _cf.set_valign(Gtk.Align.CENTER)
        _cf.set_hexpand(True)
        grid.attach(_cf, 1, 1, 1, 1)

        # GTK 的标准做法：SizeGroup 让两个控件共享同一宽度。
        # 右栏和罗盘框同处第 1 列，再绑一个横向 SizeGroup，
        # 两者的宽度就由布局系统取最大值统一决定 —— 不写死像素，
        # 换字体/换主题/缩放变化时也始终对齐。
        _sg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        _sg.add_widget(right)
        _sg.add_widget(_cf)

        # ---- 状态栏 ----
        # 不用 Gtk.Statusbar：它的最小宽度等于当前消息宽度，
        # 长消息（比如录制路径）会把整个窗口顶宽，导致布局看起来"又不对齐"。
        self.status = Gtk.Label()
        self.status.set_xalign(0.0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_single_line_mode(True)
        self.status.set_size_request(1220, 20)
        self.status.set_margin_start(8)
        self.ctx = None
        vbox.pack_start(self.status, False, False, 0)

        # ---- 底部信息行：左侧视频信息，右侧帧率，水平对齐 ----
        self.lbl_size = Gtk.Label(label="--x--")
        self.lbl_size.set_xalign(0.0)
        self.lbl_size.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_size.set_hexpand(True)
        self.lbl_size.set_size_request(-1, 18)
        self.lbl_fps = Gtk.Label(label="视频 -- fps")
        self.lbl_fps.set_xalign(1.0)
        self.lbl_fps.set_size_request(240, 18)
        sbox = Gtk.Box(spacing=8)
        sbox.set_margin_start(8)
        sbox.set_margin_end(10)
        sbox.pack_start(self.lbl_size, True, True, 0)
        sbox.pack_end(self.lbl_fps, False, False, 0)
        vbox.pack_start(sbox, False, False, 4)

        self._update_cal_label()
        self.win.show_all()
        self.win.present()

    # ---- 状态 ----
    def log_line(self, text):
        """向事件记录框追加一行（自动滚动、限制 200 行），并写入日志文件"""
        flog.log(text.replace("\n", " "))
        try:
            self.log_buf.insert(self.log_buf.get_end_iter(), text + "\n")
            n = self.log_buf.get_line_count()
            if n > 200:
                start = self.log_buf.get_start_iter()
                end = self.log_buf.get_iter_at_line(n - 200)
                self.log_buf.delete(start, end)
            self.log_view.scroll_to_iter(
                self.log_buf.get_end_iter(), 0.0, False, 0.0, 1.0)
        except Exception:
            pass

    def set_status(self, msg):
        # 过长的消息会撑大窗口宽度，截断
        m = msg if len(msg) <= 55 else msg[:52] + "…"
        self.status.set_text(m)
        if not m.startswith("显示"):
            self.log_line(f"{time.strftime('%H:%M:%S')}  -- {m}")
        return False

    def on_video_info(self, sw, sh, w, h, mode):
        self.lbl_size.set_text(f"视频 {sw}×{sh} → {w}×{h}　{mode}")
        return False

    def _hide_video_hint(self):
        try:
            self.video_hint.set_visible(False)
        except Exception:
            pass

    def _show_video_hint(self, text="正在连接…"):
        try:
            self.video_hint.set_markup(
                f"<span size='x-large' color='#8b93a3'>{text}</span>")
            self.video_hint.set_visible(True)
            self._hint_visible = True
        except Exception:
            pass

    def on_fps(self, f):
        self.fps = f
        self.lbl_fps.set_text(f"视频 {f:.1f} fps")
        return False

    def on_frame(self, pb, host=None, epoch=None):
        """
        所有已连接设备的帧都收，按设备存放；界面只显示当前选中那台。

        注意：状态标记必须用"这帧属于哪台设备"(h)，不能用当前显示设备 ——
        否则后台设备的帧会把正在看的设备误标成"已连接"（真踩过的坑）。
        """
        h = host or self.host
        d = self._dev(h)
        # 不再按 epoch 丢弃帧：纪元号一旦对不上就"静默不再更新画面"，
        # 而读取器和录制仍在正常跑 —— 现场表现为"长时间录制后画面冻结，
        # 停止录制才恢复"，极难排查。切设备时已用 host 过滤，足够。
        d["newest_frame"] = (time.perf_counter(), pb)
        self._frame_count = getattr(self, "_frame_count", 0) + 1
        if d.get("status") != "已连接":
            d["status"] = "已连接"
            d["connected"] = True
            GLib.idle_add(self._update_device_list)
        return False

    def _display_tick(self):
        try:
            return self._display_tick_inner()
        except Exception as e:
            flog.log_exc("显示循环异常（定时器继续运行）", e)
            return True

    def _display_tick_inner(self):
        if self.settings.get("mode", "event") == "event":
            le = getattr(self, "_last_event", None)
            if le and time.time() - le[0] > 5.0:
                self._last_event = None
                self.compass.clear_fix()
                self.lbl_az.set_markup(
                    "<span font_family='Monospace' weight='bold' size='large'>--.-°</span> 待事件"
                    "　<span size='small' color='#8b93a3'>等待声音触发</span>")
        dcur = self.devices.get(self.host) or {}
        item = dcur.get("newest_frame")
        if item is not None:
            dcur["newest_frame"] = None
            t_read, pb = item
            dcur["last_pb"] = pb          # 每台设备各自保存最后一帧
            self._age = (time.perf_counter() - t_read) * 1000.0
            self._last_frame_t = time.time()
            if getattr(self, "_stalled", False):
                # 画面恢复：把状态从「信号中断」改回来
                self._stalled = False
                dcur["status"] = "已连接"
                GLib.idle_add(self._update_device_list)
                self.set_status(f"{self.host} 已恢复")
            if getattr(self, "_hint_visible", False):
                self._hint_visible = False
                self._hide_video_hint()
        # 卡死检测：有读取器但 6 秒没新帧 → 标记信号中断（不再显示"已连接"骗人）
        if dcur.get("vreader") and self._last_frame_t:
            if not getattr(self, "_stalled", False) and \
                    time.time() - self._last_frame_t > 6.0:
                self._stalled = True
                dcur["status"] = "信号中断"
                self._update_device_list()
                self.set_status(f"{self.host} 信号中断（{int(time.time()-self._last_frame_t)} 秒无画面）")
        pb_show = dcur.get("last_pb")
        if pb_show is not None:
            self.video.set_from_pixbuf(pb_show)
            self._dn = getattr(self, "_dn", 0) + 1
            now = time.time()
            if not hasattr(self, "_dt0"):
                self._dt0 = now
            elif now - self._dt0 >= 1.0:
                age = getattr(self, "_age", 0.0)
                self._fps_last = self._dn / (now - self._dt0)
                self.lbl_fps.set_text(
                    f"显示 {self._fps_last:.1f} fps"
                    f"   延迟 {age:.0f}ms")
                # 每 60 秒往日志写一行运行状态，便于事后对时间线
                if now - getattr(self, "_stat_t0", 0.0) >= 60.0:
                    self._stat_t0 = now
                    try:
                        devs = " ".join(
                            f"{h}[{d.get('status', '?')}"
                            f"{',REC' if d.get('rec_active') else ''}]"
                            for h, d in sorted(self.devices.items()))
                        db = float(np.max(self.levels.db))
                        # 音频长时间贴地板时留个警告：要么设备没在推流，
                        # 要么处理链出问题了 —— 后期排查时这条很关键
                        if db <= -119.0:
                            self._silent_n = getattr(self, "_silent_n", 0) + 1
                            if self._silent_n == 2:
                                flog.log("音频连续 2 分钟无数据（电平贴地板）",
                                         level="WARN")
                        else:
                            self._silent_n = 0
                        fc = self._frame_count
                        dfr = fc - getattr(self, "_frame_count_last", fc)
                        self._frame_count_last = fc
                        lag = age if age < 5000 else -1   # -1 = 无有效画面
                        if dfr == 0 and dcur.get("vreader"):
                            v = dcur["vreader"]
                            alive = getattr(v, "proc", None) and v.proc.poll() is None
                            flog.log(
                                f"WARN 无帧到达: host={self.host} "
                                f"reader_alive={bool(alive)} "
                                f"stalled={getattr(self,'_stalled',False)} "
                                f"age={age:.0f}ms", level="WARN")
                        mem = self._mem_report()
                        flog.log(
                            f"stats: fps={self._fps_last:.1f} "
                            f"lag={('%.0fms' % lag) if lag >= 0 else 'n/a'} "
                            f"audio={db:.1f}dB {mem} devs={devs or '-'}")
                    except Exception:
                        pass
                self._dn = 0
                self._dt0 = now
        return True

    def on_audio(self, a, host=None, epoch=None):
        """由音频线程调用：只有当前显示设备的数据进入处理队列"""
        h = host or self.host
        d = self._dev(h)
        if h != self.host:
            return False        # 后台设备：不处理音频（省 CPU，录制不受影响）
        # 带上数据所属世代：取用时不匹配的直接丢掉，
        # 否则切换设备那一瞬间旧设备残留的最后一块会把波形"定格"
        self._audio_q.append(
            (epoch if epoch is not None else d.get("epoch", 0), a))
        return False

    def _audio_tick(self):
        # 必须 try/except：这是 GLib 定时器回调，一旦抛异常
        # 定时器会被永久移除，表现为"音频从此再也不动"（很难查）
        try:
            n = 0
            # 用"当前设备"的世代号比对（不是全局 epoch，否则音频全被丢）
            cur_ep = (self.devices.get(self.host) or {}).get("epoch", 0)
            while self._audio_q and n < 8:
                item = self._audio_q.popleft()
                ep, a = item if isinstance(item, tuple) else (cur_ep, item)
                if ep != cur_ep:
                    continue    # 该设备已重连/切换，丢弃旧数据
                self._process_audio(a)
                n += 1
        except Exception as e:
            flog.log_exc("音频处理异常（定时器继续运行）", e)
        return True

    def _process_audio(self, a):
        rms = np.sqrt(np.mean(a * a, axis=1) + 1e-12)
        self._last_rms = rms
        # 按 channel_map 重排成"物理位置"顺序，供罗盘显示
        try:
            phys = rms[np.asarray(self.locator.channel_map)]
            self.compass.set_mic_levels(20.0 * np.log10(phys + 1e-12))
            self.compass.angle_off_disp = float(self.locator.angle_offset)
        except Exception:
            pass

        # 敲击检测用的包络：快攻(立即跟上) + 慢放(0.85 衰减)
        env = getattr(self, "_env", None)
        if env is None:
            self._env = rms.copy()
            self._env_base = rms.copy()
        else:
            self._env = np.maximum(rms, env * 0.82)
            # 基线用很慢的跟踪，避免被敲击拉高
            self._env_base = self._env_base * 0.99 + rms * 0.01
        self.levels.update_db(20.0 * np.log10(rms + 1e-12))
        self.wave.push(a)

        # ---------- 事件检测（咳嗽/犬吠等短促声音）----------
        # 注意：这段必须在"事件模式提前返回"之前，否则事件模式永远不会触发
        self._ring.append(a.copy())
        ev = self.detector.feed(float(np.max(band_level(a))), time.time())
        if ev == "start":
            self._event_t0 = time.time()
            # 记录起音时刻在环形缓冲中的位置（块数），定位时从起音开始取窗口
            self._ev_start_pos = len(self._ring)
        elif ev == "end":
            self._localize_event()

        if self.settings.get("mode", "event") == "event":
            return False          # 事件模式：只在事件结束时定位

        # ---------- 连续模式：每 ~24ms 算一次 ----------
        if self._lbuf is None:
            self._lbuf = a.copy()
        else:
            self._lbuf = np.concatenate((self._lbuf, a), axis=1)

        _now = time.time()
        _due = (_now - getattr(self, "_last_loc_t", 0.0)) >= 0.024
        if self._lbuf.shape[1] >= 1024 and _due:
            self._last_loc_t = _now
            # 取最后 1024 点后必须裁剪，否则缓冲区只增不减：
            # 每秒涨 1.5MB，且 concatenate 是 O(n²)，越跑越慢直到卡死。
            blk = self._lbuf[:, -1024:].copy()
            self._lbuf = blk
            try:
                res = self.locator.process(blk)
            except Exception:
                res = None
            self._last_peak = res[0]
            if res:
                az, conf = self._smooth_azimuth(res[0])
                extra = ""
                if len(res) > 1:
                    extra = "   " + " ".join(
                        f"+{a:.0f}°({dir_name(a)})" for a, c in res[1:])
                stale = getattr(self, "_stale", False)
                self.compass.set_azimuth(az, conf, res, valid=not stale)
                self.waterfall.push(az, conf)
                if getattr(self, "_cal_samples", None) is not None and conf >= 1.5:
                    self._cal_samples.append(az)
                self._maybe_log_csv(az, conf)
                try:
                    p = self._last_rms[np.asarray(self.locator.channel_map)]
                    k = int(np.argmax(p))
                    loud = f"   最强 位置{k+1}"
                except Exception:
                    loud = ""
                # 强瞬态但没有方向：说明是敲击板子（固体传声），明确提示
                impulse_nodir = (self._level_db() > self.IMPULSE_DB
                                 and conf < 1.60)
                if impulse_nodir:
                    self.lbl_az.set_markup(
                        "<span font_family='Monospace' weight='bold' size='large'>--.-°</span> 瞬态"
                        "　<span size='small' color='#8b93a3'>板体传声·无方向</span>")
                elif stale:
                    # 保留旧角度会误导，直接说明当前没有可靠方向
                    self.lbl_az.set_markup(
                        "<span font_family='Monospace' weight='bold' size='large'>--.-°</span> 无源"
                        f"　<span size='small' color='#8b93a3'>置信{conf:.1f}{loud}</span>")
                else:
                    self.lbl_az.set_markup(
                        f"<span font_family='Monospace' weight='bold' size='large'>{az:05.1f}°</span> {dir_name(az)}"
                        f"　<span size='small' color='#8b93a3'>置信{conf:.1f}"
                        f" · {conf_word(conf)}{extra}{loud}</span>")
        return False

    # ---- 连接 ----
    # ---------------- 多设备管理 ----------------
    def _dev(self, host=None):
        """取（或创建）某台设备的状态记录"""
        host = host or self.host
        d = self.devices.get(host)
        if d is None:
            d = {"host": host, "connected": False, "rec": None,
                 "rec_dir": "", "rec_active": False, "status": "未连接",
                 "vreader": None, "areader": None,
                 "newest_frame": None, "epoch": 0, "last_pb": None}
            self.devices[host] = d
        return d

    def _alias(self, host):
        """取设备的可读别名（没设置就返回空）"""
        try:
            return (self.settings.get("aliases") or {}).get(host, "").strip()
        except Exception:
            return ""

    def _disp(self, host):
        """显示用名称：有别名时 "别名 (ip)"，否则就是 ip"""
        a = self._alias(host)
        return f"{a} ({host})" if a else host

    def _rec_dirname(self, host):
        """录制目录名：优先用别名，非法字符替换，冲突时带上 IP"""
        a = self._alias(host)
        if not a:
            return f"dev_{host}"
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in a).strip()
        safe = safe[:32] or f"dev_{host}"
        # 两台同名时用 IP 区分
        same = [h for h in self.devices
                if h != host and self._alias(h) == a]
        return f"{safe}_{host}" if same else safe

    @staticmethod
    def _fmt_dur(sec):
        """秒数 -> HH:MM:SS（录制时长显示用）"""
        sec = max(0, int(sec))
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    def _dev_markup(self, d):
        """单台设备的列表行文本"""
        bits = []
        st = d.get("status", "未连接")
        if st == "已连接":
            bits.append("<span color='#4ade80'>● 已连接</span>")
        elif st == "连接中":
            bits.append("<span color='#facc15'>◌ 连接中</span>")
        elif st == "信号中断":
            bits.append("<span color='#f87171'>● 信号中断</span>")
        else:
            bits.append("<span color='#8b93a3'>○ 未连接</span>")
        if d.get("rec_active"):
            t0 = d.get("rec_start") or time.time()
            bits.append("<span color='#f87171'>● 录制中 "
                        + self._fmt_dur(time.time() - t0) + "</span>")
        mark = "▶ " if d["host"] == self.host else "  "
        a = self._alias(d["host"])
        name = (f"<b>{a}</b> <span size='small' color='#8b93a3'>{d['host']}</span>"
                if a else f"<b>{d['host']}</b>")
        return f"{mark}{name}　" + "　".join(bits)

    def _free_gb(self, path=None):
        """录制目录所在分区剩余空间（GB）"""
        try:
            import shutil
            p = path or self._outdir("recordings")
            return shutil.disk_usage(p).free / 1073741824.0
        except Exception:
            return 999.0

    def _min_free_gb(self):
        try:
            return float(self.settings.get("rec_min_free_gb", 2) or 0)
        except Exception:
            return 2.0

    def _check_recorders_alive(self):
        """后台录制是"视频进程 + 音频进程"，任一意外退出就整体停掉。

        否则会出现"画面没了、声音还在录"的半残状态，而且很难被发现。
        （复用模式的录制跑在读取器里，读取器本来就会自动重连，不在此列。）
        """
        for h, d in list(self.devices.items()):
            procs = d.get("rec")
            if not d.get("rec_active") or not isinstance(procs, (list, tuple)):
                continue
            dead = [p for p in procs if p is not None and p.poll() is not None]
            if not dead:
                continue
            flog.log(f"录制进程意外退出（{h}），停止整台设备的录制: "
                     f"rc={[p.returncode for p in dead]}")
            self.log_line(
                f"{time.strftime('%H:%M:%S')}  -- ⚠ 录制进程退出，"
                f"已停止 {h} 的录制（音视频一起停）")
            try:
                self._stop_dev_rec(d)
            except Exception:
                pass
            self._sync_rec_button()
            self._update_device_list()

    def _check_space_and_stop(self):
        """空间不足时把**所有**录制一起停掉。

        以前只有视频那一路会失败退出，音频还在继续录，结果是
        "只剩声音没有画面"的半残状态，很难发现。这里统一处理：
        空间低于阈值 → 停掉全部设备的录制，并在界面和日志里说清楚。
        """
        recs = [h for h, d in self.devices.items() if d.get("rec_active")]
        if not recs:
            return
        need = self._min_free_gb()
        if need <= 0:
            return
        free = self._free_gb(self.devices[recs[0]].get("rec_dir"))
        if free >= need:
            return
        for h in recs:
            d = self.devices[h]
            flog.log(f"空间不足（剩 {free:.1f} GB < {need:.0f} GB），"
                     f"自动停止录制: {h}")
            self.log_line(
                f"{time.strftime('%H:%M:%S')}  -- ⚠ 空间不足（剩 {free:.1f} GB），"
                f"已停止 {h} 的录制（音视频一起停）")
            try:
                self._stop_dev_rec(d)
            except Exception as e:
                flog.log(f"停止录制失败 {h}: {e}")
        self.set_status(f"⚠ 磁盘剩余 {free:.1f} GB，已自动停止录制")
        self._sync_rec_button()
        self._update_device_list()

    def _stop_dev_rec(self, dev):
        """停掉一台设备的录制（复用模式换回纯显示 / 后台模式杀进程）"""
        if dev.get("rec") is None and dev.get("vreader") and dev.get("rec_active"):
            self._restart_reader_plain(dev)
        else:
            self._kill_dev_rec(dev)
        dev["rec_active"] = False
        dev["rec_start"] = None

    def _mem_report(self):
        """内存诊断串：App 自身 RSS + cgroup 的 anon/file/swap。

        现场出现过"录制一段时间后界面卡死"，但看不出是程序堆还是文件页缓存
        把 cgroup 撑满，所以每分钟把这些数都记下来。
        """
        out = []
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        out.append(f"rss={int(line.split()[1]) // 1024}MB")
                        break
        except Exception:
            pass
        try:
            cg = ""
            with open("/proc/self/cgroup") as f:
                cg = f.readline().split(":", 2)[-1].strip()
            base = "/sys/fs/cgroup" + cg
            st = {}
            with open(base + "/memory.stat") as f:
                for line in f:
                    k, v = line.split()
                    if k in ("anon", "file", "slab", "kernel"):
                        st[k] = int(v) // 1048576
            cur = 0
            with open(base + "/memory.current") as f:
                cur = int(f.read()) // 1048576
            sw = 0
            try:
                with open(base + "/memory.swap.current") as f:
                    sw = int(f.read()) // 1048576
            except Exception:
                pass
            out.append(f"cg={cur}MB(a{st.get('anon',0)}/f{st.get('file',0)})")
            if sw:
                out.append(f"swap={sw}MB")
        except Exception:
            pass
        return " ".join(out)

    def _rec_tick(self):
        """每秒刷新列表行文字；每 10 秒检查一次磁盘空间"""
        try:
            self._tick_n = getattr(self, "_tick_n", 0) + 1
            if self._tick_n % 5 == 0:
                self._check_recorders_alive()
            if self._tick_n % 10 == 0:
                self._check_space_and_stop()
            for row in self.dev_rows.get_children():
                d = self.devices.get(getattr(row, "host", ""))
                lbl = getattr(row, "label", None)
                if d and lbl is not None:
                    lbl.set_markup(self._dev_markup(d))
        except Exception:
            pass
        return True

    def _update_device_list(self):
        """刷新左下角设备列表"""
        try:
            for row in list(self.dev_rows.get_children()):
                self.dev_rows.remove(row)
            for host in sorted(self.devices):
                d = self.devices[host]
                lbl = Gtk.Label()
                lbl.set_xalign(0.0)
                lbl.set_markup(self._dev_markup(d))
                lbl.set_margin_start(4)
                row = Gtk.ListBoxRow()
                row.add(lbl)
                row.host = host
                row.label = lbl
                self.dev_rows.add(row)
            self.dev_rows.show_all()
        except Exception:
            pass

    def switch_to(self, host):
        """切换显示到某台设备（录制不受影响，各设备独立进行）"""
        if not host:
            return
        if host == self.host and (self.vreader or self.areader):
            return
        # 旧设备的读取器要停掉，它的状态随之变为未连接
        # 注意：**不停掉旧设备的读取器** —— 各设备独立保持连接，
        # 否则"切到 2 号，1 号就断线"会让人以为设备掉线了。
        old = self.devices.get(self.host) or {}
        _prev = self.host
        self.host = host
        self.ip.set_text(host)
        self._reset_view_state()
        self._sync_rec_button()
        # 列表只负责"切换显示 + 状态展示"，绝不自动联网：
        # 需要接收时由用户显式点「连接」
        dev = self._dev(host)
        _od = self.devices.get(_prev) or {}
        if (_prev and _prev != host and not _od.get("vreader")
                and not _od.get("areader") and not _od.get("rec_active")):
            self.devices.pop(_prev, None)     # 旧地址已下线，别留在列表里
        self._update_device_list()
        self._sync_device_combo(host)
        self.btn_conn.set_sensitive(not bool(dev.get("vreader")))
        self.btn_disc.set_sensitive(bool(dev.get("vreader")))
        if dev.get("vreader"):
            self._show_video_hint("正在切换…")
            self.set_status(f"已切换到 {host}")
        else:
            self._show_video_hint("未连接 · 点「连接」开始")
            self.set_status(f"已切换到 {host}（未连接，点「连接」开始接收）")

    def _reset_view_state(self):
        """切换设备时彻底清空显示，避免残留上一台设备的画面"""
        self._age = 0.0
        try:
            # 视频：换成纯黑帧，避免旧画面残留造成混淆
            blank = np.zeros((540, 960, 3), dtype=np.uint8)
            self.video.set_from_pixbuf(np_to_pixbuf(blank))
        except Exception:
            pass
        try:
            self.wave.reset()
            self.waterfall.clear()
            self.compass.trail.clear()
            self.compass.clear_fix()
            self._audio_q.clear()
            self._newest_frame = None
            self._lbuf = None
            self._env = None
            self._env_base = None
            self._last_rms = None
            self._last_event = None
            self.locator.srp[:] = 0
            self.compass.clear_fix()
            self.compass.trail.clear()
            self.compass.set_mic_levels(np.full(AUDIO_CH, -120.0))
            self.levels.update_db(np.full(AUDIO_CH, -120.0))
            self.lbl_az.set_markup(
                "<span font_family='Monospace' weight='bold' size='large'>--.-°</span>"
                " 待事件　<span size='small' color='#8b93a3'>等待声音触发</span>")
            self._show_video_hint("正在连接…")
            self._hint_visible = True
        except Exception:
            pass

    # ---------------- 局域网设备发现 ----------------
    @staticmethod
    def _local_subnets():
        """从本机路由表推断要扫描的网段（/24）"""
        nets = set()
        try:
            out = subprocess.run(["ip", "-o", "-4", "addr"], timeout=3,
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                for tok in line.split():
                    if "/" in tok and tok.count(".") == 3:
                        ip, plen = tok.split("/")
                        if ip.startswith("127.") or ip.startswith("169.254."):
                            continue
                        if plen == "24":
                            nets.add(".".join(ip.split(".")[:3]))
        except Exception:
            pass
        if not nets:
            nets.add("192.168.2")
        return sorted(nets)

    def scan_devices(self):
        """并发扫描网段，同时开放 554(video) 和 8554(audio) 的判定为本设备"""
        self.btn_scan.set_sensitive(False)
        self.btn_scan.set_label("扫描中…")
        self.set_status("正在扫描局域网…")

        def worker():
            import socket
            from concurrent.futures import ThreadPoolExecutor
            nets = self._local_subnets()
            found = []

            def probe(ip):
                ok = 0
                for port in (554, 8554):
                    sk = socket.socket()
                    sk.settimeout(0.25)
                    try:
                        if sk.connect_ex((ip, port)) == 0:
                            ok += 1
                    except Exception:
                        pass
                    finally:
                        sk.close()
                return ip if ok == 2 else None

            ips = []
            for n in nets:
                ips += [f"{n}.{i}" for i in range(1, 255)]
            with ThreadPoolExecutor(max_workers=96) as ex:
                for r in ex.map(probe, ips):
                    if r:
                        found.append(r)

            def done():
                self._combo_guard = True
                self.cmb_dev.remove_all()
                for ip in found:
                    a = self._alias(ip)
                    self.cmb_dev.append(ip, f"{a} · {ip}" if a else ip)
                if found:
                    self.cmb_dev.set_active(0)
                    # 只调 set_status：它内部会写事件记录框和日志文件，
                    # 再单独 log_line 会重复两遍
                    self.set_status(f"发现 {len(found)} 台设备：{', '.join(found)}")
                else:
                    self.set_status("未发现设备（检查网线 / IP 网段）")
                self._combo_guard = False

                # 只保留"当前能探测到的"设备：
                # 已经消失、又没有在录制/没有活动读取器的条目直接移除，
                # 免得列表里长期挂着早就下线的地址
                found_set = set(found)
                drop = []
                for h, dd in list(self.devices.items()):
                    if h in found_set:
                        continue
                    if dd.get("rec_active") or dd.get("vreader") or dd.get("areader"):
                        continue
                    drop.append(h)
                for h in drop:
                    self.devices.pop(h, None)
                if drop:
                    flog.log(f"移除已下线设备: {', '.join(drop)}")
                    self._update_device_list()

                # 当前设备不在扫描结果里 → 自动切到一台还在线的，
                # 避免界面和 IP 框停在已经消失的地址上
                _cur = self.devices.get(self.host) or {}
                if (found and self.host not in found_set
                        and not _cur.get("vreader") and not _cur.get("rec_active")):
                    self.switch_to(found[0])
                    self.ip.set_text(found[0])

                # 扫描结束后让下拉框仍指向当前设备
                self._sync_device_combo(self.host)
                self.btn_scan.set_sensitive(True)
                self.btn_scan.set_label("发现")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def edit_alias(self, *_):
        """给当前设备设置别名"""
        host = self.host
        dlg = Gtk.Dialog(title="设备别名", transient_for=self.win, modal=True)
        dlg.add_buttons("取消", Gtk.ResponseType.CANCEL, "保存", Gtk.ResponseType.OK)
        dlg.set_default_size(360, -1)
        box = dlg.get_content_area()
        box.set_spacing(8)
        lab = Gtk.Label()
        lab.set_markup(f"设备 <b>{host}</b> 的别名（用于界面显示和录制目录名）：")
        lab.set_xalign(0.0)
        box.pack_start(lab, False, False, 0)
        ent = Gtk.Entry()
        ent.set_text(self._alias(host))
        ent.set_placeholder_text("例如：前门 / 猪舍-东 / 一号机")
        ent.set_activates_default(True)
        box.pack_start(ent, False, False, 0)
        hint = Gtk.Label()
        hint.set_markup("<span size='small' color='#8b93a3'>留空表示清除别名</span>")
        hint.set_xalign(0.0)
        box.pack_start(hint, False, False, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            name = ent.get_text().strip()
            al = dict(self.settings.get("aliases") or {})
            if name:
                al[host] = name
            else:
                al.pop(host, None)
            self.settings["aliases"] = al
            try:
                save_settings(self.settings)
            except Exception:
                pass
            self._update_device_list()
            self._sync_device_combo(self.host)
            self.log_line(f"{time.strftime('%H:%M:%S')}  -- 设备 {host} 别名："
                          f"{name or '（已清除）'}")
        dlg.destroy()

    def _on_row_activated(self, lb, row):
        host = getattr(row, "host", None)
        if host:
            self.switch_to(host)

    def _on_pick_device(self, cmb):
        """下拉框选中某台设备 = 切换到那台（改 host / 按钮状态 / 清空画面）"""
        if getattr(self, "_combo_guard", False):
            return
        txt = cmb.get_active_id() or cmb.get_active_text()
        if not txt:
            return
        self.ip.set_text(txt)
        if txt != self.host:
            self.switch_to(txt)

    def _sync_device_combo(self, host):
        """让下拉框选中与当前设备一致（切换设备后调用）"""
        if not host:
            return
        try:
            self._combo_guard = True
            model = self.cmb_dev.get_model()
            if model:
                for i, row in enumerate(model):
                    if row[0] == host:
                        self.cmb_dev.set_active(i)
                        break
                else:
                    # 当前设备不在扫描结果里（已经下线）：
                    # 不要再把它塞回下拉框 —— 那会让已消失的地址长期挂在那里
                    self.cmb_dev.set_active(-1)
        except Exception:
            pass
        finally:
            self._combo_guard = False

    def connect(self):
        cur = self._dev()
        if cur.get("vreader") or cur.get("areader"):
            self.set_status(f"{self.host} 已连接")
            return
        # 输入框被用户改过就用输入框，否则用当前选中的设备
        typed = self.ip.get_text().strip()
        host = typed or self.host
        if host != self.host:
            # 用户手填了新地址，视为切换设备
            self.host = host
            self.ip.set_text(host)
        self.settings["last_host"] = host
        try:
            save_settings(self.settings)
        except Exception:
            pass
        n = kill_orphan_streams(host)
        if n:
            self.set_status(f"已清理 {n} 个残留拉流进程")
            time.sleep(0.5)
        scale = self.settings.get("display_scale", "fit")
        if scale and "x" in str(scale):
            tw, th = str(scale).split("x")
            target = (int(tw), int(th))
        else:
            alloc = self.video.get_allocation()
            if alloc.width > 200 and alloc.height > 120:
                target = (alloc.width, alloc.height)
            else:
                target = (960, 540)
        dev0 = self._dev(host)
        dev0["epoch"] = dev0.get("epoch", 0) + 1
        ep = dev0["epoch"]
        self.vreader = VideoReader(f"rtsp://{host}/live",
                                   lambda pb, h=host, e=ep: self.on_frame(pb, h, e),
                                   self.set_status, target=target)

        # 选择解码模式：本地合成码流探测（0.4~1 秒，不占用设备的 RTSP 客户端）
        hmode = self.settings.get("hwaccel", "auto")
        if hmode in ("auto", "vaapi"):
            cached = self.settings.get("hwmode_last", "")
            if cached in ("vaapi", "vaapi_sw"):
                self.vreader.hwmode = cached
                self.log_line(
                    f"{time.strftime('%H:%M:%S')}  -- 解码模式（缓存）："
                    f"{'全GPU' if cached == 'vaapi' else 'GPU解码+软缩放'}")
            else:
                self.set_status("正在检测硬件解码…")
                mode = probe_vaapi()
                self.vreader.hwmode = mode
                if mode != "sw":
                    self.settings["hwmode_last"] = mode
                    try:
                        save_settings(self.settings)
                    except Exception:
                        pass
                self.log_line(
                    f"{time.strftime('%H:%M:%S')}  -- "
                    + {"vaapi": "硬件解码可用：全 GPU 管线",
                       "vaapi_sw": "硬件解码可用：GPU 解码 + CPU 缩放",
                       }.get(mode, "硬件解码不可用，使用软件解码"))
        else:
            self.vreader.hwmode = "sw"
        self.set_status(self.vreader.hwmode)

        self.vreader.on_fps = self.on_fps
        self.vreader.on_video_info = self.on_video_info
        self._apply_transport()
        dev0["vreader"] = self.vreader
        self.vreader.start()
        self.areader = AudioReader(f"rtsp://{host}:8554/live",
                                   lambda a, h=host, e=ep: self.on_audio(a, h, e),
                                   self.set_status, label=host)
        dev0["areader"] = self.areader
        self.areader.start()
        self.btn_conn.set_sensitive(False)
        self.btn_disc.set_sensitive(True)
        dev = self._dev(host)
        dev["connected"] = True
        dev["status"] = "连接中"
        self._update_device_list()
        # 多设备同时解码会叠加 CPU，超过 2 台给个提示
        n_live = sum(1 for x in self.devices.values() if x.get("vreader"))
        if n_live > 2:
            self.log_line(
                f"{time.strftime('%H:%M:%S')}  -- 注意：已有 {n_live} 台设备在"
                f"同时解码，CPU 会随数量线性上升")
        self._sync_rec_button()

    def disconnect(self):
        d = self._dev()
        d["epoch"] = d.get("epoch", 0) + 1     # 作废该设备的回调
        self._show_video_hint("已断开")
        if d.get("vreader"):
            d["vreader"].stop(); d["vreader"] = None
        if d.get("areader"):
            d["areader"].stop(); d["areader"] = None
        self.vreader = None
        self.areader = None
        self.btn_conn.set_sensitive(True)
        self.btn_disc.set_sensitive(False)
        dev = self._dev()
        dev["connected"] = False
        dev["status"] = "已断开"
        self._update_device_list()
        self.set_status("已断开")

    # ---- 录制 ----
    def _outdir(self, sub):
        root = self.settings.get("out_root") or OUT_ROOT
        d = os.path.join(root, sub)
        os.makedirs(d, exist_ok=True)
        return d

    def _stamp(self):
        return time.strftime("%Y%m%d_%H%M%S")

    def record_gb_per_hour(self):
        """按当前音频编码估算每小时占用（FLAC 约为 PCM 的 51%）"""
        a = EST_AUDIO_MBPS
        if self.settings.get("rec_audio_codec", "flac") == "flac":
            a *= 0.51
        return (EST_VIDEO_MBPS + a) * 3600 / 8 / 1000

    def record_free_gb(self, path=None):
        try:
            st = os.statvfs(path or self.out_root)
            return st.f_bavail * st.f_frsize / (1024 ** 3)
        except Exception:
            return 0.0

    # ---------------- 录制：音频后台无损压缩 ----------------
    # WAV 直接录，时长元数据正确；每段录完后在后台转成 FLAC（无损，省 ~49%）。
    def _compress_closed_segments(self, d, final=False):
        """把已写完的 seg_NNN.wav 转成 seg_NNN.flac 并删除 wav，返回剩余数量"""
        import glob
        files = sorted(glob.glob(os.path.join(d, "*.wav")))
        if not files:
            return 0
        idxs = []
        for f in files:
            b = os.path.basename(f)
            try:
                idxs.append(int(b[4:7]))
            except Exception:
                idxs.append(0)
        last = max(idxs) if idxs else 0
        for f, i in zip(files, idxs):
            if not final and i >= last:      # 最后一段还在写
                continue
            flac = f[:-4] + ".flac"
            if os.path.exists(flac):
                try:
                    os.remove(f)
                except Exception:
                    pass
                continue
            try:
                r = subprocess.run(
                    ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                     "-i", f, "-c:a", "flac", "-compression_level", "5", flac],
                    timeout=1800)
                if r.returncode == 0 and os.path.exists(flac):
                    os.remove(f)
            except Exception:
                pass
        return len(glob.glob(os.path.join(d, "*.wav")))

    def _flac_loop(self, d):
        while True:
            time.sleep(4)
            left = self._compress_closed_segments(d, final=not self._rec_active)
            if not self._rec_active and left == 0:
                self.log_line(
                    f"{time.strftime('%H:%M:%S')}  -- 音频已全部转为 FLAC（无损）")
                break

    def _sync_rec_button(self):
        """让录制按钮（状态+文字）反映"当前设备"是否正在录制"""
        try:
            active = bool(self._dev().get("rec_active"))
            if self.btn_recv.get_active() != active:
                self._rec_guard = True
                self.btn_recv.set_active(active)
                self._rec_guard = False
            # 标签必须显式更新：set_active 触发的 toggled 被 _rec_guard 挡掉了，
            # 不会走到 toggle_rec_video 里改文字
            self.btn_recv.set_label("停止录像" if active else "录制音视频")
        except Exception:
            pass

    def toggle_rec_video(self, btn):
        """
        音视频统一录制：同一个 ffmpeg 进程同时拉视频(554)和 8 轨音频(8554)，
        保证起止时间对齐；按设定时长分段，每段生成同名的 .mp4 + .wav。

        每台设备独立录制：切到别的设备录制不会影响这台，后台继续跑。
        """
        if getattr(self, "_rec_guard", False):
            return
        dev = self._dev()

        # 开始录制前先看空间够不够（放不住就不开始，免得录出半截文件）
        if btn.get_active():
            need = self._min_free_gb()
            free = self._free_gb(self._outdir("recordings"))
            if need > 0 and free < need:
                self._rec_guard = True
                btn.set_active(False)
                self._rec_guard = False
                self.set_status(
                    f"⚠ 磁盘剩余 {free:.1f} GB，不足 {need:.0f} GB，无法开始录制")
                flog.log(f"拒绝开始录制: 剩余 {free:.1f} GB < {need:.0f} GB")
                return

        # ── 复用模式 ──────────────────────────────────────────────
        # 如果录的就是"当前显示"的设备，它已经有一路视频会话了。
        # 再起一个 ffmpeg 就是第二路客户端 —— 而设备端视频服务是单客户端
        # 的预编译程序，多一路会话会显著增加它异常退出的概率，
        # 退出后还会留下约 25MB 的孤儿 ION 缓冲（整机只有 51MB），
        # 最终把设备拖死。现场两台设备同固件，只有"显示+录制"的那台挂掉。
        #
        # 所以这里把录像任务并进显示读取器：一个 ffmpeg 同时
        #   ① -c copy 写 mp4 分段  ② 输出原始帧给界面  ③ 写 FLAC 音频
        # 全程只占 1 路视频客户端。
        if btn.get_active() and dev.get("vreader") and dev["host"] == self.host:
            seg_min = max(1, int(self.settings.get("rec_segment_min", 60)))
            d = os.path.join(self._outdir("recordings"),
                             self._rec_dirname(dev["host"]), self._stamp())
            os.makedirs(d, exist_ok=True)
            old = dev["vreader"]
            old.stop()
            time.sleep(0.3)
            # 注意：这里**不能**递增 dev["epoch"]。
            # 世代号是"整台设备"的，音频读取器还活着并带着旧纪元号，
            # 一旦递增，on_audio 会把它的数据全部当作旧数据丢弃 ——
            # 表现就是「一点录制，音频实时显示就没了」。
            # 旧视频读取器已经 stop()，不会有残留帧污染。
            ep = dev.get("epoch", 0)
            stamp = os.path.basename(d)        # 目录名就是开始时间 20261025_102401
            v = VideoReader(f"rtsp://{dev['host']}/live",
                            lambda pb, h=dev["host"], e=ep: self.on_frame(pb, h, e),
                            self.set_status, target=old.target,
                            rec={"dir": d, "seg": seg_min * 60, "prefix": stamp})
            v.hwmode = old.hwmode
            v.transport = old.transport
            v.on_video_info = self.on_video_info
            dev["vreader"] = v
            v.start()

            # 音频录制放到音频读取器里做（纯音频进程，内存零增长）。
            # 以前把 8 路音频和视频塞进同一个 ffmpeg，实测 20MB/分钟线性上涨，
            # 一两个小时就把 cgroup 撑爆被 SIGKILL —— 这就是"录制后卡死"的根因。
            aold = dev.get("areader")
            if aold is not None:
                aold.stop()
                time.sleep(0.2)
            aep = dev.get("epoch", 0)
            ar = AudioReader(f"rtsp://{dev['host']}:8554/live",
                             lambda a, h=dev["host"], e=aep: self.on_audio(a, h, e),
                             self.set_status, label=dev["host"],
                             rec={"dir": d, "seg": seg_min * 60, "prefix": stamp})
            dev["areader"] = ar
            ar.start()

            dev["rec_dir"] = d
            dev["rec_active"] = True
            dev["rec_start"] = time.time()
            dev["rec"] = None
            self._rec_path = d
            self._rec_active = True
            self._update_device_list()
            self._sync_rec_button()
            btn.set_label("停止录像")
            self.log_line(f"{time.strftime('%H:%M:%S')}  -- ● 录制中（复用显示流，"
                          f"{seg_min}分钟分段）→ {d}")
            flog.log(f"开始录制（复用模式）: {dev['host']} → {d}")
            return

        if btn.get_active():
            seg_min = max(1, int(self.settings.get("rec_segment_min", 60)))
            total_h = float(self.settings.get("rec_total_hours", 0) or 0)
            d = os.path.join(self._outdir("recordings"),
                             self._rec_dirname(dev["host"]), self._stamp())
            os.makedirs(d, exist_ok=True)
            seg = seg_min * 60

            # 注意两个坑（实测踩过）：
            # 1) 音频服务器不支持 RTSP over TCP，音频输入必须用 UDP；
            #    视频用 TCP，录制容不得丢包。
            # 2) 分段 muxer 对"先视频后音频"的组合会失效（视频不分段），
            #    必须把音频输出放前面、视频输出放后面，两者才能都正常分段。
            # 两个独立进程：视频一个、音频一个。
            #
            # 千万别合到一个进程里 —— 实测"视频解码 + 8 路 RTSP 音频"
            # 同进程会让 ffmpeg 以 ~20MB/分钟线性吃内存，一两个小时
            # 就把 cgroup 撑爆、进程被 SIGKILL（现场"录制后卡死"的根因）。
            # 拆开后两边实测都是 0 增长。
            audio_map = "".join(f"[0:a:{i}]" for i in range(AUDIO_CH))
            lim = ["-t", str(int(total_h * 3600))] if total_h > 0 else []
            stamp = os.path.basename(d)
            vcmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", f"rtsp://{self.host}/live",
                    "-map", "0:v:0", "-c:v", "copy",
                    "-f", "segment", "-segment_time", str(seg),
                    "-reset_timestamps", "1"] + lim + [
                    os.path.join(d, f"{stamp}_%03d.mp4")]
            acmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-rtsp_transport", "udp", "-buffer_size", "1048576",
                    "-i", f"rtsp://{self.host}:8554/live",
                    "-filter_complex",
                    f"{audio_map}amerge=inputs={AUDIO_CH}[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le",
                    "-f", "segment", "-segment_time", str(seg),
                    "-reset_timestamps", "1"] + lim + [
                    os.path.join(d, f"{stamp}_%03d.wav")]

            dev["rec"] = [subprocess.Popen(vcmd), subprocess.Popen(acmd)]
            dev["rec_dir"] = d
            dev["rec_active"] = True
            dev["rec_start"] = time.time()
            self.recs.append(dev["rec"])
            self._rec_path = d
            self._rec_active = True
            self._update_device_list()
            if self.settings.get("rec_audio_codec", "flac") == "flac":
                threading.Thread(target=self._flac_loop, args=(d,),
                                 daemon=True).start()
            btn.set_label("停止录像")
            est = self.record_gb_per_hour()
            self.set_status(
                f"● 录制中（{seg_min}分钟分段，约 {est:.1f} GB/小时）→ {d}")
            self.log_line(
                f"{time.strftime('%H:%M:%S')}  -- 开始录制：{seg_min}分钟/段，"
                f"{'不限时长' if total_h <= 0 else f'{total_h:g} 小时'}，"
                f"约 {est:.1f} GB/小时")
        else:
            self._stop_dev_rec(dev)
            self._update_device_list()
            self._sync_rec_button()
            btn.set_label("录制音视频")
            self.set_status(f"✓ 已保存 {getattr(self, '_rec_path', '')}")
            self.log_line(
                f"{time.strftime('%H:%M:%S')}  -- 停止录制：{getattr(self, '_rec_path', '')}")

    def toggle_rec_audio(self, btn):
        if btn.get_active():
            d = self._outdir(os.path.join("audio", f"audio_{self._stamp()}"))
            outs = []
            for i in range(AUDIO_CH):
                outs += ["-map", f"0:a:{i}", os.path.join(d, f"ch{i}.wav")]
            self.recs.append(subprocess.Popen(
                ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                 "-rtsp_transport", "udp",
                 "-i", f"rtsp://{self.host}:8554/live"] + outs))
            self._rec_path = d
            btn.set_label("停止录音")
            self.set_status(f"● 录音中 → {d}")
        else:
            self._kill_recs()
            btn.set_label("录 8 轨音频")
            self.set_status(f"✓ 已保存 {getattr(self, '_rec_path', '')}")

    def _restart_reader_plain(self, dev):
        """把复用录制中的读取器换回纯显示模式"""
        host = dev["host"]
        old = dev.get("vreader")
        if old:
            old.stop()
            time.sleep(0.3)
        ep = dev.get("epoch", 0)     # 同样不能递增，见上面的说明
        v = VideoReader(f"rtsp://{host}/live",
                        lambda pb, h=host, e=ep: self.on_frame(pb, h, e),
                        self.set_status, target=getattr(old, "target", None))
        if old:
            v.hwmode = old.hwmode
            v.transport = old.transport
        v.on_video_info = self.on_video_info
        dev["vreader"] = v
        v.start()

        # 音频读取器同样换回"不录像"的版本
        aold = dev.get("areader")
        if aold is not None:
            aold.stop()
            time.sleep(0.2)
        dev["areader"] = AudioReader(
            f"rtsp://{host}:8554/live",
            lambda a, h=host, e=dev.get("epoch", 0): self.on_audio(a, h, e),
            self.set_status, label=host)
        dev["areader"].start()

        if dev.get("rec_dir"):
            self.log_line(f"{time.strftime('%H:%M:%S')}  -- ✓ 已保存 {dev['rec_dir']}")
            flog.log(f"停止录制（复用模式）: {dev['rec_dir']}")
        dev["rec_dir"] = ""

    def _kill_dev_rec(self, dev):
        """只停止某台设备的录制（视频、音频各是一个进程）"""
        procs = dev.get("rec")
        if not procs:
            return
        if not isinstance(procs, (list, tuple)):
            procs = [procs]
        for p in procs:
            if p is None:
                continue
            try:
                p.send_signal(signal.SIGINT)
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                self.recs.remove(p)
            except Exception:
                pass
        dev["rec"] = None

    def _kill_recs(self):
        # SIGINT 让 ffmpeg 正常收尾（写完 mp4 的 moov / wav 的头部）
        flat = []
        for x in self.recs:
            flat.extend(x if isinstance(x, (list, tuple)) else [x])
        for p in flat:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
        for p in self.recs:
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.recs.clear()

    AZ_WIN = 20          # 平滑窗口（只保留通过置信度门限的样本）
    AZ_MIN_CONF = 1.8    # 低于此置信度：不更新方位，保持上一次稳定值
    AZ_HYST = 8.0        # 迟滞：变化小于该角度不改显示
    AZ_HOLD_SEC = 1.5    # 超过这么久没有可靠样本 -> 判定"无明确声源"，隐藏指针

    LOUD_DB = -45.0      # 超过这个电平认为"确实有人在发声"（正常说话即可触发）
    IMPULSE_DB = -30.0   # 超过这个电平视为瞬态事件（敲击/拍手），直接跳到该方向

    def _level_db(self):
        r = getattr(self, "_last_rms", None)
        if r is None:
            return -120.0
        try:
            return float(20.0 * np.log10(float(np.max(r)) + 1e-12))
        except Exception:
            return -120.0

    def _smooth_azimuth(self, peak):
        """
        低信噪比下的稳定策略：

        1. 只把 conf >= AZ_MIN_CONF 的样本放进平滑窗口；
        2. 对窗口做置信度加权圆周平均（避免 0/360 跳变）；
        3. 迟滞，小于 AZ_HYST 的变化不改显示；
        4. 长时间没有可靠样本就保持旧值，并标记 stale，
           而不是让指针乱转 —— 安静房间里本来就没有声源。
        """
        az, conf = peak
        now = time.time()
        hist = getattr(self, "_az_hist", None)
        if hist is None:
            hist = self._az_hist = collections.deque(maxlen=self.AZ_WIN)

        # 有明确声音时放宽门限：麦克风电平已经很高，说明确实有源，
        # 此时即使 SRP 置信度一般也应该跟随，否则会出现"点亮了指针不动"
        level_db = self._level_db()
        loud = level_db > self.LOUD_DB
        # 强瞬态（拍手/敲击）：直接跳到该方向，不做平滑也不看置信度。
        # 敲击只有 20ms，SRP 置信度天然偏低，但它确实是一次明确的声音事件。
        # 注意：敲板子时声音经 PCB 固体传导，8 个麦克风几乎同时收到，
        # TDOA≈0，方向不可解。这种瞬态必须要求足够置信度，
        # 否则会让指针跳到随机方向，比不动更误导。
        impulse = level_db > self.IMPULSE_DB and conf >= 1.60

        gate = 1.10 if loud else self.AZ_MIN_CONF
        if impulse:
            gate = 0.0

        if conf >= gate:
            hist.append((az, conf))
            self._last_good_t = now
            if impulse:
                hist.clear()
                hist.append((az, conf))
                self._disp_az_target = az
                self._stale = False
                return (az, float(conf))
            if loud:
                # 大声时窗口缩短，响应更快
                while len(hist) > 8:
                    hist.popleft()

        if not hist:
            self._stale = True
            return peak

        w = np.array([max(0.0, c - 1.0) for _, c in hist], dtype=np.float64)
        if w.sum() <= 0:
            w = np.ones(len(hist))
        ang = np.deg2rad([a for a, _ in hist])
        mean = np.rad2deg(np.arctan2((w * np.sin(ang)).sum(),
                                     (w * np.cos(ang)).sum())) % 360.0
        smooth_conf = float(np.average([c for _, c in hist], weights=w))

        disp = getattr(self, "_disp_az_target", None)
        if disp is not None:
            d = abs((mean - disp + 180.0) % 360.0 - 180.0)
            if d < self.AZ_HYST:
                mean = disp
            elif loud and d > 25.0:
                # 明显换方向了，直接跟过去，不被平滑拖住
                mean = 0.5 * mean + 0.5 * az
        self._disp_az_target = mean

        self._stale = (now - getattr(self, "_last_good_t", 0.0)) > self.AZ_HOLD_SEC
        return (mean, smooth_conf)

    def _update_cal_label(self):
        """把校准状态显示在界面上，让"是否已校准"一目了然"""
        cm = self.settings.get("channel_map")
        ts = self.settings.get("calibrated_at", "")
        if not cm or sorted(cm) != list(range(AUDIO_CH)):
            self.lbl_cal.set_markup(
                "<span size='small' color='#e0803a'>未校准 · 请点「校准」</span>")
            return
        identity = cm == list(range(AUDIO_CH))
        mapping = " ".join(f"{i+1}→{cm[i]+1}" for i in range(AUDIO_CH))
        tag = "默认顺序" if identity else mapping
        when = f"　{ts}" if ts else ""
        self.lbl_cal.set_markup(
            f"<span size='small' color='#4ade80'>已校准</span>"
            f"　<span size='small' color='#8b93a3'>{tag}{when}</span>")

    def _reprobe_bg(self, host):
        """后台探测硬解能力；发现更优模式就热切换到正在运行的拉流"""
        try:
            mode = probe_vaapi(f"rtsp://{host}/live")
        except Exception:
            return
        if mode != "sw" and mode != self.settings.get("hwmode_last"):
            self.settings["hwmode_last"] = mode
            try:
                save_settings(self.settings)
            except Exception:
                pass
        v = getattr(self, "vreader", None)
        if v is not None and mode != v.hwmode and mode != "sw":
            v.hwmode = mode
            v._switching = True
            GLib.idle_add(self._restart_video)
            self.log_line(
                f"{time.strftime('%H:%M:%S')}  -- 切换到"
                f"{'全GPU' if mode == 'vaapi' else 'GPU解码+软缩放'}解码")

    def _restart_video(self):
        """让重连循环用新的解码模式重新起 ffmpeg（热切换，不掉界面）"""
        try:
            if self.vreader and self.vreader.proc:
                self.vreader.proc.kill()
        except Exception:
            pass
        return False

    def _apply_transport(self):
        tp = self.settings.get("transport", "udp")
        if tp not in ("udp", "tcp"):
            tp = "udp"
        for r in (getattr(self, "vreader", None), getattr(self, "areader", None)):
            if r is not None:
                r.transport = tp

    def _apply_algorithm(self):
        """切换定位算法（热切换，无需重启）"""
        alg = self.settings.get("algorithm", "srp")
        if alg not in ("srp", "music", "mvdr", "fusion"):
            alg = "srp"
        self.locator.method = alg
        self.locator.srp[:] = 0

    def _apply_event_sens(self):
        """事件检测灵敏度：越灵敏越容易触发远距离声音，但误报也更多"""
        table = {
            "low":    (3.0, 0.0015, 1.2),
            "medium": (2.2, 0.0008, 2.2),
            "high":   (1.7, 0.0004, 1.8),
        }
        r, lvl, rise = table.get(self.settings.get("event_sens", "medium"),
                                 table["medium"])
        self.detector.START_RATIO = r
        self.detector.MIN_LEVEL = lvl
        self.detector.RISE_RATIO = rise

    def _apply_sensitivity(self):
        table = {"low": 2.2, "medium": 1.8, "high": 1.5}
        self.AZ_MIN_CONF = table.get(self.settings.get("sensitivity", "medium"), 1.8)

    def _localize_event(self):
        """
        对刚结束的事件做定位。

        只取事件的起音段（前 ~200ms）—— 起音是宽带的，
        而咳嗽/犬吠的持续段能量集中在 2kHz 以下，
        带宽不足会让 9cm 孔径分辨不出方向。
        """
        if len(self._ring) < 8:
            return
        buf = np.concatenate(list(self._ring), axis=1)   # (8, N)
        # 关键：从**起音**开始取窗口，而不是从末尾。
        # 咳嗽/犬吠的起音是宽带的（含高频气流成分），
        # 持续段能量集中在 2kHz 以下，带宽不足会让 9cm 孔径分辨不出方向。
        win = min(buf.shape[1], int(0.25 * AUDIO_RATE))
        end_pos = getattr(self, "_ev_start_pos", len(self._ring))
        end_samples = min(buf.shape[1], end_pos * 480)
        start_samples = max(0, end_samples - win)
        seg = buf[:, start_samples:min(buf.shape[1], start_samples + win + 480)]
        if seg.shape[1] < win:
            seg = buf[:, -win:]

        nfft = self.locator.nfft
        if seg.shape[1] < nfft:
            return

        # 把整段起音窗口切成多个 1024 点帧，一次性交给定位器：
        # - SRP：逐帧相关后相加（非相干累积）
        # - MUSIC/MVDR：这些帧用于估计 8×8 协方差矩阵（帧越多越稳）
        step = max(1, (seg.shape[1] - nfft) // 8)
        chunks = []
        for off in range(0, max(1, seg.shape[1] - nfft + 1), step):
            c = seg[:, off:off + nfft]
            if c.shape[1] < nfft:
                break
            chunks.append(c)
        if not chunks:
            return
        res = self.locator.process_window(chunks)
        ests = [res[0]] if res and res[0][1] >= 1.15 else []
        if not ests:
            print(f"[event] 检测到事件但定位失败（起始段带宽不足？）",
                  flush=True)
            self.lbl_az.set_markup(
                "<span font_family='Monospace' weight='bold' size='large'>--.-°</span> 事件"
                "　<span size='small' color='#8b93a3'>窄带声音·无法定向</span>")
            self._last_event = None
            return

        az, conf = ests[0]
        peak_db = 20.0 * np.log10(self.detector.peak + 1e-12)

        self._event_no += 1
        self._last_event = (time.time(), az, conf, peak_db)
        print(f"[event] #{self._event_no} az={az:.1f} conf={conf:.2f} "
              f"peak={peak_db:.0f}dBFS", flush=True)
        self.log_line(f"{time.strftime('%H:%M:%S')}  "
                      f"{az:5.1f}° {dir_name(az)}  "
                      f"c{conf:.1f}  {peak_db:4.0f}dB")

        # 事件定位只更新一次指针，之后保持 4 秒
        self.compass.set_azimuth(az, conf, [(az, conf)], valid=True)
        self.waterfall.push(az, conf)
        self._maybe_log_csv(az, conf)

        self.lbl_az.set_markup(
            f"<span font_family='Monospace' weight='bold' size='large'>{az:05.1f}°</span> {dir_name(az)}"
            f"　<span size='small' color='#8b93a3'>事件#{self._event_no}"
            f"  置信{conf:.1f}  {peak_db:.0f}dB</span>")

    def _maybe_log_csv(self, az, conf):
        if not self.settings.get("log_csv"):
            return
        now = time.time()
        if now - getattr(self, "_csv_t", 0.0) < 0.5:
            return
        self._csv_t = now
        if getattr(self, "_csv_fp", None) is None:
            d = self._outdir("logs")
            path = os.path.join(d, f"azimuth_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            try:
                self._csv_fp = open(path, "w", encoding="utf-8")
                self._csv_fp.write("time,azimuth_deg,direction,confidence\n")
                self._csv_path = path
            except Exception:
                self.settings["log_csv"] = False
                return
        try:
            self._csv_fp.write(
                f"{time.strftime('%H:%M:%S')},{az:.1f},{dir_name(az)},{conf:.2f}\n")
            self._csv_fp.flush()
        except Exception:
            pass

    def open_calibration(self, *_):
        # 对话框可能被 keep_above 主窗口盖住
        self.win.set_keep_above(False)
        dlg = CalibDialog(self)
        dlg.connect("response", lambda d, r: (d.destroy(),
                                              self.win.set_keep_above(True)))
        dlg.show()

    def open_settings(self, *_):
        # 对话框可能被 keep_above 的主窗口盖住，先临时取消置顶
        self.win.set_keep_above(False)
        try:
            dlg = SettingsDialog(self.win, self.settings)
            resp = dlg.run()
            if resp == Gtk.ResponseType.OK:
                self.settings.update(dlg.result())
                if save_settings(self.settings):
                    self.set_status(f"✓ 设置已保存到 {CONFIG_PATH}")
                else:
                    self.set_status("设置保存失败（仅本次生效）")
                self.levels.floor = self.settings.get("meter_floor", -60)
                self.locator.angle_offset = float(self.settings.get("angle_offset", 0.0))
                self._apply_sensitivity()
                self._apply_event_sens()
                self._apply_algorithm()
                self._apply_transport()
            dlg.destroy()
        finally:
            self.win.set_keep_above(True)
            self.win.present()

    def snapshot(self, *_):
        d = self._outdir("snapshots")
        path = os.path.join(d, f"snap_{self._stamp()}.png")

        if not self.settings.get("snap_full_res", True):
            pb = getattr(self, "_last_pb", None)
            if pb is None:
                self.set_status("还没有画面")
                return
            try:
                pb.savev(path, "png", [], [])
                self.set_status(f"✓ 截图已保存（当前画面） {path}")
            except Exception as e:
                self.set_status(f"截图失败: {e}")
            return

        # 原始分辨率：另开一个短连接抓一帧（设备端支持多客户端，实测不影响主画面）
        self.set_status("正在抓取原始分辨率帧…")

        def worker():
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                   "-rtsp_transport", "tcp", "-i", f"rtsp://{self.host}/live",
                   "-frames:v", "1", path]
            try:
                subprocess.run(cmd, timeout=25, check=False)
                if os.path.exists(path) and os.path.getsize(path) > 1000:
                    GLib.idle_add(self.set_status, f"✓ 截图已保存 {path}")
                else:
                    GLib.idle_add(self.set_status, "截图失败（未取到帧）")
            except Exception as e:
                GLib.idle_add(self.set_status, f"截图失败: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def quit(self, *_):
        self._kill_recs()
        self.disconnect()
        Gtk.main_quit()



def _acquire_single_instance():
    """单实例锁：已经有一个在跑就直接退出。

    场景：装成 systemd 服务后它随登录自启，用户再从菜单点一次图标，
    就会出现两个实例抢同一个设备的 RTSP 客户端（设备端是单客户端设计）。
    """
    import tempfile
    # 允许测试时用独立锁文件，避免和生产实例互斥
    path = os.environ.get("VA_CAPTURE_LOCK") or os.path.join(
        tempfile.gettempdir(), "va-capture.lock")
    try:
        f = open(path, "w")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f            # 保持引用，进程存活期间一直持有
    except Exception:
        return None


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.2"
    # 让窗口与 va-capture.desktop 关联起来：
    # 不设的话 WM_CLASS 会是 "__main__.py"，任务栏只能显示通用/python 图标
    try:
        GLib.set_prgname("va-capture")
        GLib.set_application_name("VA Capture")
        Gdk.set_program_class("va-capture")
        Gtk.Window.set_default_icon_name("va-capture")
    except Exception:
        pass

    _lock = _acquire_single_instance()
    if _lock is None:
        print("va-capture 已在运行（单实例保护），本次启动退出")
        return
    app = App(host)
    # 不自动连接：窗口先出来，由用户点「连接」再拉流。
    # 但启动 1 秒后自动扫一次网段，把设备列进下拉框（多设备时不用手输 IP）
    if os.environ.get("TVMON_NO_SCAN") != "1":
        GLib.timeout_add(1000, lambda: (app.scan_devices(), False)[1])
    # 需要自动连时设置环境变量 TVMON_AUTOCONNECT=1 即可。
    if os.environ.get("TVMON_AUTOCONNECT") == "1":
        GLib.timeout_add(300, lambda: (app.connect(), False)[1])

    if os.environ.get("TVMON_SELFTEST"):
        def _selftest():
            print("[selftest] 截图…", flush=True)
            app.snapshot()
            print("[selftest] 开始录像…", flush=True)
            app.btn_recv.set_active(True)
            GLib.timeout_add(3000, _selftest_stop)
            return False

        def _selftest_stop():
            print("[selftest] 停止录像…", flush=True)
            app.btn_recv.set_active(False)
            GLib.timeout_add(1500, lambda: (print("[selftest] 完成", flush=True),
                                            Gtk.main_quit())[1])
            return False

        GLib.timeout_add(9000, _selftest)

    Gtk.main()


if __name__ == "__main__":
    main()
