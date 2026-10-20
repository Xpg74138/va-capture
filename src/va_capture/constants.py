"""TinyVision 监看台 —— 常量和全局配置"""
import os
import collections
import numpy as np

AUDIO_CH = 8

# 录制码率估算（Mbps）：视频实测 ~10.7，音频 8×48k×16bit = 6.14
EST_VIDEO_MBPS = 10.7
EST_AUDIO_MBPS = 6.14


# ---- 界面统一尺寸（所有面板宽高对齐）----
PANEL_W = 252        # 右侧栏所有组件宽度
DEV_W = 380          # 左下设备列表宽度
LEVELS_H = 200       # 电平表
WATERFALL_H = 90     # 瀑布图
LOGBOX_H = 130       # 事件记录
COMPASS_S = 220      # 罗盘（正方形）
WAVE_W = 560         # 波形宽度（设备列表占用一部分）
WAVE_H = 200         # 波形高度
AUDIO_RATE = 48000
AUDIO_BLOCK = 480                       # 10ms
AUDIO_BYTES = AUDIO_BLOCK * AUDIO_CH * 2

# 录像/截图/录音的自动保存根目录（不再弹文件对话框，
# 因为主窗口是 keep_above，对话框会被盖住导致"点了没反应"）
DEFAULT_SETTINGS = {
    "out_root": "",                 # 空=自动选择
    "snap_full_res": True,          # 截图用原始分辨率
    "display_scale": "fit",         # fit / 960x540 / 1280x720
    "meter_floor": -60,             # 电平表下限 dBFS
    "angle_offset": 0.0,            # 阵列安装角度偏移（度）
    "log_csv": False,               # 是否把方位写入 CSV
    "sensitivity": "medium",        # low/medium/high -> 置信度门限
    "hwaccel": "auto",              # auto / vaapi / off
    "mode": "event",                # event(事件定位) / continuous(连续跟踪)
    "event_sens": "medium",         # low/medium/high 事件检测灵敏度
    "algorithm": "srp",             # srp / music / mvdr / fusion
    "transport": "udp",             # udp / tcp（花屏严重时切 tcp）
    "rec_segment_min": 60,          # 录制分段时长（分钟）
    "rec_total_hours": 0,           # 录制总时长（小时，0=不限）
    "rec_audio_codec": "flac",      # flac（无损压缩，省49%）/ wav（未压缩）
}



def dir_name(az):
    """方位角 -> 中文方向名"""
    a = az % 360.0
    if a < 22.5 or a >= 337.5:
        return "前"
    if a < 67.5:
        return "右前"
    if a < 112.5:
        return "右"
    if a < 157.5:
        return "右后"
    if a < 202.5:
        return "后"
    if a < 247.5:
        return "左后"
    if a < 292.5:
        return "左"
    return "左前"


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
