"""Typed INI configuration, defaults, validation, and guided option hints for picast."""

from __future__ import annotations

import contextlib
import logging
import socket
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)
DEFAULT_CONFIG_PATH: Final[Path] = Path("/etc/picast/picast.conf")


class TransportMode(StrEnum):
    """Discovery and transport mode for incoming Windows WIN+K connections."""

    BOTH = "both"
    P2P = "p2p"
    MICE = "mice"


class PinMode(StrEnum):
    """WPS PIN lifecycle policy displayed on the TTY/framebuffer splash screen."""

    PER_REBOOT = "per-reboot"
    PER_SESSION = "per-session"
    FIXED = "fixed"
    NONE = "none"


class SyncMode(StrEnum):
    """GStreamer sink clock synchronization policy."""

    LOW_LATENCY = "low-latency"
    AV_SYNC = "av-sync"


class CodecPreference(StrEnum):
    """Negotiated Miracast video codec advertisement policy."""

    AUTO = "auto"
    H264 = "h264"
    H265 = "h265"


class AudioSinkMode(StrEnum):
    """Target audio output backend."""

    AUTO = "auto"
    HDMI = "hdmi"
    JACK = "jack"
    ALSA = "alsa"
    PIPEWIRE = "pipewire"


class WifiBandMode(StrEnum):
    """Preferred Wi-Fi Direct Group Owner operating band."""

    AUTO = "auto"
    GHZ_24 = "2.4ghz"
    GHZ_5 = "5ghz"


@dataclass(frozen=True, slots=True)
class SettingGuide:
    """Concise plain-English operator guidance shown during `picast configure`."""

    key: str
    summary: str
    recommended: str


SETTING_GUIDES: Final[dict[str, SettingGuide]] = {
    "device_name": SettingGuide(
        key="device_name",
        summary="Name shown in Windows WIN+K device list.",
        recommended="Hostname or room name (e.g. picast-livingroom)",
    ),
    "transport_mode": SettingGuide(
        key="transport_mode",
        summary="both=Wi-Fi Direct + LAN MICE; p2p=standalone; mice=LAN only.",
        recommended="both (use p2p or Ethernet+p2p on single-radio Zero 2 W for lowest jitter)",
    ),
    "use_own_supplicant": SettingGuide(
        key="use_own_supplicant",
        summary=(
            "Runs a dedicated wpa_supplicant so NetworkManager (Trixie) cannot tear "
            "down the Wi-Fi Direct group; required for WIN+K P2P on NM systems."
        ),
        recommended="true (falls back to system supplicant if no Wi-Fi association)",
    ),
    "pin_mode": SettingGuide(
        key="pin_mode",
        summary="WPS PIN policy: per-reboot, per-session, fixed, or none (buttonless PBC).",
        recommended="per-reboot",
    ),
    "max_fps": SettingGuide(
        key="max_fps",
        summary="Max advertised frame rate (auto/30/50/60). Zero 2 W HW cap is 1080p30.",
        recommended="auto (30 on Pi Zero 2 W, 60 on Pi 4/5)",
    ),
    "jitter_ms": SettingGuide(
        key="jitter_ms",
        summary=(
            "RTP reorder buffer depth in ms "
            "(lower=snappier, higher=smoother over lossy Wi-Fi)."
        ),
        recommended="45",
    ),
    "sync_mode": SettingGuide(
        key="sync_mode",
        summary="low-latency skips strict A/V clock wait; av-sync locks lipsync.",
        recommended="low-latency",
    ),
    "codec": SettingGuide(
        key="codec",
        summary="auto advertises H.264 universally + WFD2 HEVC when Pi 4/5 HW supports it.",
        recommended="auto",
    ),
    "wifi_band": SettingGuide(
        key="wifi_band",
        summary=(
            "2.4ghz works on all Pis (Zero 2 W is 2.4 GHz only); "
            "5ghz cuts interference on Pi 4/5."
        ),
        recommended="auto (2.4ghz on Zero 2 W, 5ghz ch 36 on Pi 4/5)",
    ),
    "ht40": SettingGuide(
        key="ht40",
        summary="40 MHz channel width doubles bandwidth on 5 GHz; keep off on 2.4 GHz Zero 2 W.",
        recommended="false on Zero 2 W, true on 5 GHz Pi 4/5",
    ),
    "wifi_power_save": SettingGuide(
        key="wifi_power_save",
        summary="Disabling Wi-Fi power_save prevents radio sleep micro-stutters.",
        recommended="false (power_save off)",
    ),
    "suppress_bg_scans": SettingGuide(
        key="suppress_bg_scans",
        summary="Pauses background Wi-Fi AP scans during an active cast session.",
        recommended="true",
    ),
    "tcp_nodelay": SettingGuide(
        key="tcp_nodelay",
        summary="Sends RTSP control packets immediately without Nagle buffering delay.",
        recommended="true",
    ),
    "uibc_enabled": SettingGuide(
        key="uibc_enabled",
        summary="Forwards Pi-side USB keyboard & mouse to the Windows source over WFD UIBC HIDC.",
        recommended="true (disable if no keyboard/mouse is attached)",
    ),
    "idr_on_loss": SettingGuide(
        key="idr_on_loss",
        summary="Requests an instant keyframe when UDP packets are dropped.",
        recommended="true",
    ),
    "kms_force_hdmi": SettingGuide(
        key="kms_force_hdmi",
        summary="Keeps HDMI signal active across reboots so monitors do not switch inputs.",
        recommended="true for dedicated displays",
    ),
}


