# picast Modernization & Trixie Execution Plan

> **Purpose:** Finalized engineering, build-verification, and live Raspberry Pi testing handoff for an off-site execution agent (`picast`, successor fork of `LeHuman/lazycast`, now `LeHuman/picast`). The architecture cleanly separates the **C++26 media engine** (`app/` + `modules/*`, each independently buildable/testable with `CMakeLists.txt` + `test/` and statically linked with Release LTO + `--gc-sections`) from the **Python 3.13+ systemd control service & CLI** (`service/`), unified under root `CMakeLists.txt` to compile, validate, test, and package an immediately runnable `.deb` (`picast.service` → `/usr/bin/picast run` → `picast-player`).

---

## 1. Confirmed Architectural Decisions & Technical Rationale

1. **Two-Component Split Unified by Root CMake (`app/` + `modules/` + `service/`):**
   - **Data Plane (`app/`, `modules/`):** C++26 (`CMAKE_CXX_STANDARD 26`, GCC 14 Trixie ABI-compatible), safety-critical patterns (pre-allocated circular `RtpJitterBuffer` with zero hot-path allocations, `std::span`, `std::expected`, `[[nodiscard]]`, `noexcept`, RAII `SocketGuard`, full Doxygen docstrings).
   - **Control Service (`service/`):** Self-contained Python 3.13+ package (`service/CMakeLists.txt`, `service/pyproject.toml`, `service/src/`, `service/test/`). Root CMake triggers `service/` syntax validation, runs `picast.service_tests` in CTest (when `pytest` is installed), and installs the Python library, `/usr/bin/picast`, `/lib/systemd/system/picast.service`, and `/etc/picast/picast.conf` into the `.deb`.
2. **Target Platform Discipline (RPi / Trixie Only Receiver):**
   - Windows 10/11 is strictly a Miracast sender (`WIN + K`), never a receiver; all `#ifdef _WIN32`/Winsock code has been stripped. Windows/x86_64 dev machines build and test using **WSL2** or **Docker (`debian:trixie-slim`)** via simplified presets (`debug`, `release`, `rpi-arm64`, `rpi-pi5`).
3. **Target Hardware, Radio & Codec Realities:**
   - **Pi Zero 2 W (Primary, 512 MB RAM):** Hardware H.264 decode (`/dev/video10` `v4l2h264dec capture-io-mode=4` -> `kmssink`). **Radio is 2.4 GHz only**; `HT40` stays **off** on Zero 2 W to prevent 2.4 GHz OBSS coexistence degradation. Rated for **1080p30**.
   - **Pi 4 & Pi 5 (Secondary):** Dual-band **5 GHz** (non-DFS ch `36` / `5180 MHz` default) + optional `HT40` + `1080p60`. **Pi 5 lacks HW H.264 decode** (uses NEON `avdec_h264`), **but CAN hardware-decode HEVC** (`/dev/video19` `v4l2slh265dec`).
   - **WFD 1.0 vs WFD 2.0 HEVC:** Always advertise **H.264** universally; offer `wfd2_video_formats` conditionally on Pi 4/5 when the Windows sender supports WFD 2.0 HEVC.
4. **Exclusive Display Ownership & UX Polish:**
   - No Wayland/`labwc` overhead: `picast.service` claims `tty1` / DRM directly (`multi-user.target`, `Nice=-10`, `OOMScoreAdjust=-500`) and `picast service disable` restores graphical boot cleanly if needed.
   - **Color-Block Splash (`splash.py`):** Oversized high-contrast pixel-block typography to `/dev/fb0` (32-bpp mmap) or `/dev/tty1` ANSI blocks.
   - **PIN Policy:** **`per-reboot`** default (`/run/picast/pin`), plus `per-session`, `fixed:<8-digits>`, and `none` (`PBC`).
   - **KMS Always-On HDMI (`picast display force-hdmi` / `capture-edid`):** Updates `/boot/firmware/cmdline.txt` safely with backup.
5. **Audit Fixes Applied:**
   - Wired `WifiP2pManager.start()` to await creation of the `p2p-wl*-*` group interface, arm `wps_pin`/`wps_pbc` directly on the group netdev, and spawn `dnsmasq` single-IP DHCP.
   - Parsed real M6 `Session:` ID and M4 `wfd_presentation_URL`, and added the UDP loopback IDR listener (`idr_ipc_port`) forwarding `wfd_idr_request` during packet loss.
   - Added MS-MICE v1 `SOURCE_READY` TLV parsing in `MiceService` triggering outbound RTSP connection to the Windows sender under `Session Lock`.

---

## 2. Off-Site Agent Remaining Execution Checklist

