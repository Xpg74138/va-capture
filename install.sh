#!/bin/bash
# 源码安装（无需打包）：安装依赖 + 注册桌面入口 + 建立命令
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"

echo "==> 检查依赖"
MISSING=()
python3 -c "import gi" 2>/dev/null || MISSING+=("python3-gi")
python3 -c "import numpy" 2>/dev/null || MISSING+=("python3-numpy")
command -v ffmpeg >/dev/null || MISSING+=("ffmpeg")
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "缺少: ${MISSING[*]}"
    echo "请先执行: sudo apt install ${MISSING[*]} gir1.2-gtk-3.0"
    exit 1
fi

echo "==> 安装到 $PREFIX"
mkdir -p "$PREFIX/lib/va-capture/va_capture" "$PREFIX/bin" "$PREFIX/share/applications" \
         "$PREFIX/share/icons/hicolor/scalable/apps"
cp "$HERE"/src/va_capture/*.py "$PREFIX/lib/va-capture/va_capture/"

cat > "$PREFIX/bin/va-capture" <<LAUNCH
#!/bin/sh
export PYTHONPATH="$PREFIX/lib/va-capture\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m va_capture "\$@"
LAUNCH
chmod +x "$PREFIX/bin/va-capture"

sed "s|^Exec=.*|Exec=$PREFIX/bin/va-capture|" "$HERE/packaging/va-capture.desktop" \
    > "$PREFIX/share/applications/va-capture.desktop"
cp "$HERE/packaging/va-capture.svg" "$PREFIX/share/icons/hicolor/scalable/apps/"

command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" || true

echo "==> 完成"
echo "    运行: $PREFIX/bin/va-capture"
[ -d "$PREFIX/share/applications" ] && echo "    应用菜单里也能找到「VA Capture」"
