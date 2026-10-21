"""RTSP 拉流线程：视频 / 8 轨音频，以及解码能力探测"""
import os, subprocess, threading, time
import numpy as np
import fcntl
import collections
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import GLib, GdkPixbuf, Pango, Gdk, Gtk

from .constants import AUDIO_CH, AUDIO_RATE, AUDIO_BLOCK, AUDIO_BYTES

def probe_video(url, timeout=10):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "csv=p=0", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout).stdout.strip().split(",")
        if len(out) >= 2 and out[0].isdigit():
            return int(out[0]), int(out[1])
    except Exception:
        pass
    return None



def kill_all_orphan_streams():
    """
    启动时清扫**所有**遗留拉流进程（不区分设备）。

    为什么需要：kill_orphan_streams(host) 只清理"要连接的那台"，
    切到别的设备后，原设备的 ffmpeg 会一直挂着：
      - 占着设备端 RTSP 客户端槽位（音频/视频都是单客户端设计）
      - 空转消耗 CPU、也让我们误以为设备还连着

    只杀命令行里同时含 ffmpeg 与 rtsp:// 的进程，不会误伤别的程序。
    """
    killed = 0
    try:
        me = os.getpid()
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if cmd.startswith("ffmpeg") or "/ffmpeg" in cmd.split(" ")[0]:
                if "rtsp://" in cmd:
                    try:
                        os.kill(int(pid), 9)
                        killed += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return killed


def kill_orphan_streams(host):
    """
    启动时清理上一次实例遗留的拉流进程。

    App 被强制结束时（kill -9、崩溃），它派生的 ffmpeg 会变成孤儿进程，
    继续占着设备端 rtsp-av 的**单客户端**槽位，导致新实例拿不到音频。
    这里按命令行匹配本机 IP 清理一次。
    """
    killed = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if "ffmpeg" in cmd and host in cmd and "rtsp://" in cmd:
                try:
                    os.kill(int(pid), 9)
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return killed


def probe_vaapi(url=None, timeout=20):
    """
    探测硬件解码能力，返回 "vaapi" / "vaapi_sw" / "sw"。

    关键：**不再连设备的 RTSP**，改用本地生成的 H.264 码流测试。
    原因：设备的 RTSP 服务端客户端槽位有限，App 正在拉流时再去探测
    会被拒绝或抢不到带宽，导致误判"硬解不可用"（这是个真踩过的坑）。

      "vaapi"    全 GPU 管线（解码 + scale_vaapi 缩放）
      "vaapi_sw" 仅 GPU 解码，缩放走 CPU
      "sw"       软件解码
    """
    tmp = os.path.join("/tmp", "tvmon_probe.h264")
    try:
        # 生成一小段 H.264（软件编码，约 0.3 秒）
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30",
             "-frames:v", "20", "-c:v", "libx264", "-preset", "ultrafast",
             tmp],
            timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0 or not os.path.exists(tmp):
            return "sw"
    except Exception:
        return "sw"

    # ① 全 GPU：解码 + 缩放都在显卡
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
             "-i", tmp,
             "-vf", "scale_vaapi=320:240,hwdownload,format=nv12",
             "-f", "null", "-"],
            timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return "vaapi"
    except Exception:
        pass

    # ② 仅 GPU 解码，缩放交给 CPU
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-hwaccel", "vaapi", "-i", tmp, "-f", "null", "-"],
            timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return "vaapi_sw"
    except Exception:
        pass
    return "sw"



