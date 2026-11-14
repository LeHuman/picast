"""Hardware capability probing and safe Raspberry Pi system tuning helpers."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .config import CodecPreference, PicastConfig, WifiBandMode

logger = logging.getLogger(__name__)


class BoardKind(StrEnum):
    """Detected Raspberry Pi or generic host family."""

    PI_ZERO_2_W = "pi-zero-2w"
    PI_3 = "pi-3"
    PI_4 = "pi-4"
    PI_5 = "pi-5"
    GENERIC_LINUX = "linux"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    """Runtime hardware profile guiding codec, radio, and FPS defaults."""

    board: BoardKind
    model_name: str
    has_v4l2_h264: bool
    has_v4l2_hevc: bool
    has_5ghz_wifi: bool
    supports_ht40: bool
    recommended_max_fps: int


def classify_board(model_text: str, cpuinfo_text: str) -> BoardKind:
    """Classify Raspberry Pi hardware from device-tree model and `/proc/cpuinfo`."""
    low_model = model_text.lower()
    low_cpu = cpuinfo_text.lower()
    if "zero 2" in low_model:
        return BoardKind.PI_ZERO_2_W
    if "raspberry pi 5" in low_model or "compute module 5" in low_model or "bcm2712" in low_cpu:
        return BoardKind.PI_5
    if (
        "raspberry pi 4" in low_model
        or "raspberry pi 400" in low_model
        or "compute module 4" in low_model
        or "bcm2711" in low_cpu
    ):
        return BoardKind.PI_4
    if "raspberry pi 3" in low_model:
        return BoardKind.PI_3
    if model_text or cpuinfo_text:
        return BoardKind.GENERIC_LINUX
    return BoardKind.UNKNOWN


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").replace("\x00", "").strip()
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return ""


def _has_v4l2_driver(
    v4l2_sysfs: Path,
    dev_dir: Path,
    driver_needle: str,
    fallback_node: str,
) -> bool:
    if v4l2_sysfs.exists():
        try:
            for entry in sorted(v4l2_sysfs.iterdir()):
                name = _read_text(entry / "name").lower()
                if driver_needle in name and (dev_dir / entry.name).exists():
                    return True
        except OSError as exc:
            logger.debug("Failed iterating %s: %s", v4l2_sysfs, exc)
    return (dev_dir / fallback_node).exists()


def probe_host_capabilities(
    dt_model_path: Path = Path("/proc/device-tree/model"),
    cpuinfo_path: Path = Path("/proc/cpuinfo"),
    dev_dir: Path = Path("/dev"),
    v4l2_sysfs: Path = Path("/sys/class/video4linux"),
) -> HostCapabilities:
    """Inspect procfs, sysfs, and `/dev/video*` to determine optimal codec and radio settings."""
    model_str = _read_text(dt_model_path)
    cpuinfo_str = _read_text(cpuinfo_path)
    board = classify_board(model_str, cpuinfo_str)

    has_h264_dec = _has_v4l2_driver(v4l2_sysfs, dev_dir, "bcm2835-codec-decode", "video10")
    has_hevc_dec = _has_v4l2_driver(v4l2_sysfs, dev_dir, "rpi-hevc-dec", "video19")

    if board is BoardKind.PI_ZERO_2_W:
        return HostCapabilities(
            board=board,
            model_name=model_str or "Raspberry Pi Zero 2 W",
            has_v4l2_h264=has_h264_dec,
            has_v4l2_hevc=False,
            has_5ghz_wifi=False,
            supports_ht40=False,
            recommended_max_fps=30,
        )
    if board in (BoardKind.PI_4, BoardKind.PI_5):
        return HostCapabilities(
            board=board,
            model_name=model_str or board.value,
            has_v4l2_h264=has_h264_dec and board is BoardKind.PI_4,
            has_v4l2_hevc=has_hevc_dec,
            has_5ghz_wifi=True,
            supports_ht40=True,
            recommended_max_fps=60,
        )
    if board is BoardKind.PI_3:
        is_plus = "plus" in model_str.lower() or "3 b+" in model_str.lower()
        return HostCapabilities(
            board=board,
            model_name=model_str or "Raspberry Pi 3",
            has_v4l2_h264=has_h264_dec,
            has_v4l2_hevc=False,
            has_5ghz_wifi=is_plus,
            supports_ht40=is_plus,
            recommended_max_fps=30,
        )
    return HostCapabilities(
        board=board,
        model_name=model_str or "Generic Linux",
        has_v4l2_h264=False,
        has_v4l2_hevc=False,
        has_5ghz_wifi=False,
        supports_ht40=False,
        recommended_max_fps=60,
    )


def resolve_effective_fps(cfg: PicastConfig, caps: HostCapabilities) -> int:
    """Resolve explicit FPS or board-appropriate `auto` FPS ceiling."""
    if cfg.max_fps == "auto":
        return caps.recommended_max_fps
    try:
        parsed = int(cfg.max_fps)
        return max(24, min(60, parsed))
    except ValueError:
        return caps.recommended_max_fps


def resolve_wfd2_hevc_enabled(cfg: PicastConfig, caps: HostCapabilities) -> bool:
    """Determine whether WFD 2.0 HEVC may be offered alongside universal H.264."""
    if cfg.codec is CodecPreference.H264:
        return False
    if cfg.codec is CodecPreference.H265:
        return caps.has_v4l2_hevc or caps.board is BoardKind.PI_5
    return caps.has_v4l2_hevc and caps.board in (BoardKind.PI_4, BoardKind.PI_5)


def resolve_p2p_oper_freq(cfg: PicastConfig, caps: HostCapabilities) -> int:
    """Select P2P GO operating frequency in MHz (`2437` ch6 vs non-DFS `5180` ch36)."""
    if cfg.wifi_band is WifiBandMode.GHZ_24:
        return 2437
    if cfg.wifi_band is WifiBandMode.GHZ_5 and caps.has_5ghz_wifi:
        return 5180
    return 5180 if caps.has_5ghz_wifi else 2437


def resolve_p2p_ht40_allowed(cfg: PicastConfig, caps: HostCapabilities) -> bool:
    """Only enable 40 MHz HT40 when operating on 5 GHz capable hardware."""
    return bool(cfg.ht40 and caps.supports_ht40 and resolve_p2p_oper_freq(cfg, caps) >= 5000)


def apply_runtime_wifi_tuning(interface: str, power_save: bool) -> None:
    """Disable Wi-Fi power saving on `interface` (`iw dev <iface> set power_save off`)."""
    iw_bin = shutil.which("iw")
    if not iw_bin:
        return
    mode = "on" if power_save else "off"
    try:
        res = subprocess.run(
            [iw_bin, "dev", interface, "set", "power_save", mode],
            check=False,
            timeout=5.0,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            logger.debug(
                "iw power_save %s failed on %s (rc=%d): %s",
                mode,
                interface,
                res.returncode,
                res.stderr.strip(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Timed out or failed tuning power_save on %s: %s", interface, exc)
