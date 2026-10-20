"""
应用日志：写到 ~/.local/state/va-capture/app.log

目的：长期运行时出问题（设备掉线、花屏、录制中断）后能倒查时间线，
和固件侧 /mnt/UDISK/logs/device.log 对照。

格式：每行 `YYYY-MM-DD HH:MM:SS.mmm [级别] 消息`
轮转：单文件 2 MB，保留一份 .1
"""
import os
import threading
import time

_MAX_BYTES = 2 * 1024 * 1024
_lock = threading.Lock()
_fp = None


def log_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(base, "va-capture")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".va-capture.log")
    return os.path.join(d, "app.log")


def init():
    """打开日志文件；失败则静默降级（不影响主功能）"""
    global _fp
    p = log_path()
    try:
        if _fp is None:
            _fp = open(p, "a", encoding="utf-8", buffering=1)
    except Exception:
        _fp = None
    return p


def _rotate():
    global _fp
    p = log_path()
    try:
        if _fp:
            _fp.close()
            _fp = None
        if os.path.exists(p) and os.path.getsize(p) > _MAX_BYTES:
            os.replace(p, p + ".1")
    except Exception:
        pass
    try:
        _fp = open(p, "a", encoding="utf-8", buffering=1)
    except Exception:
        _fp = None


def log(msg, level="INFO"):
    """写一行日志。任何异常都不抛出。"""
    line = "%s.%03d [%s] %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), int((time.time() % 1) * 1000), level, msg)
    with _lock:
        try:
            if _fp is None:
                init()
            if _fp is None:
                return
            _fp.write(line + "\n")
            if _fp.tell() > _MAX_BYTES:
                _rotate()
        except Exception:
            pass


def log_exc(msg, exc):
    log("%s: %s: %s" % (msg, type(exc).__name__, exc), level="ERROR")
