#!/bin/bash
# 把 va-capture 安装/更新为 systemd 用户服务
#
# 好处：
#   1. 独立 cgroup —— 不会被终端里其它进程的内存问题连带杀掉
#   2. 崩溃自动重启（5 秒后）
#   3. 内存上限 2G，不会拖垮整机
#
# 用法: ./scripts/install-service.sh [--remove]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/va-capture.service"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now va-capture.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "✓ 已卸载 va-capture.service"
    exit 0
fi

# 优先用源码树：仓库里的版本通常比 /usr 里装的新
# （系统里可能还留着很老的 .deb 安装，直接用会跑成旧版本，日志都没有）
if [ -x "$ROOT/bin/va-capture" ] && [ -d "$ROOT/src/va_capture" ]; then
    # systemd 的 ExecStart 对带空格的路径很挑剔（本仓库路径含空格），
    # 建一个无空格的软链接再指过去，稳妥
    mkdir -p "$HOME/.local/bin"
    ln -sf "$ROOT/bin/va-capture" "$HOME/.local/bin/va-capture"
    EXEC="$HOME/.local/bin/va-capture"
    echo "  运行方式: 源码树 $(grep -oP '__version__\s*=\s*"\K[^"]+' "$ROOT/src/va_capture/__init__.py" 2>/dev/null)"
elif [ -d /usr/lib/va-capture/va_capture ]; then
    EXEC="/usr/bin/va-capture"
    echo "  运行方式: 系统安装 /usr/lib/va-capture"
else
    echo "✗ 既没有源码树也没有系统安装"; exit 1
fi

mkdir -p "$UNIT_DIR"
sed "s|@EXEC@|$EXEC|" "$ROOT/packaging/va-capture.service" > "$UNIT"

systemctl --user daemon-reload
systemctl --user enable va-capture.service
systemctl --user restart va-capture.service
sleep 2
systemctl --user --no-pager status va-capture.service | head -6
echo
echo "✓ 已安装并启动"
echo "  日志: journalctl --user -u va-capture -f"
echo "  停止: systemctl --user stop va-capture"
echo "  卸载: $0 --remove"
