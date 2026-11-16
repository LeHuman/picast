# Goal

The goal for `picast` (modernized successor fork of `lazycast` for Raspberry Pi OS Trixie) is as follows:

- [x] Upgrade target platform to Raspberry Pi OS Trixie (Debian 13) — C++26 GCC 14 + GStreamer V4L2/DRM-KMS + Python 3.13+
- [x] Pull useful changes from the related forks (`wifi_display=1`, `p2p_ssid_postfix`, orphan `p2p-wl*` cleanup, `Content-Length` TCP framing, dynamic `CSeq`, non-leaky GStreamer queue + `config-interval=-1`, sysfs DRM EDID, dynamic DHCP lease lookup)
- [x] Prioritize usage with RPi Zero 2 W (512 MB RAM, 2.4 GHz radio / no HT40, V4L2 H.264 1080p30 HW cap), with RPi 4 & RPi 5 (5 GHz + NEON H.264 / HW HEVC WFD2) supported
- [x] Push for performance where possible (static linking + Release LTO + `--gc-sections`, `-mcpu=cortex-a53`, `Nice=-10`, exclusive TTY/DRM ownership without Wayland, zero hot-path allocation RTP ring buffer, `power_save off`, scan suppression, `TCP_NODELAY`, dirty-rect framebuffer splash)
- [x] Convert project to use CMake with `CONFIGURE_DEPENDS` discovery and independent per-target `CMakeLists.txt` + `test/` suites unified by root CMake
- [x] Remove the need for a custom OS image; build an installable `.deb` with runnable `picast.service` via root CMake + CPack
- [x] Use modern streaming capabilities (GStreamer 1.0 V4L2/NEON/`kmssink`, ALSA `sysdefault` / PipeWire, conditional WFD 2.0 HEVC, UIBC HIDC keyboard/mouse passthrough)
- [x] Prioritize Windows 10/11 (`WIN + K` Wi-Fi Direct & MS-MICE) compatibility with configurable deployment (`picast configure`, `picast doctor`)
- [x] Refactor data plane to safety-critical C++26 with Doxygen docstrings and control plane to typed Python 3.13+ (`ruff`, `mypy --strict`, `pytest`)
- [x] Post-reset maintainability & dead-code audit (`9195ece` + follow-up fixes verified against `pidev` build/CTest and live `picast` Zero 2 W hardware)
