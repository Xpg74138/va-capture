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

# 启动器（安装版：PYTHONPATH 指向 /usr/lib/va-capture）
cat > "$BUILD/usr/bin/va-capture" <<'LAUNCH'
#!/bin/sh
export PYTHONPATH="/usr/lib/va-capture${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m va_capture "$@"
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

# 给"执行 sudo 的那个用户"装好 systemd 用户服务并启用（随登录自启）。
# 这样换台电脑只要装上这个 deb 就完事，不用再跑额外脚本。
U="${SUDO_USER:-}"
if [ -n "$U" ] && [ "$U" != "root" ] && id "$U" >/dev/null 2>&1; then
    UH="$(getent passwd "$U" | cut -d: -f6)"
    UD="$UH/.config/systemd/user"
    mkdir -p "$UD" 2>/dev/null || true
    ln -sf /usr/lib/systemd/user/va-capture.service "$UD/va-capture.service" 2>/dev/null || true
    chown -h "$U:$U" "$UD/va-capture.service" 2>/dev/null || true
    # 以该用户身份启用（失败不阻塞安装）
    runuser -u "$U" -- systemctl --user daemon-reload 2>/dev/null || true
    runuser -u "$U" -- systemctl --user enable va-capture.service 2>/dev/null || true
    echo "已为 $U 启用 va-capture 用户服务（下次登录自动启动，内存上限 2G）"
    echo "  立即启动: systemctl --user start va-capture"
    echo "  查看日志: journalctl --user -u va-capture -f"
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
