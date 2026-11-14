# Goal

The goal for this existing project (`picast`, modernized successor fork of `lazycast`) is as follows:

- [x] Upgrade target platform to Raspberry Pi OS Trixie (Debian 13) — C++26 GCC 14 + GStreamer V4L2/DRM-KMS + Python 3.13+
- [x] Pull useful changes from the related forks (`wifi_display=1`, `p2p_ssid_postfix`, orphan `p2p-wl*` cleanup, `Content-Length` TCP framing, dynamic `CSeq`, non-leaky GStreamer queue + `config-interval=-1`, sysfs DRM EDID, single-IP DHCP)
- [x] Prioritize usage with RPi Zero 2 W (512 MB RAM, 2.4 GHz radio / no HT40, V4L2 H.264 1080p30 HW cap), with RPi 4 & RPi 5 (5 GHz + NEON H.264 / HW HEVC WFD2) supported
- [x] Push for performance where possible (static linking + Release LTO + `--gc-sections`, `-mcpu=cortex-a53`, `Nice=-10`, exclusive TTY/DRM ownership without Wayland, zero hot-path allocation RTP ring buffer, `power_save off`, scan suppression, `TCP_NODELAY`)
- [x] Convert project to use CMake with `CONFIGURE_DEPENDS` discovery and independent per-target `CMakeLists.txt` + `test/` suites unified by root `CMakeLists.txt`
- [x] Add `.vscode` tasks and launch configurations for building, testing, and SSH `.deb` deployment
- [x] Clean up repository layout and remove legacy Bullseye/OpenMAX files (`app/`, `modules/{platform_probe,rtp_jitter,media_pipeline}/`, `service/`, `packaging/`, `cmake/`)
- [x] Remove the need for a custom OS image; build an installable `.deb` with runnable `picast.service` via root CMake + CPack
- [x] Use modern streaming capabilities (GStreamer 1.0 V4L2/NEON/`kmssink`, PipeWire/ALSA, conditional WFD 2.0 HEVC)
- [x] Prioritize Windows 10/11 (`WIN + K` Wi-Fi Direct & MS-MICE) support cleanly with RPi-only target code
- [x] Allow configurable deployment (device name, `per-reboot`/`per-session`/`fixed`/`none` WPS PIN, latency, FPS, codecs, Wi-Fi band, KMS always-on HDMI) with plain-English guidance (`picast configure`, `picast doctor`)
- [x] Refactor data plane to safety-critical C++26 with Doxygen docstrings and control plane to typed Python 3.13+ (`service/pyproject.toml`, `ruff`, `mypy --strict`, `pytest`)
- [x] Update `README.md` to match the `LeHuman/PMPi` standard
- [ ] **Remaining (Off-Site Agent in `.agent/PLAN.md`):**
  - Local Git normalization (`master` -> `main` keeping `upstream/master`, Conventional Commits + Gitmojis)
  - Native / Docker / WSL2 compilation, `ruff` + `mypy` + `pytest`, `ctest`, and `.deb` packaging verification
  - Live Raspberry Pi Zero 2 W & Pi 5 hardware testing against Windows 10/11 `WIN + K` (Wi-Fi Direct + MICE)
  - Stretch goals: `UibcInput` (`evdev` -> Windows TCP HIDC) and `armhf` benchmark preset

## Related Forks

- https://github.com/gustavomorozi/lazycast2.0
- https://github.com/K0reem0/lazycast
- https://github.com/alexanderphoenix/lazycast
- https://github.com/ressidell/lazycast

## Local setup

Look at `LOCAL_SETUP.md` for more info on our local testing ground (not git tracked).

## Conventions

- Updated `README.md` to fit the `LeHuman/PMPi` standard.
- Rename default branch to `main` while tracking `upstream/master` (see `.agent/PLAN.md` Phase 0).
- Use Conventional Commits with Gitmojis in concise, reviewable commits (see `.agent/PLAN.md` Phase 0).
- Maintain modern standards (`C++26` safety-critical + Doxygen, typed `Python 3.13+` in `service/` with `ruff`/`mypy --strict`/`pytest`).
