#!/usr/bin/env python3
"""Elgato Key Light Tray App for Linux.

Features:
- System tray integration (Taskleiste)
- Device discovery in local network
- Save and reload discovered devices
- Per-device on/off, brightness, and temperature controls
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6 import QtCore, QtGui, QtWidgets

CONFIG_DIR = Path.home() / ".config" / "elgato-keylight-tray"
DEVICES_FILE = CONFIG_DIR / "devices.json"
TRAY_HINT_FILE = CONFIG_DIR / "tray_hint_seen"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PID_FILE = CONFIG_DIR / "app.pid"
DEFAULT_PORTS = (9123, 9220)
HTTP_TIMEOUT = float(os.environ.get("ELGATO_HTTP_TIMEOUT", "1.2"))
HTTP_RETRIES = int(os.environ.get("ELGATO_HTTP_RETRIES", "2"))
HTTP_RETRY_DELAY = float(os.environ.get("ELGATO_HTTP_RETRY_DELAY", "0.15"))
MAX_SCAN_HOSTS_PER_NET = 512
MAX_SCAN_WORKERS = int(os.environ.get("ELGATO_SCAN_WORKERS", "28"))

_DEVICE_LOCKS: dict[str, threading.RLock] = {}
_DEVICE_LOCKS_GUARD = threading.Lock()


@dataclass
class KeyLightDevice:
    serial: str
    name: str
    ip: str
    port: int = 9123

    @property
    def display_name(self) -> str:
        name = self.name.strip() if self.name else "Key Light"
        serial = self.serial.strip() if self.serial else "unknown"
        return f"{name} ({serial})"


@dataclass
class LightState:
    on: bool
    brightness: int
    kelvin: int


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def kelvin_to_mired(kelvin: int) -> int:
    kelvin = clamp(kelvin, 2900, 7000)
    return clamp(round(1_000_000 / kelvin), 143, 344)


def mired_to_kelvin(mired: int) -> int:
    mired = clamp(mired, 143, 344)
    return clamp(round(1_000_000 / mired), 2900, 7000)


def get_device_lock(device: KeyLightDevice) -> threading.RLock:
    key = f"{device.ip}:{device.port}"
    with _DEVICE_LOCKS_GUARD:
        lock = _DEVICE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DEVICE_LOCKS[key] = lock
    return lock


def request_json(
    ip: str,
    endpoint: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT,
    port: int = 9123,
    retries: int = HTTP_RETRIES,
) -> dict[str, Any]:
    url = f"http://{ip}:{port}{endpoint}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            req = Request(
                url=url,
                data=body,
                method=method,
                headers={
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
            )
            with urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="ignore")
                if not raw:
                    return {}
                return json.loads(raw)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(HTTP_RETRY_DELAY * (attempt + 1))

    if last_error is not None:
        raise last_error
    raise RuntimeError("HTTP Anfrage fehlgeschlagen")


def get_accessory_info(ip: str, port: int) -> dict[str, Any] | None:
    try:
        data = request_json(ip, "/elgato/accessory-info", port=port, retries=1)
    except (URLError, TimeoutError, OSError, ValueError):
        return None

    product = str(data.get("productName", ""))
    display = str(data.get("displayName", ""))
    if "Key Light" not in product and "Key Light" not in display:
        return None
    return data


def get_light_state(device: KeyLightDevice) -> LightState:
    with get_device_lock(device):
        data = request_json(device.ip, "/elgato/lights", port=device.port)
        lights = data.get("lights") or []
        if not lights:
            raise RuntimeError("Leuchte meldet keinen Zustand")
        light = lights[0]
        return LightState(
            on=bool(light.get("on", 0)),
            brightness=clamp(light.get("brightness", 0), 0, 100),
            kelvin=mired_to_kelvin(light.get("temperature", 213)),
        )


def set_light_state(
    device: KeyLightDevice,
    *,
    on: bool | None = None,
    brightness: int | None = None,
    kelvin: int | None = None,
) -> LightState:
    with get_device_lock(device):
        current = request_json(device.ip, "/elgato/lights", port=device.port)
        lights = current.get("lights") or []
        if not lights:
            raise RuntimeError("Leuchte meldet keinen Zustand")

        base = lights[0]
        target_on = int(base.get("on", 0))
        target_brightness = clamp(base.get("brightness", 0), 0, 100)
        target_temperature = clamp(base.get("temperature", 213), 143, 344)

        if on is not None:
            target_on = 1 if on else 0
        if brightness is not None:
            target_brightness = clamp(brightness, 0, 100)
        if kelvin is not None:
            target_temperature = kelvin_to_mired(kelvin)

        current_on = int(base.get("on", 0))
        current_brightness = clamp(base.get("brightness", 0), 0, 100)
        current_temperature = clamp(base.get("temperature", 213), 143, 344)

        # Avoid sending redundant updates; this reduces network chatter and flicker.
        if (
            target_on == current_on
            and target_brightness == current_brightness
            and target_temperature == current_temperature
        ):
            return LightState(
                on=bool(target_on),
                brightness=target_brightness,
                kelvin=mired_to_kelvin(target_temperature),
            )

        payload = {
            "numberOfLights": 1,
            "lights": [
                {
                    "on": target_on,
                    "brightness": target_brightness,
                    "temperature": target_temperature,
                }
            ],
        }

        request_json(
            device.ip,
            "/elgato/lights",
            method="PUT",
            payload=payload,
            port=device.port,
        )
        return LightState(
            on=bool(target_on),
            brightness=target_brightness,
            kelvin=mired_to_kelvin(target_temperature),
        )


def parse_local_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    try:
        output = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        output = ""

    for line in output.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        idx = parts.index("inet")
        if idx + 1 >= len(parts):
            continue
        cidr = parts[idx + 1]
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            continue

        net = iface.network
        if net.num_addresses > MAX_SCAN_HOSTS_PER_NET:
            net = ipaddress.ip_network(f"{iface.ip}/24", strict=False)
        if net not in networks:
            networks.append(net)

    if not networks:
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
            ip_obj = ipaddress.ip_address(host_ip)
            if not ip_obj.is_loopback:
                networks.append(ipaddress.ip_network(f"{host_ip}/24", strict=False))
        except OSError:
            pass

    return networks


def try_probe_host(ip: str) -> KeyLightDevice | None:
    for port in DEFAULT_PORTS:
        info = get_accessory_info(ip, port)
        if not info:
            continue

        serial = str(info.get("serialNumber") or info.get("serial") or ip)
        name = str(info.get("displayName") or info.get("productName") or "Key Light")
        return KeyLightDevice(serial=serial, name=name, ip=ip, port=port)
    return None


def scan_network_for_keylights(
    saved_devices: list[KeyLightDevice],
    max_workers: int = MAX_SCAN_WORKERS,
) -> list[KeyLightDevice]:
    devices: dict[str, KeyLightDevice] = {}

    # Probe saved devices first so known IPs remain fast and stable.
    for saved in saved_devices:
        found = try_probe_host(saved.ip)
        if found:
            devices[found.serial] = found

    networks = parse_local_networks()
    candidates: set[str] = set()

    for net in networks:
        for host in net.hosts():
            candidates.add(str(host))

    for saved in saved_devices:
        candidates.add(saved.ip)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(try_probe_host, ip) for ip in candidates]
        for future in as_completed(futures):
            result = future.result()
            if result:
                devices[result.serial] = result

    return sorted(devices.values(), key=lambda d: (d.name.lower(), d.serial.lower()))


def load_devices() -> list[KeyLightDevice]:
    if not DEVICES_FILE.exists():
        return []
    try:
        content = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    loaded: list[KeyLightDevice] = []
    for item in content:
        try:
            loaded.append(
                KeyLightDevice(
                    serial=str(item["serial"]),
                    name=str(item.get("name", "Key Light")),
                    ip=str(item["ip"]),
                    port=int(item.get("port", 9123)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return loaded


def save_devices(devices: list[KeyLightDevice]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = [asdict(device) for device in devices]
    DEVICES_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def detect_desktop_environment() -> str:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").strip()
    if not desktop:
        desktop = os.environ.get("DESKTOP_SESSION", "").strip()
    return desktop.lower()


def tray_hint_needed(tray_enabled: bool) -> bool:
    if tray_enabled:
        return False
    return not TRAY_HINT_FILE.exists()


def mark_tray_hint_seen() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TRAY_HINT_FILE.write_text("seen\n", encoding="utf-8")


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_settings(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_instance_lock() -> bool:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    legacy_lock = CONFIG_DIR / "app.lock"
    if legacy_lock.exists():
        try:
            legacy_lock.unlink()
        except OSError:
            pass

    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            existing_pid = 0
        if process_is_running(existing_pid):
            return False
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def release_instance_lock() -> None:
    if not PID_FILE.exists():
        return
    try:
        existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        existing_pid = 0
    if existing_pid and existing_pid != os.getpid():
        return
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def create_tray_icon() -> QtGui.QIcon:
    size = 96
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

    gradient = QtGui.QRadialGradient(size / 2, size / 2, size / 2)
    gradient.setColorAt(0.0, QtGui.QColor("#ffd87a"))
    gradient.setColorAt(0.7, QtGui.QColor("#efb84d"))
    gradient.setColorAt(1.0, QtGui.QColor("#7a4e00"))

    painter.setPen(QtGui.QPen(QtGui.QColor("#4e3300"), 4))
    painter.setBrush(QtGui.QBrush(gradient))
    painter.drawEllipse(12, 12, 72, 72)

    painter.setPen(QtGui.QPen(QtGui.QColor("#fff5c2"), 5, QtCore.Qt.PenStyle.SolidLine))
    painter.drawLine(48, 2, 48, 18)
    painter.drawLine(48, 78, 48, 94)
    painter.drawLine(2, 48, 18, 48)
    painter.drawLine(78, 48, 94, 48)

    painter.end()
    return QtGui.QIcon(pixmap)


def apply_app_style(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Noto Sans", 10))
    app.setStyleSheet(
        """