### Phase 0: Local Git Normalization & Commits
- [ ] Verify `.gitignore` keeps `.agent/LOCAL_SETUP.md` and AI Studio harness files (`package.json`, `index.html`, `vite.config.ts`, `tsconfig.json`, `metadata.json`, `node_modules/`, `dist/`) untracked (`git status --ignored`).
- [ ] Add upstream remote and rename `master` -> `main` while keeping `upstream/master` fetchable:
  ```bash
  git remote add upstream https://github.com/homeworkc/lazycast.git
  git fetch upstream
  git branch -m master main
  git push -u origin main
  ```
- [ ] Split and commit changes cleanly with **Conventional Commits + Gitmojis** (short messages):
  - `✨ feat(cmake): add modular C++26/LTO CMake, service CMake, and .deb CPack config`
  - `✨ feat(modules): implement platform_probe, rtp_jitter, media_pipeline, and unit tests`
  - `✨ feat(app): implement picast-player POSIX receiver and CLI argument parser`
  - `♻️ refactor(service): implement typed Python 3.13+ daemon, WFD/MICE, splash, and CLI`
  - `👷 ci: add GitHub Actions QA, standalone module build check, and Trixie .deb pipeline`
  - `🔥 chore: remove retired legacy lazycast scripts and OpenMAX C sources`
  - `📝 docs: update README to PMPi standard for picast`

### Phase 1: Compilation, Static Analysis & Packaging Verification
- [ ] Run Linux Debug build + all C++ & Python CTests (natively, in WSL2, or Docker):
  ```bash
  cmake --preset debug -DPICAST_WARNINGS_AS_ERRORS=ON
  cmake --build --preset debug -j"$(nproc)"
  ctest --preset debug
  ```
- [ ] Verify standalone per-module build & test composability (`platform_probe`, `rtp_jitter`, `media_pipeline`, `app`, `service`):
  ```bash
  cmake -S modules/rtp_jitter -B build/standalone-rtp -G Ninja -DPICAST_WARNINGS_AS_ERRORS=ON
  cmake --build build/standalone-rtp && ctest --test-dir build/standalone-rtp --output-on-failure
  ```
- [ ] Verify `service/` QA suite cleanly under Python 3.13:
  ```bash
  cd service && python -m pip install -e ".[dev]" && ruff check src test && mypy && pytest
  ```
- [ ] Build the `arm64` `.deb` (via `docker run --platform linux/arm64 ... debian:trixie-slim` or directly on Pi) and inspect `.deb` contents (`dpkg-deb -c dist/picast-*.deb`) to confirm `/usr/bin/picast-player`, `/usr/bin/picast`, `/usr/lib/picast/python/picast/*.py`, `/lib/systemd/system/picast.service`, and `/etc/picast/picast.conf` are all present and executable.

### Phase 2: Real Hardware Spike & Live Streaming Verification (`Pi Zero 2 W` & `Pi 5` via `.agent/LOCAL_SETUP.md`)
- [ ] Install built `.deb` (`sudo apt install ./dist/picast_*.deb`) on **Pi Zero 2 W** and **Pi 5** running stock Raspberry Pi OS Lite (Trixie, `arm64`).
- [ ] Run `sudo picast configure`, `sudo picast doctor`, and `sudo picast service enable`; confirm `systemctl status picast.service` runs cleanly with splash visible on `tty1`/HDMI and `v4l2h264dec capture-io-mode=4 ! kmssink` (Zero 2 W) / `avdec_h264` or `v4l2slh265dec` + `kmssink` (Pi 5).
- [ ] Verify NetworkManager coexistence under Trixie: confirm `p2p-dev-wlan0` GO group does not get torn down (`AP-DISABLED`) by NetworkManager and `iw event` shows zero background Wi-Fi scanning during casting (add a scoped NM pause/unmanaged rule in `wifi_p2p.py` if Trixie NM interferes).
- [ ] Check for **brcmfmac dual-IP quirk** on Zero 2 W (`192.168.173.1/24` on `wlan0` if Windows fails TCP `:7236` reachability across `p2p-wl*`).
- [ ] End-to-end **Windows 10/11 `WIN + K`** test over **Wi-Fi Direct (`p2p`)** and **MICE (`mice` / `both`)**:
  - Verify WPS PIN entry with `per-reboot` PIN & color-block splash on `/dev/fb0` and `/dev/tty1`
  - Confirm `picast-player` video + audio playback, IDR packet-loss recovery (`wfd_idr_request`), and clean disconnect/lock release back to splash
  - Verify WFD 2.0 HEVC negotiation vs H.264 fallback on Pi 5
  - Verify `sudo picast display force-hdmi` + `capture-edid` persistence across reboots, and `sudo picast service disable` restore behavior.

### Phase 3: Stretch Goals (Post-MVP)
- [ ] **`UibcInput` Module (`modules/uibc_input/`):** Port `evdev` mouse/keyboard capture forwarding over Windows TCP HIDC without X11 (`Xlib`).
- [ ] **`armhf` Benchmark Target:** Add a 32-bit `armhf` build preset & CI matrix entry to benchmark 32-bit RSS memory savings vs `arm64` on 512 MB Pi Zero 2 W.
