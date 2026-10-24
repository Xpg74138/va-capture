#!/bin/bash
# 构建 va-capture 的 Debian 安装包
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VERSION="$(grep -oP '__version__\s*=\s*"\K[^"]+' "$ROOT/src/va_capture/__init__.py")"
PKG="va-capture_${VERSION}_all"
BUILD="$ROOT/build/$PKG"

rm -rf "$BUILD"
mkdir -p "$BUILD/DEBIAN" \
         "$BUILD/usr/lib/va-capture/va_capture" \
         "$BUILD/usr/bin" \
         "$BUILD/usr/share/applications" \
         "$BUILD/usr/share/icons/hicolor/scalable/apps" \
         "$BUILD/usr/lib/systemd/user" \
         "$BUILD/usr/share/doc/va-capture"

# 代码
install -m 644 "$ROOT"/src/va_capture/*.py "$BUILD/usr/lib/va-capture/va_capture/"

# 启动器 = 包装脚本
#   · 从服务里调用（带 VA_CAPTURE_SERVICE）→ 直接运行 App
#   · 从菜单/命令行调用 → 转去 systemctl --user start，让 App 在服务 cgroup 里跑
#     （从而拿到 2GB 内存上限和崩溃自愈）
#   · 没有 systemd 用户会话时 → 退化为直接运行
cat > "$BUILD/usr/bin/va-capture" <<'LAUNCH'
#!/bin/sh
export PYTHONPATH="/usr/lib/va-capture${PYTHONPATH:+:$PYTHONPATH}"

if [ -n "$VA_CAPTURE_SERVICE" ]; then
    exec python3 -m va_capture "$@"          # 已在服务内，直接跑
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    # 已经有实例在跑 → start 是幂等的，直接返回
    if systemctl --user start va-capture.service 2>/dev/null; then
        exit 0
    fi
fi

exec python3 -m va_capture "$@"              # 兜底：直接运行
LAUNCH
chmod 755 "$BUILD/usr/bin/va-capture"

install -m 644 "$ROOT/packaging/va-capture.desktop" "$BUILD/usr/share/applications/"
install -m 644 "$ROOT/packaging/va-capture.svg" \
    "$BUILD/usr/share/icons/hicolor/scalable/apps/va-capture.svg"
install -m 644 "$ROOT/packaging/va-capture.service" \
    "$BUILD/usr/lib/systemd/user/va-capture.service"
install -m 644 "$ROOT/README.md" "$BUILD/usr/share/doc/va-capture/README.md"

SIZE=$(du -sk "$BUILD/usr" | cut -f1)
cat > "$BUILD/DEBIAN/control" <<CTRL
Package: va-capture
Version: ${VERSION}
Section: video
Priority: optional
Architecture: all
Installed-Size: ${SIZE}
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, python3-numpy, ffmpeg
Maintainer: Xpg74138 <xpg74138@users.noreply.github.com>
Homepage: https://github.com/Xpg74138/va-capture
Description: TinyVision 8-mic array + camera monitor
 实时预览 TinyVision 设备的视频与 8 通道音频，支持 SRP-PHAT/MUSIC/MVDR
 声源定位、事件触发、多设备切换与独立录制（视频 + 8 轨 FLAC）。
CTRL

cat > "$BUILD/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi

# 用户服务单元放在 /usr/lib/systemd/user 下，所有用户都能直接 start，
# 但**不 enable** —— 按需求：只有打开 App 时才启动（见 /usr/bin/va-capture 包装脚本）
U="${SUDO_USER:-}"
if [ -n "$U" ] && [ "$U" != "root" ] && id "$U" >/dev/null 2>&1; then
    runuser -u "$U" -- systemctl --user daemon-reload 2>/dev/null || true
    echo "va-capture 已安装。从应用菜单打开即可（会自动以服务方式运行，"
    echo "带内存上限与崩溃自愈；不会随开机自启）。"
fi
exit 0
POST
chmod 755 "$BUILD/DEBIAN/postinst"

cat > "$BUILD/DEBIAN/prerm" <<'PRE'
#!/bin/sh
set -e
U="${SUDO_USER:-}"
if [ -n "$U" ] && [ "$U" != "root" ]; then
    UH="$(getent passwd "$U" | cut -d: -f6)"
    runuser -u "$U" -- systemctl --user disable --now va-capture.service 2>/dev/null || true
    rm -f "$UH/.config/systemd/user/va-capture.service" 2>/dev/null || true
fi
exit 0
PRE
chmod 755 "$BUILD/DEBIAN/prerm"

dpkg-deb --root-owner-group --build "$BUILD" "$ROOT/build/${PKG}.deb" >/dev/null
echo "✓ 已生成: build/${PKG}.deb"
dpkg-deb --info "$ROOT/build/${PKG}.deb" | sed -n '1,12p'
