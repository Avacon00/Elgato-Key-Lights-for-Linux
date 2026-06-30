#!/usr/bin/env python3
"""Elgato Key Light Tray App for Linux."""
from __future__ import annotations

import errno
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
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6 import QtCore, QtGui, QtWidgets

APP_VERSION = "0.2.5"
APP_NAME = "Elgato Key Light Tray"
CONFIG_DIR = Path.home() / ".config" / "elgato-keylight-tray"
DEVICES_FILE = CONFIG_DIR / "devices.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
TRAY_HINT_FILE = CONFIG_DIR / "tray_hint_seen"
LOCK_FILE = CONFIG_DIR / "app.lock"
PID_FILE = CONFIG_DIR / "app.pid"
DEFAULT_PORTS = (9123, 9220)
HTTP_TIMEOUT = float(os.environ.get("ELGATO_HTTP_TIMEOUT", "1.2"))
HTTP_RETRIES = int(os.environ.get("ELGATO_HTTP_RETRIES", "2"))
HTTP_RETRY_DELAY = float(os.environ.get("ELGATO_HTTP_RETRY_DELAY", "0.15"))
MAX_SCAN_HOSTS_PER_NET = 512
MAX_SCAN_WORKERS = int(os.environ.get("ELGATO_SCAN_WORKERS", "28"))
_DEVICE_LOCKS: dict[str, threading.RLock] = {}
_DEVICE_LOCKS_GUARD = threading.Lock()
_INSTANCE_LOCK_HANDLE: Any | None = None


@dataclass
class KeyLightDevice:
    serial: str
    name: str
    ip: str
    port: int = 9123

    @property
    def display_name(self) -> str:
        return f"{(self.name or 'Key Light').strip()} ({(self.serial or 'unknown').strip()})"


@dataclass
class LightState:
    on: bool
    brightness: int
    kelvin: int


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def kelvin_to_mired(kelvin: int) -> int:
    return clamp(round(1_000_000 / clamp(kelvin, 2900, 7000)), 143, 344)


def mired_to_kelvin(mired: int) -> int:
    return clamp(round(1_000_000 / clamp(mired, 143, 344)), 2900, 7000)


def get_device_lock(device: KeyLightDevice) -> threading.RLock:
    key = f"{device.ip}:{device.port}"
    with _DEVICE_LOCKS_GUARD:
        if key not in _DEVICE_LOCKS:
            _DEVICE_LOCKS[key] = threading.RLock()
        return _DEVICE_LOCKS[key]


