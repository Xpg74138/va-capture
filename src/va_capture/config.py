"""配置读写、CSS、通用小工具"""
import json, os, time
import numpy as np
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, Pango, GdkPixbuf, GLib

from .constants import *
from .constants import AUDIO_CH, DEFAULT_SETTINGS, PANEL_W

def _config_path():
    # 测试/多实例时可用环境变量指定独立配置文件，避免污染真实配置
    env = os.environ.get("VA_CAPTURE_CONFIG")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.expanduser("~/.config"), here):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".tvmon_probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return os.path.join(d, "tinyvision-monitor.json")
        except Exception:
            continue
    return "/tmp/tinyvision-monitor.json"


CONFIG_PATH = _config_path()


def load_settings():
    st = dict(DEFAULT_SETTINGS)
    try:
        with open(CONFIG_PATH) as f:
            st.update(json.load(f))
    except Exception:
        pass
    return st


def save_settings(st):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


_CSS_DONE = [False]


def _apply_css():
    """统一所有文字的字体与字号，避免不同控件大小不一致"""
    if _CSS_DONE[0]:
        return
    _CSS_DONE[0] = True
    from gi.repository import Gdk
    try:
        st = Gtk.Settings.get_default()
        if st:
            st.set_property("gtk-application-prefer-dark-theme", True)
    except Exception:
        pass
    css = b"""
    * { font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", Sans; font-size: 10pt; }
    label { color: #d7dbe3; }
    .val   { font-family: Monospace; font-size: 11pt; font-weight: bold; color: #eef2f8; }
    .dim   { color: #8b93a3; font-size: 9pt; }
    button { padding: 3px 10px; }
    entry  { font-family: Monospace; }
    statusbar { font-size: 9pt; }
    """
    prov = Gtk.CssProvider()
    prov.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), prov,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def titled(title, widget):
    """把控件包进带标题的外框（标题在左上角），统一边距"""
    f = Gtk.Frame(label=None)
    lbl = Gtk.Label()
    lbl.set_markup(f"<span size='small' color='#9aa3b2'>{title}</span>")
    lbl.set_xalign(0.0)
    lbl.set_margin_start(4)
    f.set_label_widget(lbl)
    f.add(widget)
    f.set_size_request(PANEL_W, -1)
    return f


def _pick_out_root():
    """依次尝试若干可写目录，选第一个能写的作为输出根目录"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("TVMON_OUT"),
                 os.path.expanduser("~/TinyVision"),
                 os.path.join(here, "recordings"),
                 "/tmp/TinyVision"):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write_probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return cand
        except Exception:
            continue
    return "/tmp/TinyVision"


OUT_ROOT = _pick_out_root()

# 方位约定：0°=设备正前方，逆时针增加，90°=左，180°=后，270°=右
_DIR_NAMES = ["前", "左前", "左", "左后", "后", "右后", "右", "右前"]


def dir_name(az):
    return _DIR_NAMES[int(((az % 360) + 22.5) // 45) % 8]


def conf_word(conf):
    if conf >= 3.0:
        return "可靠"
    if conf >= 2.0:
        return "一般"
    if conf >= 1.4:
        return "较弱"
    return "不可信"

CH_COLORS = [
    (0.31, 0.63, 1.00), (0.24, 0.86, 0.52), (1.00, 0.75, 0.24),
    (1.00, 0.54, 0.30), (1.00, 0.35, 0.30), (0.78, 0.42, 1.00),
    (0.30, 0.82, 0.88), (0.94, 0.38, 0.57),
]


# --------------------------------------------------------------------------