class VideoReader(threading.Thread):
    """ffmpeg -> rgb24 -> GdkPixbuf"""

    def __init__(self, url, on_frame, on_status, target=None):
        super().__init__(daemon=True)
        self.url, self.on_frame, self.on_status = url, on_frame, on_status
        self.target = target
        # 解码模式：vaapi=全GPU管线, vaapi_sw=GPU解码+CPU缩放, sw=纯软解
        self.hwmode = "sw"
        self._switching = False      # 主动热切换时不计入失败次数
        self.transport = "udp"
        # 由 App 注入；未注入时退回 on_status，避免 AttributeError
        self.on_video_info = on_status
        self._stop = threading.Event()
        self.proc = None

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def run(self):
        """外层重连循环：网络抖动/服务重启后自动恢复"""
        hw_fails = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._run_once()
            except Exception as e:
                GLib.idle_add(self.on_status, f"视频异常: {e}")
            dur = time.time() - t0

            # 硬件解码不可用时（比如账号不在 render 组）会立刻失败，
            # 若连续两次都秒断，自动退回软件解码，避免无限重连
            if self._switching:          # 主动切换导致的重连，跳过降级判定
                self._switching = False
                hw_fails = 0
            elif self.hwmode != "sw" and dur < 15.0:
                hw_fails += 1
                if hw_fails >= 3:
                    hw_fails = 0
                    # 降级阶梯：全GPU -> GPU解码+CPU缩放 -> 纯软解
                    if self.hwmode == "vaapi":
                        self.hwmode = "vaapi_sw"
                        msg = "全 GPU 管线失败，改用「硬件解码+软件缩放」"
                    else:
                        self.hwmode = "sw"
                        msg = "硬件解码不可用，已切回软件解码"
                    GLib.idle_add(self.on_status, msg)
            else:
                hw_fails = 0

            if self._stop.is_set():
                break
            GLib.idle_add(self.on_status, "视频断开，3 秒后重连…")
            time.sleep(3)

    def _run_once(self):
        # 视频服务可能在客户端断开时被重启，这里带重试
        info = None
        for attempt in range(60):
            if self._stop.is_set():
                return
            info = probe_video(self.url, timeout=5)
            if info:
                break
            # 只报第 1/5/20/60 次，避免设备挂着时日志刷屏
            if attempt + 1 in (1, 5, 20, 60):
                GLib.idle_add(self.on_status,
                              f"等待视频服务… ({attempt + 1})")
            # 间隔要够长：设备端每次 RTSP 会话都会占一块摄像头缓冲，
            # 频繁重连会把它撑到 OOM
            time.sleep(5)
        if not info:
            GLib.idle_add(self.on_status, "视频探测失败")
            return
        sw, sh = info
        # 直接解码成显示尺寸，避免"ffmpeg 缩一次 + Python 再缩一次"的双重开销
        tw, th = self.target or (960, 540)
        tw = max(2, int(tw) & ~1)
        th = max(2, int(th) & ~1)
        w, h = tw, th

        # UDP 默认接收缓冲区只有 208KB，1080p 的 I 帧一次突发就有几百 KB，
        # 缓冲区溢出会丢包 -> 花屏（要等到下一个 I 帧才恢复，GOP=2 秒）。
        # 放大到 4MB 可基本消除。
        buf_args = ["-buffer_size", "4194304"]
        tp = self.transport

        if self.hwmode == "vaapi":
            # 全 GPU 管线：解码 + 缩放都在显卡上做，CPU 几乎为零
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error",
                   "-rtsp_transport", tp] + buf_args + [
                   "-fflags", "nobuffer+discardcorrupt",
                   "-flags", "low_delay",
                   "-probesize", "32768", "-analyzeduration", "0",
                   "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                   "-i", self.url, "-an",
                   "-vf", (f"scale_vaapi={w}:{h},hwdownload,format=nv12,"
                           f"format=rgb24"),
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        elif self.hwmode == "vaapi_sw":
            # GPU 解码，缩放格式转换用 CPU：兼容性最好，解码开销仍然省下来
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error",
                   "-rtsp_transport", tp] + buf_args + [
                   "-fflags", "nobuffer+discardcorrupt",
                   "-flags", "low_delay",
                   "-probesize", "32768", "-analyzeduration", "0",
                   "-hwaccel", "vaapi",
                   "-i", self.url, "-an",
                   "-vf", f"scale={w}:{h}:flags=fast_bilinear",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        else:
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error",
                   "-rtsp_transport", tp] + buf_args + [
                   "-fflags", "nobuffer+discardcorrupt",
                   "-flags", "low_delay",
                   "-probesize", "32768", "-analyzeduration", "0",
                   "-threads", "2",
                   "-i", self.url, "-an",
                   "-vf", f"scale={w}:{h}:flags=fast_bilinear",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL,
                                         bufsize=w * h * 3)
            try:
                fcntl.fcntl(self.proc.stdout.fileno(), 1031, 1024 * 1024)
            except Exception:
                pass
        except Exception as e:
            GLib.idle_add(self.on_status, f"启动 ffmpeg 失败: {e}")
            return

        mode = {"vaapi": "硬件解码", "vaapi_sw": "硬件解码+软缩放",
                "sw": "软件解码"}.get(self.hwmode, "软件解码")
        GLib.idle_add(self.on_video_info, sw, sh, w, h, mode)
        size = w * h * 3
        n, t0 = 0, time.time()

        while not self._stop.is_set():
            buf = self.proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            pb = GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(buf), GdkPixbuf.Colorspace.RGB, False, 8,
                w, h, w * 3)
            # 关键：不要用 GLib.idle_add 每帧投递一个 1.6MB 的回调，
            # 主循环跟不上时会在 idle 队列里无限堆积（实测涨到 30MB/s）。
            # 这里只保留"最新一帧"，旧帧直接丢弃。
            self.on_frame(pb)

            n += 1
            now = time.time()
            if now - t0 >= 2.0:
                n, t0 = 0, now

        try:
            self.proc.kill()
        except Exception:
            pass
        GLib.idle_add(self.on_status, "视频已断开")