def request_json(ip: str, endpoint: str, method: str = "GET", payload: dict[str, Any] | None = None,
                 timeout: float = HTTP_TIMEOUT, port: int = 9123, retries: int = HTTP_RETRIES) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            req = Request(
                url=f"http://{ip}:{port}{endpoint}", data=body, method=method,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            with urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="ignore")
                return json.loads(raw) if raw else {}
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(HTTP_RETRY_DELAY * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("HTTP-Anfrage fehlgeschlagen")


def get_accessory_info(ip: str, port: int) -> dict[str, Any] | None:
    try:
        data = request_json(ip, "/elgato/accessory-info", port=port, retries=1)
    except (URLError, TimeoutError, OSError, ValueError):
        return None
    product = str(data.get("productName", ""))
    display = str(data.get("displayName", ""))
    return data if "Key Light" in product or "Key Light" in display else None


def get_light_state(device: KeyLightDevice) -> LightState:
    with get_device_lock(device):
        lights = request_json(device.ip, "/elgato/lights", port=device.port).get("lights") or []
        if not lights:
            raise RuntimeError("Leuchte meldet keinen Zustand")
        light = lights[0]
        return LightState(bool(light.get("on", 0)), clamp(light.get("brightness", 0), 0, 100),
                          mired_to_kelvin(light.get("temperature", 213)))


def set_light_state(device: KeyLightDevice, *, on: bool | None = None, brightness: int | None = None,
                    kelvin: int | None = None) -> LightState:
    with get_device_lock(device):
        lights = request_json(device.ip, "/elgato/lights", port=device.port).get("lights") or []
        if not lights:
            raise RuntimeError("Leuchte meldet keinen Zustand")
        base = lights[0]
        target_on = int(base.get("on", 0)) if on is None else (1 if on else 0)
        target_brightness = clamp(base.get("brightness", 0) if brightness is None else brightness, 0, 100)
        target_temperature = clamp(base.get("temperature", 213) if kelvin is None else kelvin_to_mired(kelvin), 143, 344)
        if (target_on == int(base.get("on", 0))
                and target_brightness == clamp(base.get("brightness", 0), 0, 100)
                and target_temperature == clamp(base.get("temperature", 213), 143, 344)):
            return LightState(bool(target_on), target_brightness, mired_to_kelvin(target_temperature))
        payload = {"numberOfLights": 1, "lights": [{
            "on": target_on, "brightness": target_brightness, "temperature": target_temperature,
        }]}
        request_json(device.ip, "/elgato/lights", method="PUT", payload=payload, port=device.port)
        return LightState(bool(target_on), target_brightness, mired_to_kelvin(target_temperature))


def parse_local_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    try:
        output = subprocess.check_output(["ip", "-o", "-4", "addr", "show", "scope", "global"],
                                         stderr=subprocess.DEVNULL, text=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        output = ""
    for line in output.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        try:
            iface = ipaddress.ip_interface(parts[parts.index("inet") + 1])
        except (ValueError, IndexError):
            continue
        net = iface.network if iface.network.num_addresses <= MAX_SCAN_HOSTS_PER_NET else ipaddress.ip_network(f"{iface.ip}/24", strict=False)
        if net not in networks:
            networks.append(net)
    if not networks:
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
            if not ipaddress.ip_address(host_ip).is_loopback:
                networks.append(ipaddress.ip_network(f"{host_ip}/24", strict=False))
        except OSError:
            pass
    return networks


def try_probe_host(ip: str) -> KeyLightDevice | None:
    for port in DEFAULT_PORTS:
        info = get_accessory_info(ip, port)
        if info:
            return KeyLightDevice(
                serial=str(info.get("serialNumber") or info.get("serial") or ip),
                name=str(info.get("displayName") or info.get("productName") or "Key Light"),
                ip=ip, port=port,
            )
    return None


def scan_network_for_keylights(saved_devices: list[KeyLightDevice], max_workers: int = MAX_SCAN_WORKERS) -> list[KeyLightDevice]:
    devices: dict[str, KeyLightDevice] = {}
    candidates = {device.ip for device in saved_devices}
    for device in saved_devices:
        found = try_probe_host(device.ip)
        if found:
            devices[found.serial] = found
    for net in parse_local_networks():
        candidates.update(str(host) for host in net.hosts())
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for future in as_completed([pool.submit(try_probe_host, ip) for ip in candidates]):
            try:
                found = future.result()
            except Exception:
                continue
            if found:
                devices[found.serial] = found
    return sorted(devices.values(), key=lambda d: (d.name.lower(), d.serial.lower()))


def load_devices() -> list[KeyLightDevice]:
    if not DEVICES_FILE.exists():
        return []
    try:
        content = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    devices: list[KeyLightDevice] = []
    for item in content:
        try:
            devices.append(KeyLightDevice(str(item["serial"]), str(item.get("name", "Key Light")),
                                          str(item["ip"]), int(item.get("port", 9123))))
        except (KeyError, TypeError, ValueError):
            continue
    return devices


def save_devices(devices: list[KeyLightDevice]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps([asdict(device) for device in devices], indent=2), encoding="utf-8")


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def acquire_instance_lock() -> bool:
    global _INSTANCE_LOCK_HANDLE
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        handle = LOCK_FILE.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0); handle.truncate(); handle.write(f"{os.getpid()}\n"); handle.flush()
        _INSTANCE_LOCK_HANDLE = handle
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
        return True
    except (ImportError, OSError):
        pass
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        return False if exc.errno == errno.EEXIST else False
    _INSTANCE_LOCK_HANDLE = os.fdopen(fd, "w", encoding="utf-8")
    _INSTANCE_LOCK_HANDLE.write(f"{os.getpid()}\n"); _INSTANCE_LOCK_HANDLE.flush()
    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return True


def release_instance_lock() -> None:
    global _INSTANCE_LOCK_HANDLE
    if _INSTANCE_LOCK_HANDLE is not None:
        try:
            _INSTANCE_LOCK_HANDLE.close()
        except OSError:
            pass
        _INSTANCE_LOCK_HANDLE = None
    for path in (PID_FILE, LOCK_FILE):
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            pass


class TaskSignals(QtCore.QObject):
    finished = QtCore.Signal(object, str)


class AsyncWidgetMixin:
    _tasks: list[TaskSignals]

    def run_task(self: QtWidgets.QWidget, work: Callable[[], Any], done: Callable[[Any, str], None]) -> None:
        if not hasattr(self, "_tasks"):
            self._tasks = []
        signals = TaskSignals(self)
        self._tasks.append(signals)

        def on_finished(result: object, error: str) -> None:
            try:
                done(result, error)
            finally:
                if signals in self._tasks:
                    self._tasks.remove(signals)
                signals.deleteLater()
        signals.finished.connect(on_finished)

        def worker() -> None:
            try:
                signals.finished.emit(work(), "")
            except Exception as exc:
                signals.finished.emit(None, str(exc))
        threading.Thread(target=worker, daemon=True).start()


def create_tray_icon() -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(96, 96)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap); painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    gradient = QtGui.QRadialGradient(48, 48, 48)
    gradient.setColorAt(0, QtGui.QColor("#ffd87a")); gradient.setColorAt(1, QtGui.QColor("#7a4e00"))
    painter.setPen(QtGui.QPen(QtGui.QColor("#4e3300"), 4)); painter.setBrush(QtGui.QBrush(gradient)); painter.drawEllipse(12, 12, 72, 72)
    painter.setPen(QtGui.QPen(QtGui.QColor("#fff5c2"), 5))
    for line in ((48, 2, 48, 18), (48, 78, 48, 94), (2, 48, 18, 48), (78, 48, 94, 48)):
        painter.drawLine(*line)
    painter.end()
    return QtGui.QIcon(pixmap)


