# 代码结构

```
src/va_capture/
├── __init__.py      版本号
├── __main__.py      入口（python3 -m va_capture）
├── constants.py     全局常量、默认配置、方向名/置信度文案
├── config.py        配置读写、CSS 主题、titled() 等小工具
├── readers.py       RTSP 拉流线程（视频/音频）+ VAAPI 探测
├── dsp.py           声源定位（SRP-PHAT/MUSIC/MVDR）+ 事件检测
├── widgets.py       自绘控件（电平/波形/罗盘/瀑布图）
├── dialogs.py       校准向导、设置对话框
└── app.py           主窗口与业务逻辑
```

## 数据流

```
设备 rtsp://ip/live ──► VideoReader ──► GdkPixbuf ──► Gtk.Image
                                   └──► 每设备最新帧字典

设备 rtsp://ip:8554/live ──► AudioReader ──► 有界队列
                                              │
                              _audio_tick ◄───┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
               电平表/波形    事件检测        Locator
                                             （方位角）
```

## 关键设计

**每设备独立读取器**：`App.devices[host]` 里保存 `vreader` / `areader` /
`newest_frame` / `epoch`。切换设备只改变"显示哪一台"，不中断其他设备。

**世代号（epoch）**：每次连接/断开自增，回调里校验。用来丢弃切换瞬间
旧设备残留的数据 —— 否则波形会"定格"在上一台设备的最后一帧。

**多客户端音频**：设备端 `rtsp-av` 支持最多 4 个 RTP 目标，所以
App 的实时显示流和录制流可以并行，不会互相挤掉。

**固定尺寸渲染**：所有自绘控件用固定常量渲染（numpy → GdkPixbuf），
不能用 `get_allocation()` 回读尺寸 —— 那会造成尺寸反馈循环，控件越渲染越大。

**无 cairo 依赖**：环境里 PyGObject 没有 cairo 绑定，所有图形都用 numpy
画像素，数字用 3×5 点阵字体（`draw_digit`）。

## 扩展指引

- **加新定位算法**：在 `dsp.py` 的 `Locator` 里加方法，在 `app.py` 的
  `_apply_algorithm()` 里注册
- **加新控件**：放 `widgets.py`，继承 `Gtk.Image`，用固定尺寸渲染
- **改录制逻辑**：`app.py` 的 `toggle_rec_video()` / `_flac_loop()`
