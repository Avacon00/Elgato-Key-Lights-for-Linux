#!/usr/bin/env sh
set -u

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOG_DIR="$HOME/.cache/elgato-keylight-tray"
LOG_FILE="$LOG_DIR/launcher.log"

mkdir -p "$LOG_DIR"

show_info() {
  msg="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --title="Elgato Key Light Tray" --text="$msg" >/dev/null 2>&1 || true
    return
  fi
  if command -v kdialog >/dev/null 2>&1; then
    kdialog --title "Elgato Key Light Tray" --msgbox "$msg" >/dev/null 2>&1 || true
    return
  fi
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "Elgato Key Light Tray" "$msg" >/dev/null 2>&1 || true
  fi
}

show_error() {
  msg="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Elgato Key Light Tray" --text="$msg" >/dev/null 2>&1 || true
    return
  fi
  if command -v kdialog >/dev/null 2>&1; then
    kdialog --title "Elgato Key Light Tray" --error "$msg" >/dev/null 2>&1 || true
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    xmessage -center "$msg" >/dev/null 2>&1 || true
    return
  fi
  printf '%s\n' "$msg" >&2
}

{
  printf '[%s] One-click setup start\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  sh "$APP_DIR/install_desktop.sh"
} >> "$LOG_FILE" 2>&1
status=$?

if [ "$status" -ne 0 ]; then
  show_error "Setup fehlgeschlagen.\nLog: $LOG_FILE"
  exit "$status"
fi

show_info "Desktop-Eintrag installiert. Die App wird jetzt gestartet."
exec sh "$APP_DIR/run_desktop.sh"