class AudioReader(threading.Thread):
    """ffmpeg -> s16le 8ch -> 电平 + 波形"""

    def __init__(self, url, on_audio, on_status):
        super().__init__(daemon=True)
        self.url, self.on_audio, self.on_status = url, on_audio, on_status
        self._stop = threading.Event()
        self.proc = None

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def run(self):
        """外层重连循环：换网线、服务重启后音频能自动恢复"""
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as e:
                GLib.idle_add(self.on_status, f"音频异常: {e}")
            if self._stop.is_set():
                break
            self._retry = getattr(self, "_retry", 0) + 1
            if self._retry in (1, 5, 20) or self._retry % 60 == 0:
                GLib.idle_add(
                    self.on_status, f"音频断开，2 秒后重连… (第 {self._retry} 次)")
            time.sleep(2)

    def _run_once(self):
        # 8 条独立单声道 track 必须用 amerge 合成一条 8 通道流；
        # 只写 -ac 8 是错的（那只是把一条流重采样成 8 通道）。
        graph = "".join(f"[0:a:{i}]" for i in range(AUDIO_CH))
        graph += f"amerge=inputs={AUDIO_CH}[a]"
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error",
               "-rtsp_transport", "udp", "-i", self.url,
               "-filter_complex", graph, "-map", "[a]",
               "-f", "s16le", "-acodec", "pcm_s16le",
               "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CH), "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL,
                                         bufsize=AUDIO_BYTES * 4)
        except Exception as e:
            GLib.idle_add(self.on_status, f"音频启动失败: {e}")
            return

        self._retry = 0
        GLib.idle_add(self.on_status, "8 轨音频已连接")

        while not self._stop.is_set():
            raw = self.proc.stdout.read(AUDIO_BYTES)
            if not raw or len(raw) < AUDIO_BYTES:
                break
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            a = a.reshape(AUDIO_BLOCK, AUDIO_CH).T / 32768.0
            # 同样用有界队列，溢出时丢最旧的块
            self.on_audio(a)

        try:
            self.proc.kill()
        except Exception:
            pass
        GLib.idle_add(self.on_status, "音频已断开")


# --------------------------------------------------------------------------

# 3x5 点阵数字（用于在罗盘上标麦克风编号）
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

