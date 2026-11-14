# picast — Trixie Edition

![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)
![OS](https://img.shields.io/badge/Raspberry%20Pi%20OS-Trixie%20(64--bit)-C51A4A.svg)
![C++](https://img.shields.io/badge/C%2B%2B-26%20(GCC%2014)-00599C.svg)
![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)
![Targets](https://img.shields.io/badge/Targets-Pi%20Zero%202%20W%20%7C%20Pi%204%20%7C%20Pi%205-2ea44f.svg)

A modernized, low-latency **Miracast / Wi-Fi Display (`WIN + K`)** and **MICE (Miracast over Infrastructure)** receiver for **Raspberry Pi OS Trixie**. Split cleanly into a **C++26 native media engine** (`app/` + `modules/`) and a **strictly typed Python 3.13+ control service & CLI** (`service/`), built together via root CMake into a single installable `.deb` prioritizing **Raspberry Pi Zero 2 W** performance and **Windows 10/11** compatibility. *(Successor fork of `homeworkc/lazycast`.)*

---

## Features

- **Trixie Native Media Engine (`C++26` + `GStreamer 1.0` + `DRM/KMS`):** Replaces legacy Bullseye OpenMAX IL (`/opt/vc`) with zero-copy `v4l2h264dec` → `kmssink` on Pi Zero 2 W / Pi 4, multi-threaded NEON `avdec_h264` fallback on Pi 5, and optional WFD 2.0 `v4l2slh265dec` HEVC on Pi 4/5.
- **Modular Zero-Overhead C++26 Architecture:** Independent CMake targets (`app/`, `modules/platform_probe/`, `modules/rtp_jitter/`, `modules/media_pipeline/`) each with their own `CMakeLists.txt` and `test/` suite, linked statically with Release LTO + `--gc-sections` so there is zero runtime size or performance penalty.
- **Deterministic RTP Reordering:** Pre-allocated, zero-hot-path-allocation circular `RtpJitterBuffer` with wraparound-safe sequence ordering and automatic IDR keyframe recovery.
- **First-Class `Wi-Fi Direct` & `MICE`:** Supports Wi-Fi Direct (`p2p-dev-wlan0` GO + single-IP `dnsmasq`), Miracast over Infrastructure (`_display._tcp` mDNS + MS-MICE `:7250`), or **both** side-by-side under an exclusive Session Lock.
- **High-Legibility Color-Block Splash:** Claims `tty1` / `/dev/fb0` with oversized color-block typography for the device name, active transport badges, and **per-reboot** (or per-session / fixed / PBC) 8-digit WPS PIN.
- **Exclusive Low-Resource Priority Mode:** Configures CPU/IO priority (`Nice=-10`), disables Wi-Fi power saving, suppresses background Wi-Fi scans, and claims exclusive TTY/DRM ownership (without desktop/Wayland bloat), while letting `picast service disable` restore graphical boot anytime.
- **Always-On KMS HDMI Option:** Safely configures `vc4.force_hotplug=1`, `video=HDMI-A-1:1920x1080@60D`, and captured EDID firmware so monitors do not autoswitch inputs during Pi reboots.

---

## Supported Hardware & Radio Notes

| Board | Architecture | Video Decode | Wi-Fi Direct Band | Channel Width (`HT40`) | Recommended Max FPS |
|---|---|---|---|---|---|
| **Pi Zero 2 W** *(Primary)* | `arm64` | H.264 HW (`v4l2h264dec` via `/dev/video10`) | **2.4 GHz only** *(no 5 GHz radio)* | Off *(20 MHz avoids 2.4 GHz congestion)* | `30` (`1080p30` HW limit; `720p60` optional) |
| **Pi 4 / CM4** | `arm64` | H.264 HW + HEVC HW (WFD2 opt-in) | **5 GHz** (ch 36 non-DFS) / 2.4 GHz | Optional on 5 GHz | `1080p60` |
| **Pi 5 / CM5** | `arm64` | H.264 NEON SW + **HEVC HW** (`/dev/video19`) | **5 GHz** (ch 36 non-DFS) / 2.4 GHz | Optional on 5 GHz | `1080p60` |

> [!NOTE]
> **Windows 10/11 & WFD 2.0 HEVC:** Windows negotiates WFD 2.0 (`org.wfa.wfd2.0`) HEVC only when the PC has a hardware HEVC encoder, Microsoft's HEVC Video Extensions, and WFD R2 drivers. `picast` always advertises **H.264** baseline/constrained-high so standard `WIN + K` works universally, and enables `wfd2_video_formats` automatically on Pi 4/5 when supported by the sender.

---

## Quick Install (`.deb`)

1. Flash stock **Raspberry Pi OS Lite (Trixie, 64-bit `arm64`)** using Raspberry Pi Imager (configure SSH and Wi-Fi country code).
2. Download the latest `picast_*_arm64.deb` from GitHub Releases (or CI artifacts) and install:
   ```bash
   sudo apt update
   sudo apt install ./picast_*_arm64.deb
   ```
3. Run the guided configurator (shows concise hints + recommended defaults for every option):
   ```bash
   sudo picast configure
   ```
4. Verify codecs, DRM EDID, and dependencies, then enable boot service:
   ```bash
   sudo picast doctor
   sudo picast service enable
   ```

### Useful CLI Operations

```bash
sudo picast service enable          # Switch to exclusive high-performance headless target & start
sudo picast service disable         # Stop service and restore graphical desktop target if installed
sudo picast display force-hdmi      # Enable vc4.force_hotplug=1 + 1080p60D in cmdline.txt
sudo picast display capture-edid    # Save connected HDMI EDID to /lib/firmware/picast-edid.bin
picast status                       # Print active config and systemd status
```

---

## Development & Building

`picast` targets **Raspberry Pi OS Trixie** exclusively (Windows 10/11 is the sender). Root CMake builds and tests both the C++26 engine (`app/` + `modules/`) and Python 3.13+ control service (`service/`), assembling a ready-to-install `systemd` `.deb` package via CPack.

### Raspberry Pi OS Trixie (`GCC 14`, `-std=c++26` + LTO)
```bash
sudo apt install build-essential g++ cmake ninja-build pkg-config python3 python3-pytest \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libglib2.0-dev

cmake --preset rpi-arm64
cmake --build --preset rpi-arm64 -j"$(nproc)"
ctest --preset rpi-arm64
cpack --config build/rpi-arm64/CPackConfig.cmake -G DEB -B dist
```

### Building `.deb` from Windows / x86_64 Host (Docker or WSL2)
```bash
docker run --rm --platform linux/arm64 -v "$PWD:/src" -w /src debian:trixie-slim bash -lc "
  apt-get update && apt-get install -y --no-install-recommends \
    build-essential g++ cmake ninja-build pkg-config file dpkg-dev python3 python3-pytest \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libglib2.0-dev && \
  cmake --preset rpi-arm64 && \
  cmake --build --preset rpi-arm64 -j && \
  ctest --preset rpi-arm64 && \
  cpack --config build/rpi-arm64/CPackConfig.cmake -G DEB -B dist
"
```

### Python 3.13+ Control Service QA (`service/`)
```bash
cd service
pip install -e ".[dev]"
ruff check src test
mypy
pytest
```

---

## Repository Structure

- `app/` — `picast-player` C++26 executable (`CMakeLists.txt`, `include/`, `source/`, `test/`).
- `modules/platform_probe/` — SoC detection, V4L2 node checks, DRM sysfs EDID reader (`CMakeLists.txt`, `include/`, `source/`, `test/`).
- `modules/rtp_jitter/` — Fixed-capacity circular RTP jitter buffer (`CMakeLists.txt`, `include/`, `source/`, `test/`).
- `modules/media_pipeline/` — GStreamer 1.0 V4L2/NEON/HEVC + `kmssink` + audio pipeline (`CMakeLists.txt`, `include/`, `source/`, `test/`).
- `service/` — Self-contained Python 3.13+ control service & CLI (`CMakeLists.txt`, `pyproject.toml`, `src/`, `test/`).
- `packaging/` — Systemd unit (`Nice=-10`, exclusive TTY/DRM), default conffile, wrapper, and `.deb` maintainer scripts.

---

## Credits & License

- Built on original `lazycast` by [homeworkc](https://github.com/homeworkc/lazycast), Education Edition enhancements, and community fork fixes (`gustavomorozi/lazycast2.0`, `K0reem0/lazycast`, `alexanderphoenix/lazycast`, `ressidell/lazycast`).
- Licensed under **GNU General Public License v3.0 (`GPL-3.0`)**. See `LICENSE` for details.
