<div align="center">
  <img src="assets/elgato-keylight-tray.svg" alt="Elgato Key Light Tray Icon" width="120" />
  <h1>Elgato Key Lights for Linux</h1>
  <p>Portable tray app for Elgato Key Lights on Fedora, Ubuntu, Arch and Pop!_OS.</p>
</div>

---

## Deutsch (DE)

### Was ist das?
`Elgato Key Lights for Linux` ist eine einfache Linux-App (App-Name: `Elgato Key Light Tray`), mit der du deine Elgato Key Lights direkt aus dem System-Tray steuern kannst:
- Lampen im Netzwerk automatisch finden
- Pro Lampe: AN/AUS, Helligkeit, Farbtemperatur
- Einstellungen im Tray und im Hauptfenster
- Start-Scan beim App-Start

### Schnellstart fuer Einsteiger

#### Option A: AppImage (empfohlen)
1. Lade aus GitHub Releases: `Elgato-Key-Light-Tray-<version>-x86_64.AppImage`
2. Datei ausfuehrbar machen:
   ```bash
   chmod +x Elgato-Key-Light-Tray-<version>-x86_64.AppImage
   ```
3. Starten:
   ```bash
   ./Elgato-Key-Light-Tray-<version>-x86_64.AppImage
   ```

#### Option B: Portable ohne FUSE
Wenn AppImage nicht startet:
1. Lade: `Elgato-Key-Light-Tray-<version>-x86_64-portable.tar.gz`
2. Entpacken und starten:
   ```bash
   tar -xzf Elgato-Key-Light-Tray-<version>-x86_64-portable.tar.gz
   cd Elgato-Key-Light-Tray-<version>-x86_64-portable
   ./run.sh
   ```

### Projekt lokal starten (aus Source)
```bash
cd /home/marcs/Dokumente/Software
./run.sh
```
Beim ersten Start wird automatisch `.venv` erstellt und `PySide6` installiert.

### Desktop-Integration (Startmenue + Autostart)
```bash
cd /home/marcs/Dokumente/Software
./install_desktop.sh
```
Erzeugt:
- `~/.local/share/applications/elgato-keylight-tray.desktop`
- `~/.config/autostart/elgato-keylight-tray.desktop`

### Bedienung
1. App starten -> automatischer Netzwerk-Scan
2. Tray-Icon klicken -> Schnellsteuerung oeffnen
3. Pro Lampe Helligkeit/Temperatur setzen
4. Slider reagieren sofort (debounced waehrend Ziehen, spaetestens beim Loslassen)

### Voraussetzungen
- Python 3.10+
- Lokales Netzwerk mit Elgato Key Lights

Pakete:
- Ubuntu / Pop!_OS
  ```bash
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip iproute2
  ```
- Fedora
  ```bash
  sudo dnf install -y python3 python3-pip iproute
  ```
- Arch
  ```bash
  sudo pacman -S --needed python python-pip iproute2
  ```

### Fehlerbehebung
- Kein Tray-Icon (haeufig unter GNOME):
  - AppIndicator Extension aktivieren
  - Danach ab-/anmelden
- App startet nicht:
  - `./run.sh` im Terminal ausfuehren
  - Logs pruefen:
    - `~/.cache/elgato-keylight-tray/launcher.log`
    - `~/.cache/elgato-keylight-tray/runtime.log`

---

## English (EN)

### What is this?
`Elgato Key Lights for Linux` is a simple Linux tray app (app name: `Elgato Key Light Tray`) to control Elgato Key Lights:
- Auto-discovery in your local network
- Per light: ON/OFF, brightness, color temperature
- Control from tray popup and main window
- Auto scan on startup

### Beginner Quick Start

#### Option A: AppImage (recommended)
1. Download from GitHub Releases: `Elgato-Key-Light-Tray-<version>-x86_64.AppImage`
2. Make it executable:
   ```bash
   chmod +x Elgato-Key-Light-Tray-<version>-x86_64.AppImage
   ```
3. Run it:
   ```bash
   ./Elgato-Key-Light-Tray-<version>-x86_64.AppImage
   ```

#### Option B: Portable (no FUSE)
If AppImage does not start:
1. Download: `Elgato-Key-Light-Tray-<version>-x86_64-portable.tar.gz`
2. Extract and run:
   ```bash
   tar -xzf Elgato-Key-Light-Tray-<version>-x86_64-portable.tar.gz
   cd Elgato-Key-Light-Tray-<version>-x86_64-portable
   ./run.sh
   ```

### Run from source
```bash
cd /home/marcs/Dokumente/Software
./run.sh
```
On first run, a local `.venv` is created and `PySide6` is installed.

### Desktop integration (menu + autostart)
```bash
cd /home/marcs/Dokumente/Software
./install_desktop.sh
```
Creates:
- `~/.local/share/applications/elgato-keylight-tray.desktop`
- `~/.config/autostart/elgato-keylight-tray.desktop`

### Usage
1. Start app -> automatic network scan
2. Click tray icon -> open quick control
3. Adjust brightness/temperature per light
4. Sliders apply live (debounced while dragging, guaranteed on release)

### Requirements
- Python 3.10+
- Local network access to Elgato Key Lights

Packages:
- Ubuntu / Pop!_OS
  ```bash
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip iproute2
  ```
- Fedora
  ```bash
  sudo dnf install -y python3 python3-pip iproute
  ```
- Arch
  ```bash
  sudo pacman -S --needed python python-pip iproute2
  ```

### Troubleshooting
- No tray icon (common on GNOME):
  - enable AppIndicator extension
  - log out/log in again
- App does not start:
  - run `./run.sh` from terminal
  - check logs:
    - `~/.cache/elgato-keylight-tray/launcher.log`
    - `~/.cache/elgato-keylight-tray/runtime.log`

---

## Maintainer Notes

Build AppImage + portable tarball:
```bash
scripts/build_appimage.sh 1.0.0
```
Output:
- `dist/Elgato-Key-Light-Tray-1.0.0-x86_64.AppImage`
- `dist/Elgato-Key-Light-Tray-1.0.0-x86_64-portable.tar.gz`

Key files:
- `keylight_tray.py`
- `scripts/build_appimage.sh`
- `run.sh`
- `run_desktop.sh`
- `install_desktop.sh`
- `.github/workflows/release-appimage.yml`
