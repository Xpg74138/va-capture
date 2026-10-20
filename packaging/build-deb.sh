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
exit 0
POST
chmod 755 "$BUILD/DEBIAN/postinst"

cat > "$BUILD/DEBIAN/prerm" <<'PRE'
#!/bin/sh
set -e
exit 0
PRE
chmod 755 "$BUILD/DEBIAN/prerm"

dpkg-deb --root-owner-group --build "$BUILD" "$ROOT/build/${PKG}.deb" >/dev/null
# 再复制一份固定名字的，上传 Release 后可用
#   .../releases/latest/download/va-capture_all.deb 永远指向最新版
cp "$ROOT/build/${PKG}.deb" "$ROOT/build/va-capture_all.deb"
echo "✓ 已生成: build/${PKG}.deb"
echo "✓ 固定名: build/va-capture_all.deb"
dpkg-deb --info "$ROOT/build/${PKG}.deb" | sed -n '1,12p'