def apply_app_style(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Noto Sans", 10))
    app.setStyleSheet("""
QWidget#mainWindow { background: #111827; color: #e7ecf8; }
QFrame#card, QGroupBox#deviceCard, QFrame#trayDeviceSection { background: #171f2e; border: 1px solid #2b3a55; border-radius: 12px; }
QLabel#titleLabel { font-size: 20px; font-weight: 700; color: #f4f8ff; }
QLabel#muted { color: #9fb1ce; }
QPushButton { background: #1f2a3d; color: #dce8ff; border: 1px solid #304261; border-radius: 9px; padding: 5px 10px; min-height: 28px; font-weight: 600; }
QPushButton#primaryButton { background: #2f7bff; border-color: #4b94ff; color: white; }
QPushButton#successButton { background: #1b8d65; border-color: #34b487; color: white; }
QPushButton#dangerButton { background: #9b4358; border-color: #c7657e; color: white; }
QPushButton:disabled { background: #4a5d86; color: #b9c6df; }
QLineEdit, QSpinBox { background: #0f1520; color: #e8eefc; border: 1px solid #40506a; border-radius: 7px; padding: 4px 6px; }
QScrollArea { border: none; background: transparent; }
QWidget#scrollContent { background: transparent; }
QMenu, QWidget#trayPanel { background: #101726; color: #e7ecf8; border: 1px solid #2b3a55; border-radius: 10px; }
QSlider::groove:horizontal { height: 8px; background: #2a344a; border-radius: 4px; }
QSlider::sub-page:horizontal { background: #4c8dff; border-radius: 4px; }
QSlider::handle:horizontal { background: #f7fbff; border: 2px solid #5ba1ff; width: 18px; margin: -6px 0; border-radius: 9px; }
""")


def shadow(widget: QtWidgets.QWidget) -> None:
    effect = QtWidgets.QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(24); effect.setOffset(0, 3); effect.setColor(QtGui.QColor(0, 0, 0, 90))
    widget.setGraphicsEffect(effect)


class ToggleSwitch(QtWidgets.QAbstractButton):
    def __init__(self, label: str = "", parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.label = label
        self.progress = 0.0
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(96, 28)
        self.anim = QtCore.QVariantAnimation(self)
        self.anim.setDuration(140)
        self.anim.valueChanged.connect(lambda value: (setattr(self, "progress", float(value)), self.update()))
        self.toggled.connect(self.animate)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(102, 28)

    def animate(self, checked: bool) -> None:
        self.anim.stop(); self.anim.setStartValue(self.progress); self.anim.setEndValue(1.0 if checked else 0.0); self.anim.start()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QtGui.QPainter(self); painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        track = QtCore.QRectF(0, (self.height() - 20) // 2, 44, 20)
        painter.setPen(QtCore.Qt.PenStyle.NoPen); painter.setBrush(QtGui.QColor("#39c384" if self.isChecked() else "#56627a"))
        painter.drawRoundedRect(track, 10, 10)
        painter.setBrush(QtGui.QColor("#f4f8ff")); painter.drawEllipse(QtCore.QRectF(2 + 24 * self.progress, track.y() + 2, 16, 16))
        if self.label:
            painter.setPen(QtGui.QColor("#d9e7ff")); painter.drawText(QtCore.QRectF(52, 0, self.width() - 52, self.height()),
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft, self.label)
        painter.end()


class DeviceCard(QtWidgets.QGroupBox, AsyncWidgetMixin):
    status_message = QtCore.Signal(str)

    def __init__(self, device: KeyLightDevice):
        super().__init__()
        self._tasks: list[TaskSignals] = []
        self.device = device
        self.setTitle(device.display_name); self.setObjectName("deviceCard"); shadow(self)
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(10, 8, 10, 8); root.setSpacing(6)
        meta = QtWidgets.QLabel(f"{device.ip}:{device.port}"); meta.setObjectName("muted"); meta.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(meta)
        top = QtWidgets.QHBoxLayout(); self.toggle = ToggleSwitch("Licht"); top.addWidget(self.toggle); top.addStretch(1)
        self.btn_reload = QtWidgets.QPushButton("Status laden"); self.btn_apply = QtWidgets.QPushButton("Anwenden"); self.btn_apply.setObjectName("primaryButton")
        top.addWidget(self.btn_reload); top.addWidget(self.btn_apply); root.addLayout(top)
        self.brightness_slider, self.brightness_spin = self.add_slider(root, "Helligkeit", 0, 100, "%")
        self.temp_slider, self.temp_spin = self.add_slider(root, "Temperatur", 2900, 7000, "K", 100)
        self.info = QtWidgets.QLabel("-"); self.info.setObjectName("muted"); root.addWidget(self.info)
        self.brightness_slider.valueChanged.connect(self.brightness_spin.setValue); self.brightness_spin.valueChanged.connect(self.brightness_slider.setValue)
        self.temp_slider.valueChanged.connect(self.temp_spin.setValue); self.temp_spin.valueChanged.connect(self.temp_slider.setValue)
        self.btn_reload.clicked.connect(self.refresh_state); self.btn_apply.clicked.connect(self.apply_state)
        QtCore.QTimer.singleShot(0, self.refresh_state)

    def add_slider(self, root: QtWidgets.QVBoxLayout, label: str, low: int, high: int, suffix: str, step: int = 1):
        row = QtWidgets.QHBoxLayout(); row.addWidget(QtWidgets.QLabel(label))
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); slider.setRange(low, high); slider.setSingleStep(step)
        spin = QtWidgets.QSpinBox(); spin.setRange(low, high); spin.setSingleStep(step); spin.setFixedWidth(84 if suffix == "K" else 70)
        row.addWidget(slider, 1); row.addWidget(spin); row.addWidget(QtWidgets.QLabel(suffix)); root.addLayout(row)
        return slider, spin

    def set_busy(self, busy: bool) -> None:
        self.btn_reload.setDisabled(busy); self.btn_apply.setDisabled(busy)

    def set_state(self, state: LightState) -> None:
        self.toggle.setChecked(state.on); self.brightness_slider.setValue(state.brightness); self.temp_slider.setValue(state.kelvin)

    def refresh_state(self) -> None:
        self.set_busy(True); self.info.setText("Status: wird geladen ...")
        def done(result: object, error: str) -> None:
            if error:
                self.info.setText(f"Status: offline ({error})"); self.status_message.emit(f"{self.device.display_name}: nicht erreichbar")
            else:
                self.set_state(result); self.info.setText("Status: online"); self.status_message.emit(f"{self.device.display_name}: Status geladen")
            self.set_busy(False)
        self.run_task(lambda: get_light_state(self.device), done)

    def apply_state(self) -> None:
        self.set_busy(True); self.info.setText("Status: wird gespeichert ...")
        on, brightness, kelvin = self.toggle.isChecked(), self.brightness_slider.value(), self.temp_slider.value()
        def done(result: object, error: str) -> None:
            if error:
                self.info.setText(f"Fehler: {error}"); self.status_message.emit(f"{self.device.display_name}: Fehler beim Senden")
            else:
                self.set_state(result); self.info.setText("Status: gespeichert"); self.status_message.emit(f"{self.device.display_name}: aktualisiert")
            self.set_busy(False)
        self.run_task(lambda: set_light_state(self.device, on=on, brightness=brightness, kelvin=kelvin), done)


class TrayDeviceSection(QtWidgets.QFrame, AsyncWidgetMixin):
    status_message = QtCore.Signal(str)
    state_changed = QtCore.Signal(str)

    def __init__(self, device: KeyLightDevice):
        super().__init__()
        self._tasks: list[TaskSignals] = []
        self.device = device; self.updating = False; self.busy = False; self.pending = False
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.apply_state)
        self.setObjectName("trayDeviceSection")
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(10, 9, 10, 9); root.setSpacing(7)
        head = QtWidgets.QHBoxLayout(); name = QtWidgets.QLabel(device.name or "Key Light"); self.toggle = ToggleSwitch("AN / AUS")
        head.addWidget(name, 1); head.addWidget(self.toggle); root.addLayout(head)
        meta = QtWidgets.QLabel(f"{device.ip}:{device.port}"); meta.setObjectName("muted"); root.addWidget(meta)
        self.brightness_slider, self.brightness_value = self.add_row(root, "Helligkeit", 0, 100, "%")
        self.temp_slider, self.temp_value = self.add_row(root, "Temperatur", 2900, 7000, "K")
        self.toggle.toggled.connect(self.apply_state)
        self.brightness_slider.valueChanged.connect(lambda value: self.changed(self.brightness_value, value, "%", self.brightness_slider))
        self.temp_slider.valueChanged.connect(lambda value: self.changed(self.temp_value, value, "K", self.temp_slider))
        self.brightness_slider.sliderReleased.connect(self.release_slider); self.temp_slider.sliderReleased.connect(self.release_slider)
        QtCore.QTimer.singleShot(0, self.refresh_state)

    def add_row(self, root: QtWidgets.QVBoxLayout, label: str, low: int, high: int, suffix: str):
        row = QtWidgets.QHBoxLayout(); row.addWidget(QtWidgets.QLabel(label))
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); slider.setRange(low, high); slider.setSingleStep(100 if suffix == "K" else 1); slider.setPageStep(100 if suffix == "K" else 10)
        value = QtWidgets.QLabel(f"{low}{suffix}"); value.setMinimumWidth(46)
        row.addWidget(slider, 1); row.addWidget(value); root.addLayout(row)
        return slider, value

    def changed(self, label: QtWidgets.QLabel, value: int, suffix: str, slider: QtWidgets.QSlider) -> None:
        label.setText(f"{value}{suffix}")
        if not self.updating:
            self.timer.start(220 if slider.isSliderDown() else 0)

    def release_slider(self) -> None:
        self.timer.stop(); self.apply_state()

    def set_state(self, state: LightState) -> None:
        self.updating = True
        self.toggle.setChecked(state.on); self.brightness_slider.setValue(state.brightness); self.temp_slider.setValue(state.kelvin)
        self.brightness_value.setText(f"{state.brightness}%"); self.temp_value.setText(f"{state.kelvin}K")
        self.updating = False

    def refresh_state(self) -> None:
        self.timer.stop()
        def done(result: object, error: str) -> None:
            if error:
                self.status_message.emit(f"{self.device.display_name}: Tray-Fehler ({error})")
            else:
                self.set_state(result); self.status_message.emit(f"{self.device.display_name}: Tray-Status geladen")
        self.run_task(lambda: get_light_state(self.device), done)

    def apply_state(self) -> None:
        if self.updating:
            return
        if self.busy:
            self.pending = True; return
        self.timer.stop(); self.busy = True
        on, brightness, kelvin = self.toggle.isChecked(), self.brightness_slider.value(), self.temp_slider.value()
        def done(result: object, error: str) -> None:
            if error:
                self.status_message.emit(f"{self.device.display_name}: Tray-Fehler ({error})")
            else:
                self.set_state(result); self.status_message.emit(f"{self.device.display_name}: Tray aktualisiert"); self.state_changed.emit(self.device.serial)
            self.busy = False
            if self.pending:
                self.pending = False; self.timer.start(0)
        self.run_task(lambda: set_light_state(self.device, on=on, brightness=brightness, kelvin=kelvin), done)


