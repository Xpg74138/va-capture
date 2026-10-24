# VA Capture

TinyVision V851se 设备的**音视频监看台**：实时预览摄像头画面与 8 通道麦克风阵列，
内置声源定位（方位角）、事件触发检测，并支持多设备管理与独立录制。

[![Release](https://img.shields.io/github/v/release/Xpg74138/va-capture?color=4ade80)](https://github.com/Xpg74138/va-capture/releases/latest)
[![License](https://img.shields.io/github/license/Xpg74138/va-capture?color=38bdf8)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%2B-orange)]()
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)]()


![界面](docs/screenshot.png)

## 功能

- **实时视频**：1080p H.264 硬解（VAAPI）/ 软解自动选择，UDP/TCP 传输可切换
- **8 通道音频**：8×48kHz 16bit PCM，实时电平表、波形、瀑布图
- **声源定位**：SRP-PHAT / MUSIC / MVDR 三种算法，4 麦克风对/28 对 GCC-PHAT
  - 单声源精度约 1.4°，双声源分辨率约 34°（宽带）
  - 方位罗盘 + 历史轨迹 + 置信度
- **事件触发**：按 300–3000Hz 能量突变检测猪咳/狗吠等瞬态事件，触发才定位
- **多设备管理**：自动发现局域网设备、下拉框/列表切换、每台独立状态与录制
- **录制**：视频 + 8 轨音频同步录制，按小时分段，音频 FLAC 无损压缩
- **拍照 / 校准**：原始分辨率截图，阵列角度自动校准

## 安装

### 方式一：.deb 包（推荐）

从 [Releases](https://github.com/Xpg74138/va-capture/releases/latest) 下载最新版：

```bash
# 下载并安装（自动装依赖）
# 文件名固定为 va-capture_all.deb，始终指向最新版
wget https://github.com/Xpg74138/va-capture/releases/latest/download/va-capture_all.deb
sudo apt install ./va-capture_1.0.0_all.deb
```

> 用 `apt install ./xxx.deb` 而不是 `dpkg -i`，这样依赖会被自动安装。
> 依赖：`python3-gi`、`gir1.2-gtk-3.0`、`python3-numpy`、`ffmpeg`

安装后从**应用菜单**启动「VA Capture」，或命令行：

```bash
va-capture 192.168.2.10      # 指定设备 IP（可省略）
```

卸载：

```bash
sudo apt remove va-capture
```

### 方式二：从源码运行

```bash
git clone https://github.com/Xpg74138/va-capture.git
cd va-capture
sudo apt install python3-gi gir1.2-gtk-3.0 python3-numpy ffmpeg
./bin/va-capture 192.168.2.10
```

若系统 Python 缺少 numpy，启动器会自动回退到开发用的 venv
（可用 `VA_CAPTURE_PYTHON=/path/to/python` 指定）。

### 方式三：自己打包 .deb

```bash
./packaging/build-deb.sh        # 生成 build/va-capture_<版本>_all.deb
```

推 `v*` 标签时 GitHub Actions 会自动构建并发布到 Release。

## 使用

1. 启动后窗口立即出现，1 秒后自动扫描局域网
2. 顶部下拉框选择设备 → 点「连接」
3. 左下列表显示所有已连接设备及其状态（已连接 / 录制中）
4. 点「录制音视频」开始录制当前设备，切到其他设备再点录制可同时录制多台

### 键盘与交互

| 操作 | 说明 |
|---|---|
| 点击列表行 | 切换显示该设备（不自动连接） |
| 设备列表 | 只显示当前扫描到的设备；录制中的设备会显示**已录时长**（每秒刷新） |
| 「别名」按钮 | 给当前设备起个可读名字（前门 / 猪舍-东），界面和录制目录都用它 |
| 点「连接」 | 连接当前 IP，开始接收 |
| 点「截帧」 | 保存原始分辨率截图 |
| 点「校准」 | 引导式阵列角度校准 |

## 设备别名

点工具栏「别名」给当前设备起个可读名字，例如 `前门`、`猪舍-东`、`一号机`。

- **界面**：下拉框显示 `前门 · 192.168.2.42`，设备列表显示 `▶ 前门 192.168.2.42`
- **录制目录**：`recordings/前门/20261023_173635/seg_000.mp4`
  - 没设别名时仍用 `dev_<IP>`
  - 两台设备别名相同时自动追加 IP 区分
  - 非法文件名字符（`\ / : * ? " < > |`）自动替换成 `_`

别名保存在 `~/.config/va-capture/settings.json` 的 `aliases` 字段里，重装不丢。

## 数据目录

录制默认保存在 `~/VA Capture/recordings/dev_<IP>/<时间戳>/`：

```
seg_000.mp4   seg_000.flac
seg_001.mp4   seg_001.flac
...
```

容量估算：约 **6.2 GB/小时**（视频 10.7Mbps + FLAC 音频），可在设置里调整。

## 录制：复用显示流（重要）

设备端的视频推流服务是**单客户端**的预编译程序。如果「界面显示」和「录制」
各开一路视频会话，设备侧就会同时存在 2 个客户端，带来两个后果：

- 预编译程序异常退出的概率大幅上升
- 每次异常退出会遗留约 **25 MB** 孤儿 ION 缓冲，而整机只有 51 MB
  → 一两次之后必然 OOM → 设备掉线

因此，录制**当前正在显示**的设备时，App 采用**复用模式**：
一个 ffmpeg 进程同时完成三件事，全程只占 **1 路视频客户端**：

```
rtsp://<ip>/live  ─┐
                    ├─► 一个 ffmpeg ─┬─► seg_%03d.mp4   (-c copy，零损失)
rtsp://<ip>:8554 ──┘                 ├─► seg_%03d.flac  (8 轨合并)
                                     └─► 原始帧 → 界面显示
```

录制**其它设备**（非当前显示）时，仍然单独起一个进程 —— 因为那台设备
本来就没有显示会话，总共也是 1 路客户端。

## 后台常驻运行（推荐）

长时间测试时，**不要从终端里直接启动** —— 终端的 cgroup 里若有别的程序
吃光内存，`systemd-oomd` 会把整个 cgroup 杀掉，App 会被连带杀死
（现场发生过：同一 cgroup 里某进程涨到 13 GB，App 一起被杀）。

装成 systemd 用户服务即可，独立 cgroup + 崩溃自动重启 + 内存上限：

```bash
./scripts/install-service.sh          # 安装并启动
systemctl --user status va-capture    # 看状态
journalctl --user -u va-capture -f    # 看输出
systemctl --user stop va-capture      # 停止
./scripts/install-service.sh --remove # 卸载
```

服务配置要点：

```ini
Restart=on-failure      # 崩溃 5 秒后自动拉起
MemoryHigh=1G           # 超过开始回收
MemoryMax=2G            # 硬上限，绝不会拖垮整机
OOMPolicy=stop
```

## 日志（长期运行排错用）

应用会把关键事件写到日志文件，便于事后倒查时间线：

```
~/.local/state/va-capture/app.log
```

记录内容：

| 类型 | 例子 |
|---|---|
| 启动信息 | 版本、日志路径、当前设备、解码方式、传输协议、算法、模式 |
| 连接事件 | `解码模式（缓存）：GPU解码+软缩放`、`8 轨音频已连接` |
| 状态变化 | 断开、重连、清理残留进程 |
| **每分钟统计** | `stats: fps=29.8 lag=6ms audio=-17.3dB devs=192.168.2.78[已连接]` |
| 录制事件 | 开始/停止、文件路径、ffmpeg 报错 |
| 定位事件 | 方位角与置信度 |

规则：

- 单文件 2 MB 自动轮转为 `app.log.1`
- 写入失败不影响主功能（静默降级）

**和固件日志对照**：设备端会把运行状态写到 SD 卡的
`/mnt/UDISK/logs/device.log`（每 30 秒一行）和 `kernel.log`（dmesg 快照），
两边时间一对比就能判断是设备侧挂了还是网络断了。

查看：

```bash
tail -f ~/.local/state/va-capture/app.log
```

## 配置

设置保存在 `~/.config/va-capture/settings.json`，包括：

- 传输协议（udp/tcp）、定位算法、事件灵敏度、定位模式
- 视频解码方式、显示尺寸、电平下限、角度偏移
- 录制分段时长、总时长、音频编码（FLAC/WAV）、保存路径

## 设备端要求

设备需运行 TinyVision 固件并启动两个服务：

| 服务 | 地址 | 说明 |
|---|---|---|
| `tinyvision-ipc` | `rtsp://<ip>/live` | 1080p H.264 视频 |
| `rtsp-av` | `rtsp://<ip>:8554/live` | 8 轨 L16 48kHz 音频 |

固件改动见 [tinyvision-sdk-tracker](https://github.com/Xpg74138/tinyvision-sdk-tracker)。

## 故障排查

| 现象 | 原因/处理 |
|---|---|
| 扫不到设备 | 设备要同时开放 554 和 8554 端口 |
| 花屏 | 改用 TCP 传输（设置 → 传输协议） |
| 硬解不可用 | 需 `render` 组权限；日志会记录探测结果 |
| 音频不刷新 | 设备端 `rtsp-av` 需支持多客户端（见 SDK 补丁） |
| 定位不准 | 先跑一次「校准」；窄带声源建议换 MUSIC/MVDR |

## 许可证

MIT
