#!/usr/bin/env sh
set -eu

APP_NAME="elgato-keylight-tray"
APP_TITLE="Elgato Key Light Tray"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VERSION=${1:-0.1.0}

BUILD_ROOT="$ROOT_DIR/build/appimage"
PYI_ROOT="$BUILD_ROOT/pyinstaller"
APPDIR="$BUILD_ROOT/$APP_NAME.AppDir"
DIST_DIR="$ROOT_DIR/dist"
PORTABLE_ROOT="$BUILD_ROOT/portable"

mkdir -p "$BUILD_ROOT" "$DIST_DIR" "$PORTABLE_ROOT"

ARCH_RAW=$(uname -m)
case "$ARCH_RAW" in
  x86_64|amd64)
    APPIMAGE_ARCH="x86_64"
    ;;
  aarch64|arm64)
    APPIMAGE_ARCH="aarch64"
    ;;
  *)
    echo "Nicht unterstutzte Architektur: $ARCH_RAW"
    exit 1
    ;;
esac

PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR="$BUILD_ROOT/.venv"
PY="$VENV_DIR/bin/python"

if [ ! -x "$PY" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  echo "pip fehlt in $VENV_DIR"
  exit 1
fi

"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r "$ROOT_DIR/requirements.txt" pyinstaller >/dev/null

rm -rf "$PYI_ROOT" "$APPDIR"
mkdir -p "$PYI_ROOT" "$APPDIR"

"$PY" -m PyInstaller \
  --noconfirm \
  --clean \
  --name "$APP_NAME" \
  --windowed \
  --hidden-import PySide6.QtCore \
  --hidden-import PySide6.QtGui \
  --hidden-import PySide6.QtWidgets \
  --distpath "$PYI_ROOT/dist" \
  --workpath "$PYI_ROOT/build" \
  --specpath "$PYI_ROOT/spec" \
  "$ROOT_DIR/keylight_tray.py"

APP_BIN_DIR="$PYI_ROOT/dist/$APP_NAME"
if [ ! -d "$APP_BIN_DIR" ]; then
  echo "PyInstaller output fehlt: $APP_BIN_DIR"
  exit 1
fi

PORTABLE_DIR="$PORTABLE_ROOT/${APP_TITLE// /-}-$VERSION-$APPIMAGE_ARCH-portable"
rm -rf "$PORTABLE_DIR"
mkdir -p "$PORTABLE_DIR/app"
cp -a "$APP_BIN_DIR/." "$PORTABLE_DIR/app/"
cp "$ROOT_DIR/assets/$APP_NAME.svg" "$PORTABLE_DIR/$APP_NAME.svg"

cat > "$PORTABLE_DIR/run.sh" << 'PORTABLERUN'
#!/usr/bin/env sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export ELGATO_TRAY_MINIMIZE_ON_CLOSE=1
exec "$BASE/app/elgato-keylight-tray" "$@"
PORTABLERUN
chmod +x "$PORTABLE_DIR/run.sh"

cat > "$PORTABLE_DIR/START_ELGATO_KEY_LIGHT_TRAY.desktop" << 'PORTABLEDESKTOP'
[Desktop Entry]
Type=Application
Version=1.0
Name=Start Elgato Key Light Tray
Comment=Portable Start ohne Installation
Exec=sh -c 'BASE="$(dirname "$(readlink -f "$1")")"; exec sh "$BASE/run.sh"' sh %k
Icon=elgato-keylight-tray
Terminal=false
Categories=Utility;
StartupNotify=true
PORTABLEDESKTOP
chmod +x "$PORTABLE_DIR/START_ELGATO_KEY_LIGHT_TRAY.desktop"

PORTABLE_TAR="$DIST_DIR/${APP_TITLE// /-}-$VERSION-$APPIMAGE_ARCH-portable.tar.gz"
rm -f "$PORTABLE_TAR"
tar -C "$PORTABLE_ROOT" -czf "$PORTABLE_TAR" "$(basename "$PORTABLE_DIR")"

mkdir -p "$APPDIR/usr/lib/$APP_NAME"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"

cp -a "$APP_BIN_DIR/." "$APPDIR/usr/lib/$APP_NAME/"

cat > "$APPDIR/usr/bin/$APP_NAME" << 'LAUNCHER'
#!/usr/bin/env sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export ELGATO_TRAY_MINIMIZE_ON_CLOSE=1
exec "$HERE/../lib/elgato-keylight-tray/elgato-keylight-tray" "$@"
LAUNCHER
chmod +x "$APPDIR/usr/bin/$APP_NAME"

cat > "$APPDIR/AppRun" << 'APPRUN'
#!/usr/bin/env sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export ELGATO_TRAY_MINIMIZE_ON_CLOSE=1
exec "$HERE/usr/bin/elgato-keylight-tray" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

DESKTOP_FILE="$APPDIR/usr/share/applications/$APP_NAME.desktop"
cat > "$DESKTOP_FILE" << DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_TITLE
Comment=Steuerung fur Elgato Key Lights im System Tray
Exec=$APP_NAME
Icon=$APP_NAME
Terminal=false
Categories=Utility;
StartupNotify=false
DESKTOP
cp "$DESKTOP_FILE" "$APPDIR/$APP_NAME.desktop"

cp "$ROOT_DIR/assets/$APP_NAME.svg" "$APPDIR/$APP_NAME.svg"
cp "$ROOT_DIR/assets/$APP_NAME.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP_NAME.svg"
ln -sf "$APP_NAME.svg" "$APPDIR/.DirIcon"

APPIMAGETOOL="$BUILD_ROOT/appimagetool-$APPIMAGE_ARCH.AppImage"
if [ ! -x "$APPIMAGETOOL" ]; then
  URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$APPIMAGE_ARCH.AppImage"
  echo "Lade appimagetool von $URL"
  curl -L --fail -o "$APPIMAGETOOL" "$URL"
  chmod +x "$APPIMAGETOOL"
fi

OUTPUT_FILE="$DIST_DIR/${APP_TITLE// /-}-$VERSION-$APPIMAGE_ARCH.AppImage"
ARCH="$APPIMAGE_ARCH" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT_FILE"
chmod +x "$OUTPUT_FILE"

echo "Fertig: $OUTPUT_FILE"
echo "Fertig: $PORTABLE_TAR"
