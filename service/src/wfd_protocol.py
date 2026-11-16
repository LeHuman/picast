"""Pure stateless RTSP / Wi-Fi Display (WFD 1.0 & 2.0) protocol parsers and formatters."""

from __future__ import annotations

import re
from typing import Final

from .config import CodecPreference, PicastConfig
from .edid import (
    DisplayTimingSummary,
    build_wfd2_video_formats,
    build_wfd_video_formats,
    inspect_display,
)
from .platform import (
    HostCapabilities,
    resolve_effective_fps,
    resolve_wfd2_hevc_enabled,
)

MAX_RTSP_BUFFER_BYTES: Final[int] = 65536
_CSEQ_RE = re.compile(rb"CSeq:\s*(\d+)", re.IGNORECASE)
_SESSION_RE = re.compile(r"Session:\s*([^\s;]+)", re.IGNORECASE)
_PRES_URL_RE = re.compile(r"wfd_presentation_URL:\s*([^\s\r\n]+)", re.IGNORECASE)
_TRANSPORT_PORTS_RE = re.compile(r"client_port=(\d+)(?:-(\d+))?")
_UIBC_CAPABILITY_RE = re.compile(r"wfd_uibc_capability:\s*([^\r\n]+)", re.IGNORECASE)
_WFD2_VIDEO_FORMATS_RE = re.compile(
    r"^wfd2_video_formats:\s*([^\r\n]+)", re.IGNORECASE | re.MULTILINE
)


class RtspStreamFramer:
    """Incremental TCP parser assembling bounded header+Content-Length RTSP frames."""

    def __init__(self, max_buffer_bytes: int = MAX_RTSP_BUFFER_BYTES) -> None:
        self._buf = bytearray()
        self._max_buffer_bytes = max_buffer_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        """Append incoming TCP bytes and emit any complete RTSP messages."""
        if not chunk:
            return []
        if len(self._buf) + len(chunk) > self._max_buffer_bytes:
            self._buf.clear()
            raise ValueError(f"RTSP frame exceeded {self._max_buffer_bytes} bytes")
        self._buf.extend(chunk)
        messages: list[bytes] = []

        while True:
            sep_idx = self._buf.find(b"\r\n\r\n")
            sep_len = 4
            if sep_idx == -1:
                sep_idx = self._buf.find(b"\n\n")
                sep_len = 2
            if sep_idx == -1:
                break

            header_bytes = bytes(self._buf[:sep_idx])
            content_length = 0
            for raw_line in header_bytes.splitlines():
                lower = raw_line.lower()
                if lower.startswith(b"content-length:"):
                    try:
                        content_length = max(0, int(raw_line.split(b":", 1)[1].strip()))
                    except ValueError:
                        content_length = 0

            total_len = sep_idx + sep_len + content_length
            if total_len > self._max_buffer_bytes:
                self._buf.clear()
                raise ValueError(f"Declared RTSP frame length {total_len} exceeds limit")
            if len(self._buf) < total_len:
                break

            frame = bytes(self._buf[:total_len])
            del self._buf[:total_len]
            messages.append(frame)

        return messages


def extract_cseq(message: bytes) -> str:
    """Extract `CSeq` dynamically from incoming RTSP packets (falling back to `1`)."""
    match = _CSEQ_RE.search(message)
    return match.group(1).decode("ascii") if match else "1"


def extract_session_id(message_text: str, fallback: str = "12345678") -> str:
    """Extract RTSP `Session:` identifier returned by Windows in M6 `SETUP` response."""
    match = _SESSION_RE.search(message_text)
    return match.group(1).strip() if match else fallback


def parse_presentation_url(message_text: str, peer_ip: str) -> str:
    """Extract `wfd_presentation_URL` from M4 `SET_PARAMETER`, defaulting to peer IP."""
    match = _PRES_URL_RE.search(message_text)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("rtsp://"):
            return candidate
    return f"rtsp://{peer_ip}/wfd1.0/streamid=0"


def parse_client_rtp_port(message_text: str, default_port: int = 1028) -> int:
    """Extract negotiated UDP RTP port from WFD M4/M6 `client_port=` header."""
    match = _TRANSPORT_PORTS_RE.search(message_text)
    if not match:
        return default_port
    try:
        return int(match.group(1))
    except ValueError:
        return default_port


def parse_uibc_port(message_text: str) -> int | None:
    """Extract the source's HIDC `port=<N>` from M4 `wfd_uibc_capability:`."""
    match = _UIBC_CAPABILITY_RE.search(message_text)
    if not match:
        return None
    for item in match.group(1).split(";"):
        item = item.strip()
        if not item.lower().startswith("port="):
            continue
        raw = item.split("=", 1)[1].strip()
        if raw.lower() == "none" or not raw.isdigit():
            return None
        port = int(raw)
        return port if 1 <= port <= 65535 else None
    return None


