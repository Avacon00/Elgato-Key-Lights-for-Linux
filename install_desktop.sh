#!/usr/bin/env sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_NAME="elgato-keylight-tray"
DESKTOP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"
AUTOSTART_FILE="$AUTOSTART_DIR/$APP_NAME.desktop"

mkdir -p "$DESKTOP_DIR" "$AUTOSTART_DIR"

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Elgato Key Light Tray
Comment=Steuerung fur Elgato Key Lights im System Tray
Exec=sh -c 'cd "\$1" && exec sh "./run_desktop.sh"' sh "$APP_DIR"
Icon=preferences-system
Terminal=false
Categories=Utility;
StartupNotify=false
DESKTOP

cp "$DESKTOP_FILE" "$AUTOSTART_FILE"
chmod +x "$DESKTOP_FILE" "$AUTOSTART_FILE"

echo "Installiert: $DESKTOP_FILE"
echo "Autostart:  $AUTOSTART_FILE"