class TrayControlPanel(QtWidgets.QWidget):
    request_reload = QtCore.Signal(); request_set_all = QtCore.Signal(bool); request_show_main = QtCore.Signal(); request_toggle_minimize = QtCore.Signal(bool); request_quit = QtCore.Signal()
    status_message = QtCore.Signal(str); device_updated = QtCore.Signal(str)

    def __init__(self):
        super().__init__(None, QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("trayPanel"); self.setWindowTitle("Tray Control"); self.setMinimumWidth(360); self.setMaximumWidth(440); self.device_sections: list[TrayDeviceSection] = []
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(12, 12, 12, 10); root.setSpacing(7)
        root.addWidget(QtWidgets.QLabel("Tray Schnellsteuerung")); root.addWidget(QtWidgets.QLabel("Alle Lampen direkt steuern, ohne Untermenüs."))
        row = QtWidgets.QHBoxLayout(); row.addWidget(QtWidgets.QLabel("Alle Lampen"), 1); self.toggle_all = ToggleSwitch("AN / AUS"); self.toggle_all.toggled.connect(self.request_set_all.emit); row.addWidget(self.toggle_all); root.addLayout(row)
        self.btn_reload = QtWidgets.QPushButton("Reload alle Lichter"); self.btn_reload.clicked.connect(self.request_reload.emit); root.addWidget(self.btn_reload)
        self.chk_minimize = QtWidgets.QCheckBox("X minimiert ins Tray"); self.chk_minimize.toggled.connect(self.request_toggle_minimize.emit); root.addWidget(self.chk_minimize)
        actions = QtWidgets.QHBoxLayout(); self.btn_main = QtWidgets.QPushButton("Fenster"); self.btn_quit = QtWidgets.QPushButton("Beenden"); self.btn_quit.setObjectName("dangerButton")
        self.btn_main.clicked.connect(self.request_show_main.emit); self.btn_quit.clicked.connect(self.request_quit.emit); actions.addWidget(self.btn_main); actions.addWidget(self.btn_quit); root.addLayout(actions)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_content = QtWidgets.QWidget(); self.scroll_content.setObjectName("scrollContent"); self.layout_sections = QtWidgets.QVBoxLayout(self.scroll_content); self.layout_sections.setContentsMargins(0, 0, 0, 0); self.layout_sections.setSpacing(8)
        self.scroll.setWidget(self.scroll_content); root.addWidget(self.scroll, 1)

    def set_minimize_on_close(self, enabled: bool) -> None:
        self.chk_minimize.blockSignals(True); self.chk_minimize.setChecked(enabled); self.chk_minimize.blockSignals(False)

    def set_devices(self, devices: list[KeyLightDevice]) -> None:
        while self.layout_sections.count():
            item = self.layout_sections.takeAt(0); widget = item.widget()
            if widget: widget.deleteLater()
        self.device_sections = []
        if not devices:
            self.layout_sections.addWidget(QtWidgets.QLabel("Keine Lampen gespeichert")); self.adjustSize(); return
        for device in devices:
            section = TrayDeviceSection(device); section.status_message.connect(self.status_message.emit); section.state_changed.connect(self.device_updated.emit)
            self.layout_sections.addWidget(section); self.device_sections.append(section)
        self.layout_sections.addStretch(1); self.adjustSize()

    def show_near_cursor(self) -> None:
        self.adjustSize(); pos = QtGui.QCursor.pos(); screen = QtGui.QGuiApplication.screenAt(pos) or QtGui.QGuiApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry(); self.move(clamp(pos.x() - self.width() + 24, available.left() + 8, available.right() - self.width() - 8), clamp(pos.y() + 10, available.top() + 8, available.bottom() - self.height() - 8))
        self.show(); self.raise_(); self.activateWindow()

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:  # noqa: N802
        super().focusOutEvent(event); QtCore.QTimer.singleShot(0, self.hide)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide(); return
        super().keyPressEvent(event)


class MainWindow(QtWidgets.QWidget, AsyncWidgetMixin):
    def __init__(self, tray_enabled: bool, app_icon: QtGui.QIcon):
        super().__init__(); self._tasks: list[TaskSignals] = []
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}"); self.resize(680, 500); self.setMinimumSize(620, 430); self.setObjectName("mainWindow"); self.setWindowIcon(app_icon)
        self.tray_enabled = tray_enabled; self.devices = load_devices(); self.settings = load_settings(); self.device_cards: list[DeviceCard] = []; self.tray_icon = None; self.tray_panel = None; self._quitting = False; self._scan_running = False; self._last_tray_sync = 0.0
        self.minimize_on_close = bool(self.settings.get("minimize_on_close", os.environ.get("ELGATO_TRAY_MINIMIZE_ON_CLOSE", "0") == "1"))
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(10, 10, 10, 8); root.setSpacing(8)
        header = self.card(); h = QtWidgets.QVBoxLayout(header); title = QtWidgets.QLabel("Elgato Key Light Control"); title.setObjectName("titleLabel"); subtitle = QtWidgets.QLabel(f"Version {APP_VERSION} · Netzwerk-Scan, Tray-Steuerung und direkte Presets pro Lampe."); subtitle.setObjectName("muted"); subtitle.setWordWrap(True); h.addWidget(title); h.addWidget(subtitle); root.addWidget(header)
        toolbar = self.card(); top = QtWidgets.QHBoxLayout(toolbar); self.btn_discover = QtWidgets.QPushButton("Reload (Netzwerk-Scan)"); self.btn_all_on = QtWidgets.QPushButton("Alle AN"); self.btn_all_off = QtWidgets.QPushButton("Alle AUS"); self.btn_all_on.setObjectName("successButton"); self.btn_all_off.setObjectName("dangerButton"); top.addWidget(self.btn_discover); top.addStretch(1); top.addWidget(self.btn_all_on); top.addWidget(self.btn_all_off); root.addWidget(toolbar)
        add_frame = self.card(); add = QtWidgets.QHBoxLayout(add_frame); add.addWidget(QtWidgets.QLabel("IP manuell hinzufügen:")); self.manual_ip = QtWidgets.QLineEdit(); self.manual_ip.setPlaceholderText("z. B. 192.168.178.45"); self.manual_add_btn = QtWidgets.QPushButton("Hinzufügen"); self.manual_add_btn.setObjectName("primaryButton"); self.btn_tray_hint = QtWidgets.QPushButton("Tray-Hinweis"); add.addWidget(self.manual_ip, 1); add.addWidget(self.manual_add_btn); add.addWidget(self.btn_tray_hint); root.addWidget(add_frame)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll_content = QtWidgets.QWidget(); self.scroll_content.setObjectName("scrollContent"); self.scroll_layout = QtWidgets.QVBoxLayout(self.scroll_content); self.scroll_layout.setContentsMargins(4, 4, 4, 4); self.scroll_layout.setSpacing(8); self.scroll.setWidget(self.scroll_content); root.addWidget(self.scroll, 1)
        self.status_label = QtWidgets.QLabel("Bereit"); root.addWidget(self.status_label)
        self.btn_discover.clicked.connect(self.reload_devices); self.btn_all_on.clicked.connect(lambda: self.set_all_devices(True)); self.btn_all_off.clicked.connect(lambda: self.set_all_devices(False)); self.manual_add_btn.clicked.connect(self.add_manual_ip); self.btn_tray_hint.clicked.connect(lambda: self.show_tray_hint(True))
        if tray_enabled:
            self.tray_panel = TrayControlPanel(); self.tray_panel.request_reload.connect(self.reload_devices); self.tray_panel.request_set_all.connect(self.set_all_devices); self.tray_panel.request_show_main.connect(self.show_main_window); self.tray_panel.request_toggle_minimize.connect(self.set_minimize_on_close); self.tray_panel.request_quit.connect(self.quit_app); self.tray_panel.status_message.connect(self.log_status); self.tray_panel.device_updated.connect(self.refresh_device_card); self.tray_panel.set_minimize_on_close(self.minimize_on_close); self.tray_panel.set_devices(self.devices)
            self.tray_icon = QtWidgets.QSystemTrayIcon(app_icon, self); self.tray_icon.setToolTip(f"{APP_NAME} v{APP_VERSION}"); self.tray_icon.activated.connect(self.on_tray_activated); self.rebuild_tray_menu(); self.tray_icon.show()
        else:
            self.log_status("System-Tray nicht verfügbar. App läuft als normales Fenster."); QtCore.QTimer.singleShot(900, lambda: self.show_tray_hint(False))
        self.refresh_cards(); QtCore.QTimer.singleShot(450, self.reload_devices)

    def card(self) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame(); frame.setObjectName("card"); shadow(frame); return frame

    def log_status(self, message: str) -> None:
        self.status_label.setText(message)

    def refresh_cards(self) -> None:
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0); widget = item.widget()
            if widget: widget.deleteLater()
        self.device_cards = []
        if not self.devices:
            label = QtWidgets.QLabel("Keine Key Lights gespeichert. Nutze 'Reload' oder füge eine IP manuell hinzu."); label.setWordWrap(True); self.scroll_layout.addWidget(label)
        else:
            for device in self.devices:
                card = DeviceCard(device); card.status_message.connect(self.log_status); self.scroll_layout.addWidget(card); self.device_cards.append(card)
            self.scroll_layout.addStretch(1)
        if self.tray_panel:
            self.tray_panel.set_devices(self.devices); self.sync_tray_toggle_all()
        QtCore.QTimer.singleShot(0, self.auto_resize_for_devices)

    def auto_resize_for_devices(self) -> None:
        if not self.device_cards or self.isMaximized() or self.isFullScreen(): return
        shown = min(len(self.device_cards), 3); height = sum(card.sizeHint().height() for card in self.device_cards[:shown]) + self.scroll_layout.spacing() * max(0, shown - 1) + 20
        missing = height - self.scroll.viewport().height()
        if missing > 0: self.resize(self.width(), min(860, self.height() + missing + 10))

    def set_minimize_on_close(self, enabled: bool) -> None:
        self.minimize_on_close = bool(enabled); self.settings["minimize_on_close"] = self.minimize_on_close; save_settings(self.settings)
        if self.tray_panel: self.tray_panel.set_minimize_on_close(self.minimize_on_close)
        self.log_status("Schließen per X: " + ("minimiert ins Tray" if self.minimize_on_close else "beendet komplett"))

    def show_tray_hint(self, force: bool = False) -> None:
        if not force and (self.tray_enabled or TRAY_HINT_FILE.exists()): return
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", os.environ.get("DESKTOP_SESSION", "")).lower()
        text = "System-Tray wird erkannt." if self.tray_enabled else "Kein System-Tray erkannt. Unter GNOME fehlt oft die Extension 'AppIndicator and KStatusNotifierItem Support'."
        if desktop: text += f"\n\nAktuelle Desktop-Umgebung: {desktop}"
        QtWidgets.QMessageBox.information(self, "Tray-Hinweis", text)
        if not self.tray_enabled:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True); TRAY_HINT_FILE.write_text("seen\n", encoding="utf-8")

    def rebuild_tray_menu(self) -> None:
        if not self.tray_icon: return
        menu = QtWidgets.QMenu(); panel = menu.addAction("Schnellsteuerung öffnen"); panel.triggered.connect(self.toggle_tray_panel); show = menu.addAction("Fenster zeigen"); show.triggered.connect(self.show_main_window); menu.addSeparator(); quit_action = menu.addAction("Beenden"); quit_action.triggered.connect(self.quit_app); self.tray_icon.setContextMenu(menu)

    def refresh_device_card(self, serial: str) -> None:
        for card in self.device_cards:
            if card.device.serial == serial:
                card.refresh_state(); break
        self.sync_tray_toggle_all()

    def sync_tray_toggle_all(self) -> None:
        if not self.tray_panel or not self.tray_panel.device_sections: return
        all_on = all(section.toggle.isChecked() for section in self.tray_panel.device_sections)
        self.tray_panel.toggle_all.blockSignals(True); self.tray_panel.toggle_all.setChecked(all_on); self.tray_panel.toggle_all.blockSignals(False)

    def refresh_tray_panel_states(self, force: bool = False) -> None:
        if not self.tray_panel: return
        now = time.monotonic()
        if not force and now - self._last_tray_sync < 0.8: return
        self._last_tray_sync = now
        for section in self.tray_panel.device_sections: section.refresh_state()
        self.sync_tray_toggle_all()

    def show_main_window(self) -> None:
        self.showNormal(); self.raise_(); self.activateWindow()

    def toggle_tray_panel(self) -> None:
        if not self.tray_panel: self.show_main_window(); return
        if self.tray_panel.isVisible(): self.tray_panel.hide(); return
        self.refresh_tray_panel_states(True); self.tray_panel.show_near_cursor()

    def quit_app(self) -> None:
        self._quitting = True
        if self.tray_panel: self.tray_panel.hide()
        if self.tray_icon: self.tray_icon.hide()
        app = QtWidgets.QApplication.instance()
        if app: app.quit()

    def on_about_to_quit(self) -> None:
        self._quitting = True
        if self.tray_panel: self.tray_panel.hide()
        if self.tray_icon: self.tray_icon.hide()

    def on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QtWidgets.QSystemTrayIcon.ActivationReason.Trigger, QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick}: self.toggle_tray_panel()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self.tray_enabled and not self._quitting and self.minimize_on_close and self.tray_icon and self.tray_icon.isVisible():
            self.hide();
            if self.tray_panel: self.tray_panel.hide()
            self.tray_icon.showMessage(APP_NAME, "App wurde ins Tray minimiert. Zum Beenden: Tray -> Beenden.", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1800)
            event.ignore(); return
        if self.tray_icon: self.tray_icon.hide()
        if self.tray_panel: self.tray_panel.hide()
        super().closeEvent(event)

    def add_manual_ip(self) -> None:
        ip = self.manual_ip.text().strip()
        if not ip: self.log_status("Bitte eine IP eingeben"); return
        try: ipaddress.ip_address(ip)
        except ValueError: self.log_status(f"Ungültige IP-Adresse: {ip}"); return
        self.manual_add_btn.setDisabled(True); self.log_status(f"Prüfe {ip} ...")
        def done(result: object, error: str) -> None:
            self.manual_add_btn.setDisabled(False)
            if error or result is None: self.log_status(f"Kein Key Light unter {ip} gefunden"); return
            found: KeyLightDevice = result
            by_serial = {dev.serial: dev for dev in self.devices}; by_serial[found.serial] = found
            self.devices = sorted(by_serial.values(), key=lambda d: (d.name.lower(), d.serial.lower())); save_devices(self.devices); self.refresh_cards(); self.manual_ip.clear(); self.log_status(f"Hinzugefügt: {found.display_name}")
        self.run_task(lambda: try_probe_host(ip), done)

    def set_all_devices(self, turn_on: bool) -> None:
        if not self.devices: self.log_status("Keine Geräte gespeichert"); return
        self.btn_all_on.setDisabled(True); self.btn_all_off.setDisabled(True); action = "AN" if turn_on else "AUS"; self.log_status(f"Schalte alle {action}...")
        def work() -> int:
            failures = 0
            for device in self.devices:
                try: set_light_state(device, on=turn_on)
                except Exception: failures += 1
            return failures
        def done(result: object, error: str) -> None:
            self.btn_all_on.setDisabled(False); self.btn_all_off.setDisabled(False); failures = len(self.devices) if error else int(result or 0)
            for card in self.device_cards: card.refresh_state()
            if self.tray_panel:
                for section in self.tray_panel.device_sections: section.refresh_state()
                self.tray_panel.toggle_all.blockSignals(True); self.tray_panel.toggle_all.setChecked(turn_on and failures == 0); self.tray_panel.toggle_all.blockSignals(False)
            self.log_status(f"Alle {action}: {failures} Gerät(e) nicht erreichbar" if failures else f"Alle {action}: OK")
        self.run_task(work, done)

    def reload_devices(self) -> None:
        if self._scan_running: self.log_status("Scan läuft bereits..."); return
        self._scan_running = True; self.btn_discover.setDisabled(True); self.log_status("Suche Key Lights im Netzwerk..."); saved = list(self.devices)
        def done(result: object, error: str) -> None:
            self._scan_running = False; self.btn_discover.setDisabled(False)
            if error: self.log_status(f"Scan-Fehler: {error}"); return
            devices = result or []
            if not devices: self.log_status("Keine Key Lights gefunden"); return
            self.devices = devices; save_devices(self.devices); self.refresh_cards(); self.log_status(f"Gefunden und gespeichert: {len(self.devices)} Gerät(e)")
        self.run_task(lambda: scan_network_for_keylights(saved), done)


def main() -> int:
    app = QtWidgets.QApplication([]); app.setApplicationName(APP_NAME); app.setApplicationVersion(APP_VERSION); apply_app_style(app)
    icon = create_tray_icon(); app.setWindowIcon(icon)
    if not acquire_instance_lock():
        print(f"{APP_NAME} läuft bereits. Bitte das bestehende Tray-Icon nutzen.", flush=True); return 0
    tray_enabled = QtWidgets.QSystemTrayIcon.isSystemTrayAvailable(); app.setQuitOnLastWindowClosed(not tray_enabled)
    window = MainWindow(tray_enabled, icon); app.aboutToQuit.connect(window.on_about_to_quit); app.aboutToQuit.connect(release_instance_lock); window.show()
    try: return app.exec()
    finally: release_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