def is_wfd2_hevc_selected(message_text: str) -> bool:
    """Return True only when `wfd2_video_formats:` selects an active non-H.264 format."""
    match = _WFD2_VIDEO_FORMATS_RE.search(message_text)
    if not match:
        return False
    val = match.group(1).strip()
    if not val or val.lower() == "none":
        return False
    tokens = val.split()
    # WFD 2.0: token[2] is codec-type (`02`) and token[3] is profile (`01`=HEVC Main,
    # `02`=H.264 CHP). Reject `02 02` so H.264 sent under WFD2 never launches an HEVC decoder.
    if len(tokens) >= 4 and tokens[2] == "02" and tokens[3] == "02":
        return False
    return True


def build_wfd_m3_response(
    requested_body: str,
    cfg: PicastConfig,
    caps: HostCapabilities,
    rtp_port: int,
    allow_uibc: bool = False,
    display_info: DisplayTimingSummary | None = None,
) -> str:
    """Build WFA M3 `GET_PARAMETER` response with universal H.264 + optional WFD 2.0 HEVC."""
    effective_fps = resolve_effective_fps(cfg, caps)
    video_formats = build_wfd_video_formats(cfg.max_resolution, effective_fps)
    info = display_info if display_info is not None else inspect_display()
    requested_params = {ln.strip() for ln in requested_body.splitlines() if ln.strip()}

    lines: list[str] = []
    if "wfd_video_formats" in requested_params:
        lines.append(f"wfd_video_formats: {video_formats}")
    if "wfd2_video_formats" in requested_params:
        if resolve_wfd2_hevc_enabled(cfg, caps):
            hevc_flag = cfg.codec is not CodecPreference.H264
            wfd2_fmt = build_wfd2_video_formats(cfg.max_resolution, effective_fps, hevc=hevc_flag)
            lines.append(f"wfd2_video_formats: {wfd2_fmt}")
        else:
            lines.append("wfd2_video_formats: none")
    if "wfd_audio_codecs" in requested_params:
        # Primary Sink (0x0011) MUST advertise at least one mandatory WFD audio
        # codec; returning `wfd_audio_codecs: none` makes Windows 10 abort RTSP at M3.
        if cfg.audio_codec == "lpcm":
            lines.append("wfd_audio_codecs: LPCM 00000003 00")
        else:
            lines.append("wfd_audio_codecs: AAC 00000001 00")
    if "wfd_3d_video_formats" in requested_params:
        lines.append("wfd_3d_video_formats: none")
    if "wfd_content_protection" in requested_params:
        lines.append("wfd_content_protection: none")
    if "wfd_coupled_sink" in requested_params:
        lines.append("wfd_coupled_sink: none")
    if "wfd_connector_type" in requested_params:
        lines.append("wfd_connector_type: 05")
    if "wfd_uibc_capability" in requested_params:
        lines.append(
            "wfd_uibc_capability: input_category_list=GENERIC, HIDC;"
            "generic_cap_list=Keyboard, Mouse;"
            "hidc_cap_list=Keyboard/USB, Mouse/USB;port=none"
            if allow_uibc
            else "wfd_uibc_capability: none"
        )
    if "wfd_client_rtp_ports" in requested_params:
        lines.append(f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {rtp_port} 0 mode=play")
    if "wfd_display_edid" in requested_params:
        edid_magic = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        if info.connected and info.edid_bytes[:8] == edid_magic:
            edid_len_blocks = max(1, len(info.edid_bytes) // 128)
            hex_payload = info.edid_bytes[: edid_len_blocks * 128].hex()
            lines.append(f"wfd_display_edid: {edid_len_blocks:04X} {hex_payload}")
        else:
            lines.append("wfd_display_edid: none")
    if "intel_friendly_name" in requested_params:
        lines.append(f"intel_friendly_name: {cfg.device_name}")
    if "intel_sink_manufacturer_name" in requested_params:
        lines.append("intel_sink_manufacturer_name: picast")
    if "intel_sink_model_name" in requested_params:
        lines.append("intel_sink_model_name: picast-trixie")
    if "intel_sink_version" in requested_params:
        lines.append("intel_sink_version: 2.0.0")
    if "intel_sink_device_URL" in requested_params:
        lines.append("intel_sink_device_URL: none")
    if "wfd_idr_request_capability" in requested_params:
        lines.append("wfd_idr_request_capability: 1")
    if "wfd_I2C" in requested_params:
        lines.append("wfd_I2C: none")
    if "wfd_standby_resume_capability" in requested_params:
        lines.append("wfd_standby_resume_capability: none")

    return "\r\n".join(lines) + ("\r\n" if lines else "")


def format_rtsp_response(
    cseq: str,
    body: str = "",
    extra_headers: list[str] | None = None,
) -> bytes:
    """Format an RTSP/1.0 200 OK response frame with accurate Content-Length."""
    headers = ["RTSP/1.0 200 OK", f"CSeq: {cseq}"]
    if extra_headers:
        headers.extend(extra_headers)
    if body:
        headers.append("Content-Type: text/parameters")
        headers.append(f"Content-Length: {len(body.encode('utf-8'))}")
    payload = "\r\n".join(headers) + "\r\n\r\n" + body
    return payload.encode("utf-8")