QWidget#mainWindow {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #0d1118, stop: 0.58 #131a26, stop: 1 #1a2231
    );
    color: #e7ecf8;
}
QFrame#headerCard, QFrame#toolbarCard {
    background: rgba(23, 31, 46, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
}
QLabel#titleLabel {
    font-size: 20px;
    font-weight: 700;
    color: #f4f8ff;
}
QLabel#subtitleLabel {
    font-size: 12px;
    color: #9fb1ce;
}
QLabel#statusBarLabel {
    background: rgba(25, 34, 50, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 6px 10px;
    color: #d7e3fb;
}
QGroupBox#deviceCard {
    background: rgba(23, 31, 46, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 12px;
    margin-top: 11px;
    padding-top: 10px;
    font-weight: 700;
    font-size: 13px;
    color: #eef4ff;
}
QGroupBox#deviceCard::title {
    left: 10px;
    top: 3px;
    color: #f4f8ff;
}
QLabel#deviceMetaLabel, QLabel#deviceStatusLabel {
    color: #95a9c9;
    font-size: 11px;
}
QPushButton {
    background: #1f2a3d;
    color: #dce8ff;
    border: 1px solid #304261;
    border-radius: 9px;
    padding: 5px 10px;
    font-weight: 600;
    min-height: 28px;
}
QPushButton:hover {
    background: #26344c;
}
QPushButton:pressed {
    background: #304261;
}
QPushButton#primaryButton {
    background: #2f7bff;
    border: 1px solid #4b94ff;
    color: white;
}
QPushButton#primaryButton:hover {
    background: #276de6;
}
QPushButton#primaryButton:pressed {
    background: #215ec7;
}
QPushButton#successButton {
    background: #1b8d65;
    border: 1px solid #34b487;
    color: white;
}
QPushButton#successButton:hover {
    background: #167a57;
}
QPushButton#successButton:pressed {
    background: #126647;
}
QPushButton#dangerButton {
    background: #9b4358;
    border: 1px solid #c7657e;
    color: white;
}
QPushButton#dangerButton:hover {
    background: #83384b;
}
QPushButton#dangerButton:pressed {
    background: #6d2f3e;
}
QPushButton:disabled {
    background: #4a5d86;
    border: 1px solid #4a5d86;
    color: #b9c6df;
}
QLineEdit, QSpinBox {
    background: #0f1520;
    color: #e8eefc;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 7px;
    padding: 4px 6px;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 14px;
}
QCheckBox {
    spacing: 6px;
    color: #dbe7ff;
}
QScrollArea {
    border: none;
    background: transparent;
}
QWidget#scrollContent {
    background: transparent;
}
QSlider::groove:horizontal {
    height: 8px;
    background: #2a344a;
    border-radius: 4px;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4c8dff, stop:1 #5db0ff);
    border-radius: 4px;
}
QSlider::handle:horizontal {
    background: #f7fbff;
    border: 2px solid #5ba1ff;
    width: 18px;
    margin: -6px 0;
    border-radius: 9px;
}
QMenu {
    background: #101726;
    color: #e7ecf8;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    border-radius: 7px;
    padding: 6px 10px;
}
QMenu::item:selected {
    background: #2e7bff;
    color: white;
}
QWidget#trayPanel {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #0f1928, stop: 1 #10182b
    );
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 16px;
}
QLabel#trayPanelTitle {
    font-size: 16px;
    font-weight: 700;
    color: #f3f8ff;
}
QLabel#trayPanelHint {
    font-size: 11px;
    color: #96abcf;
}
QPushButton#trayPanelButton {
    background: #22314a;
    border: 1px solid #36517c;
    border-radius: 10px;
    color: #e5efff;
    min-height: 30px;
    padding: 5px 12px;
}
QPushButton#trayPanelButton:hover {
    background: #2a3b57;
}
QPushButton#trayPanelDangerButton {
    background: #8f3f52;
    border: 1px solid #b75d72;
    border-radius: 10px;
    color: white;
    min-height: 30px;
    padding: 5px 12px;
}
QPushButton#trayPanelDangerButton:hover {
    background: #793547;
}
QCheckBox#trayPanelCheck {
    font-size: 11px;
    color: #cad8ef;
    spacing: 7px;
}
QFrame#trayDeviceSection {
    background: rgba(28, 40, 61, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 13px;
}
QFrame#trayDeviceSection:hover {
    border: 1px solid rgba(92, 156, 255, 0.6);
}
QLabel#trayDeviceName {
    font-size: 12px;
    font-weight: 700;
    color: #f2f7ff;
}
QLabel#trayDeviceMeta {
    font-size: 11px;
    color: #9ab0d1;
}
QLabel#trayLabel {
    font-size: 11px;
    color: #c2d1ea;
}
QLabel#traySliderValue {
    font-size: 11px;
    color: #d8e6ff;
    min-width: 46px;
}
QWidget#trayPanel QSlider::groove:horizontal {
    height: 7px;
    background: #2b3850;
    border-radius: 4px;
}
QWidget#trayPanel QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4a84ff, stop:1 #6fa9ff);
    border-radius: 4px;
}
QWidget#trayPanel QSlider::handle:horizontal {
    background: #f7fbff;
    border: 2px solid #5e9cff;
    width: 16px;
    margin: -5px 0;
    border-radius: 8px;
}
"""
    )


def themed_icon(name: str, fallback: QtWidgets.QStyle.StandardPixmap) -> QtGui.QIcon:
    icon = QtGui.QIcon.fromTheme(name)
    if icon.isNull():
        icon = QtWidgets.QApplication.style().standardIcon(fallback)
    return icon


def apply_card_shadow(widget: QtWidgets.QWidget) -> None:
    shadow = QtWidgets.QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(24)
    shadow.setOffset(0, 3)
    shadow.setColor(QtGui.QColor(0, 0, 0, 90))
    widget.setGraphicsEffect(shadow)


class ToggleSwitch(QtWidgets.QAbstractButton):
    def __init__(self, label: str = "", parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._knob_progress = 0.0
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setMinimumSize(96, 28)

        self._animation = QtCore.QVariantAnimation(self)
        self._animation.setDuration(140)
        self._animation.valueChanged.connect(self._on_animation_value)
        self.toggled.connect(self._animate_toggle)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(102, 28)

    def setLabel(self, label: str) -> None:
        self._label = label
        self.update()

    def _on_animation_value(self, value: object) -> None:
        try:
            self._knob_progress = float(value)
        except (TypeError, ValueError):
            self._knob_progress = 0.0
        self.update()

    def _animate_toggle(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._knob_progress)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        track_w = 44
        track_h = 20
        track_x = 0
        track_y = (self.height() - track_h) // 2
        track = QtCore.QRectF(track_x, track_y, track_w, track_h)

        on_color = QtGui.QColor("#39c384")
        off_color = QtGui.QColor("#56627a")
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(on_color if self.isChecked() else off_color)
        painter.drawRoundedRect(track, track_h / 2, track_h / 2)

        knob_d = 16
        travel = track_w - knob_d - 4
        knob_x = track_x + 2 + travel * self._knob_progress
        knob_y = track_y + (track_h - knob_d) / 2
        painter.setBrush(QtGui.QColor("#f4f8ff"))
        painter.drawEllipse(QtCore.QRectF(knob_x, knob_y, knob_d, knob_d))

        if self._label:
            text_rect = QtCore.QRectF(track_w + 8, 0, self.width() - track_w - 8, self.height())
            painter.setPen(QtGui.QColor("#d9e7ff"))
            painter.drawText(
                text_rect,
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft,
                self._label,
            )

        painter.end()


class DeviceCard(QtWidgets.QGroupBox):
    status_message = QtCore.Signal(str)

    def __init__(self, device: KeyLightDevice, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.device = device
        self.setTitle(device.display_name)
        self.setObjectName("deviceCard")
        apply_card_shadow(self)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        self.location_label = QtWidgets.QLabel(f"{device.ip}:{device.port}")
        self.location_label.setObjectName("deviceMetaLabel")
        self.location_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.location_label)

        row_toggle = QtWidgets.QHBoxLayout()
        row_toggle.setSpacing(6)
        self.on_checkbox = ToggleSwitch("Licht")
        row_toggle.addWidget(self.on_checkbox)
        row_toggle.addStretch(1)

        self.btn_reload = QtWidgets.QPushButton("Status laden")
        self.btn_apply = QtWidgets.QPushButton("Anwenden")
        self.btn_reload.setObjectName("secondaryButton")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_reload.setIcon(
            themed_icon("view-refresh", QtWidgets.QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.btn_apply.setIcon(
            themed_icon("document-save", QtWidgets.QStyle.StandardPixmap.SP_DialogApplyButton)
        )
        row_toggle.addWidget(self.btn_reload)
        row_toggle.addWidget(self.btn_apply)
        root.addLayout(row_toggle)

        bright_layout = QtWidgets.QHBoxLayout()
        bright_layout.addWidget(QtWidgets.QLabel("Helligkeit"))
        self.brightness_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(0, 100)
        self.brightness_spin = QtWidgets.QSpinBox()
        self.brightness_spin.setRange(0, 100)
        self.brightness_spin.setFixedWidth(70)
        bright_layout.addWidget(self.brightness_slider, 1)
        bright_layout.addWidget(self.brightness_spin)
        bright_layout.addWidget(QtWidgets.QLabel("%"))
        root.addLayout(bright_layout)

        temp_layout = QtWidgets.QHBoxLayout()
        temp_layout.addWidget(QtWidgets.QLabel("Temperatur"))
        self.temp_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.temp_slider.setRange(2900, 7000)
        self.temp_slider.setSingleStep(100)
        self.temp_spin = QtWidgets.QSpinBox()
        self.temp_spin.setRange(2900, 7000)
        self.temp_spin.setSingleStep(100)
        self.temp_spin.setFixedWidth(84)
        temp_layout.addWidget(self.temp_slider, 1)
        temp_layout.addWidget(self.temp_spin)
        temp_layout.addWidget(QtWidgets.QLabel("K"))
        root.addLayout(temp_layout)

        self.info_label = QtWidgets.QLabel("-")
        self.info_label.setObjectName("deviceStatusLabel")
        root.addWidget(self.info_label)

        self.brightness_slider.valueChanged.connect(self.brightness_spin.setValue)
        self.brightness_spin.valueChanged.connect(self.brightness_slider.setValue)
        self.temp_slider.valueChanged.connect(self.temp_spin.setValue)
        self.temp_spin.valueChanged.connect(self.temp_slider.setValue)

        self.btn_reload.clicked.connect(self.refresh_state)
        self.btn_apply.clicked.connect(self.apply_state)

        self.refresh_state()

    def set_busy(self, busy: bool) -> None:
        self.btn_apply.setDisabled(busy)
        self.btn_reload.setDisabled(busy)

    def refresh_state(self) -> None:
        self.set_busy(True)
        try:
            state = get_light_state(self.device)
            self.on_checkbox.setChecked(state.on)
            self.brightness_slider.setValue(state.brightness)
            self.temp_slider.setValue(state.kelvin)
            self.info_label.setText("Status: online")
            self.status_message.emit(f"{self.device.display_name}: Status geladen")
        except Exception as exc:
            self.info_label.setText(f"Status: offline ({exc})")
            self.status_message.emit(f"{self.device.display_name}: nicht erreichbar")
        finally:
            self.set_busy(False)

    def apply_state(self) -> None:
        self.set_busy(True)
        try:
            state = set_light_state(
                self.device,
                on=self.on_checkbox.isChecked(),
                brightness=self.brightness_slider.value(),
                kelvin=self.temp_slider.value(),
            )
            self.on_checkbox.setChecked(state.on)
            self.brightness_slider.setValue(state.brightness)
            self.temp_slider.setValue(state.kelvin)
            self.info_label.setText("Status: gespeichert")
            self.status_message.emit(f"{self.device.display_name}: aktualisiert")
        except Exception as exc:
            self.info_label.setText(f"Fehler: {exc}")
            self.status_message.emit(f"{self.device.display_name}: Fehler beim Senden")
        finally:
            self.set_busy(False)


class TrayDeviceSection(QtWidgets.QFrame):
    status_message = QtCore.Signal(str)
    state_changed = QtCore.Signal(str)

    def __init__(self, device: KeyLightDevice, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.device = device
        self._updating_ui = False
        self._apply_timer = QtCore.QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.timeout.connect(self.apply_state)

        self.setObjectName("trayDeviceSection")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 9, 10, 9)
        root.setSpacing(7)

        header = QtWidgets.QHBoxLayout()
        self.name_label = QtWidgets.QLabel(device.name or "Key Light")
        self.name_label.setObjectName("trayDeviceName")
        self.toggle = ToggleSwitch("AN / AUS")
        header.addWidget(self.name_label, 1)
        header.addWidget(self.toggle)
        root.addLayout(header)

        self.meta_label = QtWidgets.QLabel(f"{device.ip}:{device.port}")
        self.meta_label.setObjectName("trayDeviceMeta")
        root.addWidget(self.meta_label)

        bright_row = QtWidgets.QHBoxLayout()
        bright_row.setSpacing(6)
        bright_label = QtWidgets.QLabel("Helligkeit")
        bright_label.setObjectName("trayLabel")
        self.brightness_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(1, 100)
        self.brightness_value = QtWidgets.QLabel("1%")
        self.brightness_value.setObjectName("traySliderValue")
        bright_row.addWidget(bright_label)
        bright_row.addWidget(self.brightness_slider, 1)
        bright_row.addWidget(self.brightness_value)
        root.addLayout(bright_row)

        temp_row = QtWidgets.QHBoxLayout()
        temp_row.setSpacing(6)
        temp_label = QtWidgets.QLabel("Temperatur")
        temp_label.setObjectName("trayLabel")
        self.temp_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.temp_slider.setRange(2900, 7000)
        self.temp_slider.setSingleStep(100)
        self.temp_slider.setPageStep(100)
        self.temp_value = QtWidgets.QLabel("2900K")
        self.temp_value.setObjectName("traySliderValue")
        temp_row.addWidget(temp_label)
        temp_row.addWidget(self.temp_slider, 1)
        temp_row.addWidget(self.temp_value)
        root.addLayout(temp_row)

        self.toggle.toggled.connect(self.apply_state)
        self.brightness_slider.valueChanged.connect(self.on_brightness_changed)
        self.temp_slider.valueChanged.connect(self.on_temp_changed)
        self.brightness_slider.sliderReleased.connect(self.on_slider_released)
        self.temp_slider.sliderReleased.connect(self.on_slider_released)

        self.refresh_state()

    def on_brightness_changed(self, value: int) -> None:
        self.brightness_value.setText(f"{value}%")
        if self._updating_ui:
            return
        if self.brightness_slider.isSliderDown():
            self.schedule_apply(220)
            return
        self.schedule_apply(0)

    def on_temp_changed(self, value: int) -> None:
        self.temp_value.setText(f"{value}K")
        if self._updating_ui:
            return
        if self.temp_slider.isSliderDown():
            self.schedule_apply(220)
            return
        self.schedule_apply(0)

    def on_slider_released(self) -> None:
        self._apply_timer.stop()
        self.apply_state()

    def schedule_apply(self, delay_ms: int) -> None:
        if self._updating_ui:
            return
        self._apply_timer.start(max(0, int(delay_ms)))

    def refresh_state(self) -> None:
        self._apply_timer.stop()
        try:
            state = get_light_state(self.device)
            self._updating_ui = True
            self.toggle.setChecked(state.on)
            self.brightness_slider.setValue(max(1, state.brightness or 1))
            self.temp_slider.setValue(state.kelvin)
            self.brightness_value.setText(f"{max(1, state.brightness or 1)}%")
            self.temp_value.setText(f"{state.kelvin}K")
            self.status_message.emit(f"{self.device.display_name}: Tray-Status geladen")
        except Exception as exc:
            self.status_message.emit(f"{self.device.display_name}: Tray-Fehler ({exc})")
        finally:
            self._updating_ui = False

    def apply_state(self) -> None:
        if self._updating_ui:
            return
        self._apply_timer.stop()
        try:
            state = set_light_state(
                self.device,
                on=self.toggle.isChecked(),
                brightness=self.brightness_slider.value(),
                kelvin=self.temp_slider.value(),
            )
            self._updating_ui = True
            self.toggle.setChecked(state.on)
            self.brightness_slider.setValue(max(1, state.brightness or 1))
            self.temp_slider.setValue(state.kelvin)
            self.brightness_value.setText(f"{max(1, state.brightness or 1)}%")
            self.temp_value.setText(f"{state.kelvin}K")
            self.status_message.emit(f"{self.device.display_name}: Tray aktualisiert")
            self.state_changed.emit(self.device.serial)
        except Exception as exc:
            self.status_message.emit(f"{self.device.display_name}: Tray-Fehler ({exc})")
        finally:
            self._updating_ui = False


class TrayControlPanel(QtWidgets.QWidget):
    request_reload = QtCore.Signal()
    request_set_all = QtCore.Signal(bool)
    request_show_main = QtCore.Signal()
    request_toggle_minimize = QtCore.Signal(bool)
    request_quit = QtCore.Signal()
    status_message = QtCore.Signal(str)
    device_updated = QtCore.Signal(str)

    def __init__(self, embedded: bool = False):
        self.embedded = embedded
        window_flags = QtCore.Qt.WindowType.Widget
        if not embedded:
            window_flags = (
                QtCore.Qt.WindowType.Tool
                | QtCore.Qt.WindowType.FramelessWindowHint
                | QtCore.Qt.WindowType.WindowStaysOnTopHint
            )
        super().__init__(None, window_flags)
        self.setObjectName("trayPanel")
        self.setWindowTitle("Tray Control")
        self.setMinimumWidth(360)
        self.setMaximumWidth(440)
        self.device_sections: list[TrayDeviceSection] = []

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(7)

        title = QtWidgets.QLabel("Tray Schnellsteuerung")
        title.setObjectName("trayPanelTitle")
        root.addWidget(title)

        hint = QtWidgets.QLabel("Alle Lampen direkt steuern, ohne Untermenus.")
        hint.setObjectName("trayPanelHint")
        root.addWidget(hint)

        all_row = QtWidgets.QHBoxLayout()
        all_row.setSpacing(6)
        all_label = QtWidgets.QLabel("Alle Lampen")
        all_label.setObjectName("trayLabel")
        self.toggle_all = ToggleSwitch("AN / AUS")
        self.toggle_all.toggled.connect(self.request_set_all.emit)
        all_row.addWidget(all_label, 1)
        all_row.addWidget(self.toggle_all)
        root.addLayout(all_row)

        self.btn_reload = QtWidgets.QPushButton("Reload alle Lichter")
        self.btn_reload.setObjectName("trayPanelButton")
        self.btn_reload.setIcon(
            themed_icon("view-refresh", QtWidgets.QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.btn_reload.clicked.connect(self.request_reload.emit)
        root.addWidget(self.btn_reload)

        options = QtWidgets.QHBoxLayout()
        options.setSpacing(6)
        self.chk_minimize = QtWidgets.QCheckBox("X minimiert ins Tray")
        self.chk_minimize.setObjectName("trayPanelCheck")
        self.chk_minimize.toggled.connect(self.request_toggle_minimize.emit)
        options.addWidget(self.chk_minimize, 1)
        root.addLayout(options)

        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(6)
        self.btn_main = QtWidgets.QPushButton("Fenster")
        self.btn_main.setObjectName("trayPanelButton")
        self.btn_main.setIcon(
            themed_icon("window-new", QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
        )
        self.btn_main.clicked.connect(self.request_show_main.emit)
        self.btn_quit = QtWidgets.QPushButton("Beenden")
        self.btn_quit.setObjectName("trayPanelDangerButton")
        self.btn_quit.setIcon(
            themed_icon("application-exit", QtWidgets.QStyle.StandardPixmap.SP_DialogCloseButton)
        )
        self.btn_quit.clicked.connect(self.request_quit.emit)
        action_row.addWidget(self.btn_main)
        action_row.addWidget(self.btn_quit)
        root.addLayout(action_row)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_content = QtWidgets.QWidget()
        self.scroll_content.setObjectName("scrollContent")
        self.sections_layout = QtWidgets.QVBoxLayout(self.scroll_content)
        self.sections_layout.setContentsMargins(0, 0, 0, 0)
        self.sections_layout.setSpacing(8)
        self.scroll.setWidget(self.scroll_content)
        root.addWidget(self.scroll, 1)

    def set_minimize_on_close(self, enabled: bool) -> None:
        self.chk_minimize.blockSignals(True)
        self.chk_minimize.setChecked(enabled)
        self.chk_minimize.blockSignals(False)

    def set_devices(self, devices: list[KeyLightDevice]) -> None:
        while self.sections_layout.count():
            item = self.sections_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.device_sections = []

        if not devices:
            empty = QtWidgets.QLabel("Keine Lampen gespeichert")
            empty.setObjectName("trayLabel")
            self.sections_layout.addWidget(empty)
            self.adjustSize()
            return

        for device in devices:
            section = TrayDeviceSection(device)
            section.status_message.connect(self.status_message.emit)
            section.state_changed.connect(self.device_updated.emit)
            self.sections_layout.addWidget(section)
            self.device_sections.append(section)

        self.sections_layout.addStretch(1)
        self.adjustSize()

    def show_near_cursor(self) -> None:
        if self.embedded:
            return
        self.adjustSize()
        pos = QtGui.QCursor.pos()
        screen = QtGui.QGuiApplication.screenAt(pos) or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            self.show()
            self.raise_()
            self.activateWindow()
            return

        available = screen.availableGeometry()
        x = clamp(pos.x() - self.width() + 24, available.left() + 8, available.right() - self.width() - 8)
        y = clamp(pos.y() + 10, available.top() + 8, available.bottom() - self.height() - 8)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:  # noqa: N802
        super().focusOutEvent(event)
        if not self.embedded:
            QtCore.QTimer.singleShot(0, self.hide)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if not self.embedded and event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


class MainWindow(QtWidgets.QWidget):
    discovery_done = QtCore.Signal(list, str)

    def __init__(self, tray_enabled: bool, app_icon: QtGui.QIcon):
        super().__init__()
        self.setWindowTitle("Elgato Key Light Tray")
        self.resize(680, 500)
        self.setMinimumSize(620, 430)
        self.setObjectName("mainWindow")
        self.setWindowIcon(app_icon)
        self.tray_enabled = tray_enabled

        self.devices: list[KeyLightDevice] = load_devices()
        self.settings = load_settings()
        minimize_default = os.environ.get("ELGATO_TRAY_MINIMIZE_ON_CLOSE", "0") == "1"
        self.minimize_on_close = bool(
            self.settings.get("minimize_on_close", minimize_default)
        )
        self.device_cards: list[DeviceCard] = []
        self.discovery_thread: threading.Thread | None = None
        self.tray_icon: QtWidgets.QSystemTrayIcon | None = None
        self.tray_menu: QtWidgets.QMenu | None = None
        self.tray_panel: TrayControlPanel | None = None
        self._quitting = False
        self._last_tray_sync = 0.0

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        header = QtWidgets.QFrame()
        header.setObjectName("headerCard")
        header_layout = QtWidgets.QVBoxLayout(header)
        header_layout.setContentsMargins(12, 10, 12, 10)
        header_layout.setSpacing(2)
        title = QtWidgets.QLabel("Elgato Key Light Control")
        title.setObjectName("titleLabel")
        subtitle = QtWidgets.QLabel(
            "Automatischer Start-Scan, Tray-Steuerung und direkte Presets pro Lampe."
        )
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        apply_card_shadow(header)
        root.addWidget(header)

        toolbar = QtWidgets.QFrame()
        toolbar.setObjectName("toolbarCard")
        top = QtWidgets.QHBoxLayout(toolbar)
        top.setContentsMargins(10, 8, 10, 8)
        top.setSpacing(6)
        self.btn_discover = QtWidgets.QPushButton("Reload (Netzwerk-Scan)")
        self.btn_all_on = QtWidgets.QPushButton("Alle AN")
        self.btn_all_off = QtWidgets.QPushButton("Alle AUS")
        self.btn_discover.setObjectName("secondaryButton")
        self.btn_all_on.setObjectName("successButton")
        self.btn_all_off.setObjectName("dangerButton")
        self.btn_discover.setIcon(
            themed_icon("view-refresh", QtWidgets.QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.btn_all_on.setIcon(
            themed_icon("media-playback-start", QtWidgets.QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.btn_all_off.setIcon(
            themed_icon("media-playback-stop", QtWidgets.QStyle.StandardPixmap.SP_MediaStop)
        )
        top.addWidget(self.btn_discover)
        top.addStretch(1)
        top.addWidget(self.btn_all_on)
        top.addWidget(self.btn_all_off)
        apply_card_shadow(toolbar)
        root.addWidget(toolbar)

        add_frame = QtWidgets.QFrame()
        add_frame.setObjectName("toolbarCard")
        add_row = QtWidgets.QHBoxLayout(add_frame)
        add_row.setContentsMargins(10, 8, 10, 8)
        add_row.setSpacing(6)
        add_row.addWidget(QtWidgets.QLabel("IP manuell hinzufugen:"))
        self.manual_ip = QtWidgets.QLineEdit()
        self.manual_ip.setPlaceholderText("z.B. 192.168.178.45")
        self.manual_add_btn = QtWidgets.QPushButton("Hinzufugen")
        self.manual_add_btn.setObjectName("primaryButton")
        self.manual_add_btn.setIcon(
            themed_icon("list-add", QtWidgets.QStyle.StandardPixmap.SP_FileDialogNewFolder)
        )
        self.btn_tray_hint = QtWidgets.QPushButton("Tray-Hinweis")
        self.btn_tray_hint.setObjectName("secondaryButton")
        self.btn_tray_hint.setIcon(
            themed_icon(
                "dialog-information",
                QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation,
            )
        )
        add_row.addWidget(self.manual_ip, 1)
        add_row.addWidget(self.manual_add_btn)
        add_row.addWidget(self.btn_tray_hint)
        apply_card_shadow(add_frame)
        root.addWidget(add_frame)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll_content = QtWidgets.QWidget()
        self.scroll_content.setObjectName("scrollContent")
        self.scroll_layout = QtWidgets.QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(4, 4, 4, 4)
        self.scroll_layout.setSpacing(8)
        self.scroll.setWidget(self.scroll_content)
        root.addWidget(self.scroll, 1)

        self.status_label = QtWidgets.QLabel("Bereit")
        self.status_label.setObjectName("statusBarLabel")
        root.addWidget(self.status_label)

        self.btn_discover.clicked.connect(self.reload_devices)
        self.btn_all_on.clicked.connect(lambda: self.set_all_devices(True))
        self.btn_all_off.clicked.connect(lambda: self.set_all_devices(False))
        self.manual_add_btn.clicked.connect(self.add_manual_ip)
        self.btn_tray_hint.clicked.connect(lambda: self.show_tray_hint(force=True))
        self.discovery_done.connect(self.on_discovery_done)

        if self.tray_enabled:
            self.tray_panel = TrayControlPanel(embedded=False)
            self.tray_panel.request_reload.connect(self.reload_devices)
            self.tray_panel.request_set_all.connect(self.set_all_devices)
            self.tray_panel.request_show_main.connect(self.show_main_window)
            self.tray_panel.request_toggle_minimize.connect(self.set_minimize_on_close)
            self.tray_panel.request_quit.connect(self.quit_app)
            self.tray_panel.status_message.connect(self.log_status)
            self.tray_panel.device_updated.connect(self.refresh_device_card)
            self.tray_panel.set_minimize_on_close(self.minimize_on_close)
            self.tray_panel.set_devices(self.devices)

            self.tray_icon = QtWidgets.QSystemTrayIcon(app_icon, self)
            self.tray_icon.setToolTip("Elgato Key Light Tray")
            self.tray_icon.activated.connect(self.on_tray_activated)
            self.rebuild_tray_menu()
            self.tray_icon.show()
        else:
            self.log_status(
                "System-Tray nicht verfugbar. App lauft als normales Fenster "
                "(GNOME braucht oft AppIndicator-Extension)."
            )
            QtCore.QTimer.singleShot(900, self.show_tray_hint_once)

        self.refresh_cards()
        QtCore.QTimer.singleShot(450, self.reload_devices)

    def log_status(self, message: str) -> None:
        self.status_label.setText(message)

    def refresh_cards(self) -> None:
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.device_cards = []

        if not self.devices:
            placeholder = QtWidgets.QLabel(
                "Keine Key Lights gespeichert. Nutze 'Reload' oder IP manuell hinzufugen."
            )
            placeholder.setWordWrap(True)
            self.scroll_layout.addWidget(placeholder)
            if self.tray_panel is not None:
                self.tray_panel.set_devices(self.devices)
                self.sync_tray_toggle_all()
            return

        for device in self.devices:
            card = DeviceCard(device)
            card.status_message.connect(self.log_status)
            self.scroll_layout.addWidget(card)
            self.device_cards.append(card)

        self.scroll_layout.addStretch(1)
        if self.tray_panel is not None:
            self.tray_panel.set_devices(self.devices)
            self.sync_tray_toggle_all()
        self.schedule_auto_resize()

    def schedule_auto_resize(self) -> None:
        QtCore.QTimer.singleShot(0, self.auto_resize_for_devices)

    def auto_resize_for_devices(self) -> None:
        if not self.device_cards or self.isMaximized() or self.isFullScreen():
            return

        cards_to_show = min(len(self.device_cards), 3)
        margins = self.scroll_layout.contentsMargins()
        cards_height = sum(card.sizeHint().height() for card in self.device_cards[:cards_to_show])
        spacing = self.scroll_layout.spacing() * max(0, cards_to_show - 1)
        desired_content_height = cards_height + spacing + margins.top() + margins.bottom() + 8

        viewport_height = self.scroll.viewport().height()
        missing_height = desired_content_height - viewport_height
        if missing_height <= 0:
            return

        target_height = min(860, self.height() + missing_height + 10)
        if target_height > self.height():
            self.resize(self.width(), target_height)

    def set_minimize_on_close(self, enabled: bool) -> None:
        self.minimize_on_close = bool(enabled)
        self.settings["minimize_on_close"] = self.minimize_on_close
        save_settings(self.settings)
        if self.tray_panel is not None:
            self.tray_panel.set_minimize_on_close(self.minimize_on_close)
        mode = "minimiert ins Tray" if self.minimize_on_close else "beendet komplett"
        self.log_status(f"SchlieBen per X: {mode}")

    def build_tray_hint_text(self) -> str:
        desktop = detect_desktop_environment()
        if self.tray_enabled:
            text = (
                "System-Tray wird erkannt.\n\n"
                "Falls das Icon trotzdem fehlt, melde dich kurz ab/an oder pruefe,\n"
                "ob die Desktop-Umgebung Tray-Icons ausblendet."
            )
            if desktop:
                text += f"\n\nAktuelle Desktop-Umgebung: {desktop}"
            return text

        text = (
            "Kein System-Tray erkannt. Die App lauft als normales Fenster weiter.\n\n"
            "Hinweis: Unter GNOME fehlt oft die Extension\n"
            "'AppIndicator and KStatusNotifierItem Support'."
        )
        if desktop and "gnome" not in desktop:
            text = (
                "Kein System-Tray erkannt. Die App lauft als normales Fenster weiter.\n\n"
                f"Aktuelle Desktop-Umgebung: {desktop}"
            )
        return text

    def show_tray_hint_once(self) -> None:
        if not tray_hint_needed(self.tray_enabled):
            return
        self.show_tray_hint(force=False)

    def show_tray_hint(self, force: bool = False) -> None:
        if not force and not tray_hint_needed(self.tray_enabled):
            return

        QtWidgets.QMessageBox.information(
            self,
            "Tray nicht verfugbar",
            self.build_tray_hint_text(),
        )
        if not self.tray_enabled:
            mark_tray_hint_seen()

    def rebuild_tray_menu(self) -> None:
        if not self.tray_enabled or self.tray_icon is None:
            return
        if self.tray_menu is not None:
            self.tray_icon.setContextMenu(self.tray_menu)
            return

        tray_menu = QtWidgets.QMenu()
        action_panel = tray_menu.addAction("Schnellsteuerung offnen")
        action_panel.setIcon(
            themed_icon("preferences-system", QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView)
        )
        action_panel.triggered.connect(self.toggle_tray_panel)

        action_show = tray_menu.addAction("Fenster zeigen")
        action_show.setIcon(
            themed_icon("window-new", QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
        )
        action_show.triggered.connect(self.show_main_window)

        tray_menu.addSeparator()
        action_quit = tray_menu.addAction("Beenden")
        action_quit.setIcon(
            themed_icon("application-exit", QtWidgets.QStyle.StandardPixmap.SP_DialogCloseButton)
        )
        action_quit.triggered.connect(self.quit_app)

        self.tray_menu = tray_menu
        self.tray_icon.setContextMenu(self.tray_menu)

    def refresh_device_card(self, serial: str) -> None:
        for card in self.device_cards:
            if card.device.serial == serial:
                card.refresh_state()
                break
        if self.tray_panel is not None:
            for section in self.tray_panel.device_sections:
                if section.device.serial == serial:
                    section.refresh_state()
                    break
        self.sync_tray_toggle_all()

    def sync_tray_toggle_all(self) -> None:
        if self.tray_panel is None or not self.tray_panel.device_sections:
            return
        all_on = all(section.toggle.isChecked() for section in self.tray_panel.device_sections)
        self.tray_panel.toggle_all.blockSignals(True)
        self.tray_panel.toggle_all.setChecked(all_on)
        self.tray_panel.toggle_all.blockSignals(False)

    def refresh_tray_panel_states(self, force: bool = False) -> None:
        if self.tray_panel is None:
            return
        now = time.monotonic()
        if not force and now - self._last_tray_sync < 0.8:
            return
        self._last_tray_sync = now
        for section in self.tray_panel.device_sections:
            section.refresh_state()
        self.sync_tray_toggle_all()

    def show_main_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle_tray_panel(self) -> None:
        if self.tray_panel is None:
            self.show_main_window()
            return
        if self.tray_panel.isVisible():
            self.tray_panel.hide()
            return
        self.refresh_tray_panel_states(force=True)
        self.tray_panel.show_near_cursor()

    def quit_app(self) -> None:
        self._quitting = True
        if self.tray_panel is not None:
            self.tray_panel.hide()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()

    def on_about_to_quit(self) -> None:
        self._quitting = True
        if self.tray_panel is not None:
            self.tray_panel.hide()
        if self.tray_icon is not None:
            self.tray_icon.hide()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    def on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.toggle_tray_panel()
            return

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if (
            self.tray_enabled
            and not self._quitting
            and self.minimize_on_close
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            self.hide()
            if self.tray_panel is not None and not self.tray_panel.embedded:
                self.tray_panel.hide()
            self.tray_icon.showMessage(
                "Elgato Key Light Tray",
                "App wurde ins Tray minimiert. Zum Beenden: Tray -> Beenden.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                1800,
            )
            event.ignore()
            return
        if self.tray_icon is not None:
            self.tray_icon.hide()
        if self.tray_panel is not None and not self.tray_panel.embedded:
            self.tray_panel.hide()
        super().closeEvent(event)

    def add_manual_ip(self) -> None:
        ip = self.manual_ip.text().strip()
        if not ip:
            self.log_status("Bitte eine IP eingeben")
            return

        self.log_status(f"Prufe {ip} ...")
        found = try_probe_host(ip)
        if not found:
            self.log_status(f"Kein Key Light unter {ip} gefunden")
            return

        by_serial = {dev.serial: dev for dev in self.devices}
        by_serial[found.serial] = found
        self.devices = sorted(by_serial.values(), key=lambda d: (d.name.lower(), d.serial.lower()))
        save_devices(self.devices)
        self.refresh_cards()
        self.manual_ip.clear()
        self.log_status(f"Hinzugefugt: {found.display_name}")

    def set_all_devices(self, turn_on: bool) -> None:
        if not self.devices:
            self.log_status("Keine Gerate gespeichert")
            return

        action = "AN" if turn_on else "AUS"
        self.log_status(f"Schalte alle {action}...")

        failures = 0
        for device in self.devices:
            try:
                set_light_state(device, on=turn_on)
            except Exception:
                failures += 1

        for card in self.device_cards:
            card.refresh_state()
        if self.tray_panel is not None:
            for section in self.tray_panel.device_sections:
                section.refresh_state()
            self.tray_panel.toggle_all.blockSignals(True)
            self.tray_panel.toggle_all.setChecked(turn_on and failures == 0)
            self.tray_panel.toggle_all.blockSignals(False)

        if failures:
            self.log_status(f"Alle {action}: {failures} Gerat(e) nicht erreichbar")
        else:
            self.log_status(f"Alle {action}: OK")

    def reload_devices(self) -> None:
        if self.discovery_thread and self.discovery_thread.is_alive():
            self.log_status("Scan lauft bereits...")
            return

        self.btn_discover.setDisabled(True)
        self.log_status("Suche Key Lights im Netzwerk...")

        def worker() -> None:
            error = ""
            found: list[KeyLightDevice] = []
            try:
                found = scan_network_for_keylights(self.devices)
            except Exception as exc:
                error = str(exc)
            self.discovery_done.emit(found, error)

        self.discovery_thread = threading.Thread(target=worker, daemon=True)
        self.discovery_thread.start()

    @QtCore.Slot(list, str)
    def on_discovery_done(self, devices: list[KeyLightDevice], error: str) -> None:
        self.btn_discover.setDisabled(False)

        if error:
            self.log_status(f"Scan-Fehler: {error}")
            return

        if not devices:
            self.log_status("Keine Key Lights gefunden")
            return

        self.devices = devices
        save_devices(self.devices)
        self.refresh_cards()
        self.log_status(f"Gefunden und gespeichert: {len(self.devices)} Gerat(e)")


def main() -> int:
    app = QtWidgets.QApplication([])
    apply_app_style(app)
    app_icon = create_tray_icon()
    app.setWindowIcon(app_icon)
    if not acquire_instance_lock():
        print(
            "Elgato Key Light Tray lauft bereits. Bitte das bestehende Tray-Icon nutzen.",
            flush=True,
        )
        return 0

    tray_enabled = QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not tray_enabled)

    window = MainWindow(tray_enabled=tray_enabled, app_icon=app_icon)
    app.aboutToQuit.connect(window.on_about_to_quit)
    app.aboutToQuit.connect(release_instance_lock)
    window.show()
    try:
        return app.exec()
    finally:
        release_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
