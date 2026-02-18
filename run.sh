#!/usr/bin/env sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="$APP_DIR/.venv"
MARKER="$VENV_DIR/.deps_installed"
PY="$VENV_DIR/bin/python"
LOG_DIR="$HOME/.cache/elgato-keylight-tray"
RUNTIME_LOG="$LOG_DIR/runtime.log"

if [ ! -x "$PY" ]; then
  python3 -m venv "$VENV_DIR"
fi

# Some distros create a venv without pip by default.
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  echo "Fehler: pip fehlt in $VENV_DIR."
  echo "Installiere ggf. python3-venv/python3-pip und starte erneut."
  exit 1
fi

if [ ! -f "$MARKER" ] || [ "$APP_DIR/requirements.txt" -nt "$MARKER" ]; then
  "$PY" -m pip install --upgrade pip >/dev/null
  "$PY" -m pip install -r "$APP_DIR/requirements.txt"
  date > "$MARKER"
fi

# If started from an interactive terminal, detach so closing the terminal
# does not kill the GUI. Set ELGATO_TRAY_FOREGROUND=1 to keep foreground mode.
if [ "${ELGATO_TRAY_FOREGROUND:-0}" != "1" ] && [ -t 1 ]; then
  mkdir -p "$LOG_DIR"
  nohup "$PY" "$APP_DIR/keylight_tray.py" >> "$RUNTIME_LOG" 2>&1 &
  echo "Elgato Key Light Tray gestartet (PID $!). Log: $RUNTIME_LOG"
  exit 0
fi

exec "$PY" "$APP_DIR/keylight_tray.py"