def default_device_name() -> str:
    """Return a clean default Miracast display name derived from the local hostname."""
    raw = socket.gethostname().strip()
    return raw if raw else "picast"


@dataclass(slots=True)
class PicastConfig:
    """Strongly typed persistent runtime configuration for picast."""

    device_name: str = field(default_factory=default_device_name)
    transport_mode: TransportMode = TransportMode.BOTH
    pin_mode: PinMode = PinMode.PER_REBOOT
    fixed_pin: str = "12345670"
    p2p_interface: str = "wlan0"
    use_own_supplicant: bool = True
    p2p_ssid_postfix: str = ""
    wifi_band: WifiBandMode = WifiBandMode.AUTO
    ht40: bool = False
    wifi_power_save: bool = False
    suppress_bg_scans: bool = True
    max_resolution: str = "1920x1080"
    max_fps: str = "auto"
    codec: CodecPreference = CodecPreference.AUTO
    decoder: str = "auto"
    jitter_ms: int = 45
    sync_mode: SyncMode = SyncMode.LOW_LATENCY
    audio_enabled: bool = True
    audio_codec: str = "aac"
    audio_sink: AudioSinkMode = AudioSinkMode.AUTO
    tcp_nodelay: bool = True
    idr_on_loss: bool = True
    uibc_enabled: bool = True
    kms_force_hdmi: bool = False
    low_resource_mode: bool = True
    rtsp_port: int = 7236
    mice_port: int = 7250
    rtp_port: int = 1028
    idr_ipc_port: int = 1029

    def validated(self) -> PicastConfig:
        """Return a clamped/normalized copy enforcing valid port, PIN, and buffer bounds."""
        pin_digits = "".join(ch for ch in self.fixed_pin if ch.isdigit())
        if len(pin_digits) != 8:
            pin_digits = "12345670"

        return PicastConfig(
            device_name=self.device_name.strip()[:32] or "picast",
            transport_mode=self.transport_mode,
            pin_mode=self.pin_mode,
            fixed_pin=pin_digits,
            p2p_interface=self.p2p_interface.strip() or "wlan0",
            use_own_supplicant=bool(self.use_own_supplicant),
            p2p_ssid_postfix=self.p2p_ssid_postfix.strip()[:24],
            wifi_band=self.wifi_band,
            ht40=bool(self.ht40),
            wifi_power_save=bool(self.wifi_power_save),
            suppress_bg_scans=bool(self.suppress_bg_scans),
            max_resolution=self.max_resolution.strip() or "1920x1080",
            max_fps=self.max_fps.strip() or "auto",
            codec=self.codec,
            decoder=self.decoder.strip() or "auto",
            jitter_ms=max(10, min(500, int(self.jitter_ms))),
            sync_mode=self.sync_mode,
            audio_enabled=bool(self.audio_enabled),
            audio_codec="aac" if self.audio_codec.lower() == "aac" else "lpcm",
            audio_sink=self.audio_sink,
            tcp_nodelay=bool(self.tcp_nodelay),
            idr_on_loss=bool(self.idr_on_loss),
            uibc_enabled=bool(self.uibc_enabled),
            kms_force_hdmi=bool(self.kms_force_hdmi),
            low_resource_mode=bool(self.low_resource_mode),
            rtsp_port=max(1024, min(65535, int(self.rtsp_port))),
            mice_port=max(1024, min(65535, int(self.mice_port))),
            rtp_port=max(1024, min(65535, int(self.rtp_port))),
            idr_ipc_port=max(1024, min(65535, int(self.idr_ipc_port))),
        )


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> PicastConfig:
    """Load key=value INI configuration from `path`, falling back to defaults if absent."""
    cfg = PicastConfig()
    if not path.exists():
        return cfg.validated()

    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed reading %s (%s); using default configuration", path, exc)
        return cfg.validated()

    values: dict[str, str] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        if "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        values[key.strip()] = val.strip()

    for f in fields(PicastConfig):
        if f.name not in values:
            continue
        raw = values[f.name]
        cur = getattr(cfg, f.name)
        if isinstance(cur, bool):
            setattr(cfg, f.name, _parse_bool(raw))
        elif isinstance(cur, int):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, int(raw))
        elif isinstance(cur, TransportMode):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, TransportMode(raw.lower()))
        elif isinstance(cur, PinMode):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, PinMode(raw.lower()))
        elif isinstance(cur, SyncMode):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, SyncMode(raw.lower()))
        elif isinstance(cur, CodecPreference):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, CodecPreference(raw.lower()))
        elif isinstance(cur, AudioSinkMode):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, AudioSinkMode(raw.lower()))
        elif isinstance(cur, WifiBandMode):
            with contextlib.suppress(ValueError):
                setattr(cfg, f.name, WifiBandMode(raw.lower()))
        else:
            setattr(cfg, f.name, raw)

    return cfg.validated()


def save_config(cfg: PicastConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist `cfg` cleanly as documented `key = value` lines."""
    norm = cfg.validated()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# picast configuration (managed via `picast configure`)",
        "[picast]",
    ]
    for f in fields(PicastConfig):
        val = getattr(norm, f.name)
        if isinstance(val, bool):
            rendered = "true" if val else "false"
        elif isinstance(val, StrEnum):
            rendered = val.value
        else:
            rendered = str(val)
        lines.append(f"{f.name} = {rendered}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
