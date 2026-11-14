"""DRM sysfs EDID inspection and CEA display mode mapping for WFD negotiation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)
FALLBACK_EDID: Final[bytes] = bytes(128)


@dataclass(frozen=True, slots=True)
class DisplayTimingSummary:
    """Human-readable resolution/refresh summary extracted from an EDID blob."""

    connector: str
    connected: bool
    width: int
    height: int
    refresh_hz: int
    edid_bytes: bytes


def read_drm_edid(
    drm_root: Path = Path("/sys/class/drm"),
    preferred_connector: str | None = None,
    override_edid_path: Path | None = None,
) -> tuple[str, bool, bytes]:
    """Read raw EDID bytes via DRM sysfs without legacy `/opt/vc/bin/tvservice`."""
    try:
        if override_edid_path and override_edid_path.exists():
            raw = override_edid_path.read_bytes()
            if len(raw) >= 128:
                return ("override", True, raw)
    except OSError as exc:
        logger.warning("Failed reading override EDID %s: %s", override_edid_path, exc)

    if not drm_root.exists():
        return ("none", False, FALLBACK_EDID)

    try:
        connectors = sorted(
            p for p in drm_root.iterdir() if p.is_dir() and "HDMI-A-" in p.name
        )
    except OSError as exc:
        logger.warning("Failed iterating DRM root %s: %s", drm_root, exc)
        return ("none", False, FALLBACK_EDID)

    if preferred_connector:
        connectors.sort(key=lambda p: (0 if preferred_connector in p.name else 1, p.name))

    for conn in connectors:
        try:
            status_path = conn / "status"
            status = (
                status_path.read_text(encoding="utf-8", errors="ignore").strip()
                if status_path.exists()
                else ""
            )
            if status and status != "connected":
                continue
            edid_path = conn / "edid"
            if edid_path.exists():
                raw = edid_path.read_bytes()
                if len(raw) >= 128:
                    return (conn.name, True, raw)
        except OSError as exc:
            logger.debug("Skipping unreadable DRM connector %s: %s", conn, exc)

    for conn in connectors:
        try:
            edid_path = conn / "edid"
            if edid_path.exists():
                raw = edid_path.read_bytes()
                if len(raw) >= 128:
                    return (conn.name, False, raw)
        except OSError as exc:
            logger.debug("Skipping fallback DRM EDID %s: %s", conn, exc)

    return ("none", False, FALLBACK_EDID)


def parse_preferred_timing(edid_bytes: bytes) -> tuple[int, int, int]:
    """Decode the first Detailed Timing Descriptor (bytes 54..71) into `(width, height, hz)`."""
    if len(edid_bytes) < 72:
        return (1920, 1080, 60)

    dtd = edid_bytes[54:72]
    pixel_clock_10khz = int.from_bytes(dtd[0:2], "little")
    if pixel_clock_10khz == 0:
        return (1920, 1080, 60)

    h_active = dtd[2] | ((dtd[4] >> 4) << 8)
    h_blank = dtd[3] | ((dtd[4] & 0x0F) << 8)
    v_active = dtd[5] | ((dtd[7] >> 4) << 8)
    v_blank = dtd[6] | ((dtd[7] & 0x0F) << 8)
    if h_active <= 0 or v_active <= 0 or (h_active + h_blank) == 0 or (v_active + v_blank) == 0:
        return (1920, 1080, 60)

    pixel_clock_hz = pixel_clock_10khz * 10_000
    total_pixels = (h_active + h_blank) * (v_active + v_blank)
    hz = max(24, min(120, round(pixel_clock_hz / total_pixels)))
    return (h_active, v_active, hz)


def inspect_display(
    drm_root: Path = Path("/sys/class/drm"),
    preferred_connector: str | None = None,
    override_edid_path: Path | None = Path("/lib/firmware/picast-edid.bin"),
) -> DisplayTimingSummary:
    """Return active DRM connector status and preferred timing from EDID."""
    connector, connected, raw = read_drm_edid(drm_root, preferred_connector, override_edid_path)
    width, height, hz = parse_preferred_timing(raw)
    return DisplayTimingSummary(
        connector=connector,
        connected=connected,
        width=width,
        height=height,
        refresh_hz=hz,
        edid_bytes=raw,
    )


def _cea_native_and_mask(max_resolution: str, max_fps: int) -> tuple[str, str]:
    if max_resolution.startswith("1280x720"):
        return ("02", "00000007")
    if max_fps <= 30:
        return ("08", "00015555")
    return ("08", "0001FFFF")


def build_wfd_video_formats(max_resolution: str, max_fps: int) -> str:
    """Build WFD 1.0 H.264 capability payload matching upstream lazycast (`d2.py`).

    Fields: native (`00` = CEA idx 0, 640x480p60), preferred_display_mode (`00`),
    profile (`02` = CHP), level (`10` = H.264 Level 4.2, required for 1080p),
    CEA bitmask (`0001FEFF` caps at 1080p30; `0001FFFF` enables 1080p60;
    `0000007F` caps at 720p), VESA bitmask, HH bitmask, latency/slice/frame
    control fields. Using `native=08` or `level=02` (3.2) or zero VESA masks
    causes Windows 10 `MSMiracastSource` to drop the RTSP session at M3.
    """
    if max_resolution.startswith("1280x720"):
        return "00 00 02 10 0000007F 000003FF 00000FFF 00 0000 0000 00 none none"
    if max_fps <= 30:
        return "00 00 02 10 0001FEFF 3FFFFFFF 00000FFF 00 0000 0000 00 none none"
    return "00 00 02 10 0001FFFF 3FFFFFFF 00000FFF 00 0000 0000 00 none none"


def build_wfd2_video_formats(max_resolution: str, max_fps: int, hevc: bool = True) -> str:
    """Build WFD 2.0 video format payload (HEVC profile 0x02 or H.264) honoring caps."""
    native, cea_mask = _cea_native_and_mask(max_resolution, max_fps)
    profile = "02 01" if hevc else "02 02"
    return f"{native} 00 {profile} {cea_mask} 00000000 00000000 00 0000 0000 00 none none"
