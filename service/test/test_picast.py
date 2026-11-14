"""Unit tests for the typed picast Python 3.13+ control plane."""

from __future__ import annotations

import asyncio
import math
import random
import socket
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from src.cli import update_cmdline_kms_hotplug
from src.config import (
    CodecPreference,
    PicastConfig,
    PinMode,
    TransportMode,
    WifiBandMode,
    load_config,
    save_config,
)
from src.edid import build_wfd_video_formats, inspect_display
from src.mice_service import build_mice_txt_records, parse_mice_source_ready_port
from src.platform import (
    BoardKind,
    HostCapabilities,
    classify_board,
    resolve_effective_fps,
    resolve_p2p_ht40_allowed,
    resolve_p2p_oper_freq,
    resolve_wfd2_hevc_enabled,
)
from src.rtsp_session import (
    RtspStreamFramer,
    build_wfd_m3_response,
    extract_cseq,
    extract_session_id,
    parse_presentation_url,
    parse_uibc_port,
)
from src.splash import (
    _BLINK_DIM,
    _FB_DEVICE_COLOR,
    _FB_STAR_COLOR,
    PinManager,
    SplashState,
    Star,
    StarField,
    _fb_background,
    _fb_pattern,
    _scale_color,
    build_fb_canvas,
    fb0_geometry,
    format_ansi_splash,
    generate_wps_pin,
    render_color_block_text,
)
from src.supplicant_manager import (
    build_supplicant_config,
    parse_nm_keyfile,
    read_host_regdom,
    scan_nm_keyfiles,
)
from src.uibc import (
    BTN_LEFT,
    BTN_RIGHT,
    EV_KEY,
    EV_REL,
    HIDC_DESCRIPTORS,
    LINUX_KEY_TO_HID,
    UibcHidcBridge,
    UibcHidcState,
    discover_hid_event_paths,
    encode_keyboard_report,
    encode_mouse_report,
)
from src.wifi_p2p import build_dnsmasq_args, parse_orphan_p2p_interfaces, parse_station_freq
from src.wpa_ctrl import WpaCtrlClient, WpaCtrlMonitor, wpa_request


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "picast.conf"
    cfg = PicastConfig(
        device_name="Room-Display-42",
        transport_mode=TransportMode.BOTH,
        pin_mode=PinMode.PER_REBOOT,
        jitter_ms=55,
        codec=CodecPreference.AUTO,
    )
    save_config(cfg, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.device_name == "Room-Display-42"
    assert loaded.transport_mode is TransportMode.BOTH
    assert loaded.pin_mode is PinMode.PER_REBOOT
    assert loaded.jitter_ms == 55


def test_board_caps_and_zero2w_limits() -> None:
    assert classify_board("Raspberry Pi Zero 2 W Rev 1.0", "") is BoardKind.PI_ZERO_2_W
    assert classify_board("Raspberry Pi 5 Model B Rev 1.0", "BCM2712") is BoardKind.PI_5

    zero_caps = HostCapabilities(
        board=BoardKind.PI_ZERO_2_W,
        model_name="Raspberry Pi Zero 2 W",
        has_v4l2_h264=True,
        has_v4l2_hevc=False,
        has_5ghz_wifi=False,
        supports_ht40=False,
        recommended_max_fps=30,
    )
    pi5_caps = HostCapabilities(
        board=BoardKind.PI_5,
        model_name="Raspberry Pi 5",
        has_v4l2_h264=False,
        has_v4l2_hevc=True,
        has_5ghz_wifi=True,
        supports_ht40=True,
        recommended_max_fps=60,
    )

    cfg = PicastConfig(ht40=True, wifi_band=WifiBandMode.AUTO, max_fps="auto")
    assert resolve_p2p_oper_freq(cfg, zero_caps) == 2437
    assert not resolve_p2p_ht40_allowed(cfg, zero_caps)
    assert resolve_effective_fps(cfg, zero_caps) == 30
    assert not resolve_wfd2_hevc_enabled(cfg, zero_caps)

    assert resolve_p2p_oper_freq(cfg, pi5_caps) == 5180
    assert resolve_p2p_ht40_allowed(cfg, pi5_caps)
    assert resolve_effective_fps(cfg, pi5_caps) == 60
    assert resolve_wfd2_hevc_enabled(cfg, pi5_caps)


def test_rtsp_stream_framer_session_and_urls() -> None:
    framer = RtspStreamFramer()
    msg1 = (
        b"SET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0\r\n"
        b"CSeq: 7\r\n"
        b"Content-Length: 27\r\n\r\n"
        b"wfd_trigger_method: SETUP\r\n"
    )
    msg2 = b"GET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0\r\nCSeq: 8\r\n\r\n"
    combined = msg1 + msg2

    out1 = framer.feed(combined[:40])
    assert out1 == []
    out2 = framer.feed(combined[40:])
    assert len(out2) == 2
    assert extract_cseq(out2[0]) == "7"
    assert extract_cseq(out2[1]) == "8"

    m6_resp = "RTSP/1.0 200 OK\r\nCSeq: 3\r\nSession: 89ABCDEF;timeout=60\r\nTransport: RTP/AVP\r\n"
    assert extract_session_id(m6_resp) == "89ABCDEF"
    assert (
        parse_presentation_url(
            "wfd_presentation_URL: rtsp://192.168.173.80/wfd1.0/streamid=0",
            "1.2.3.4",
        )
        == "rtsp://192.168.173.80/wfd1.0/streamid=0"
    )

    bounded = RtspStreamFramer(max_buffer_bytes=64)
    with pytest.raises(ValueError):
        bounded.feed(b"A" * 128)


def test_wfd_m3_response_includes_h264_and_conditional_wfd2(tmp_path: Path) -> None:
    drm_dir = tmp_path / "drm"
    conn = drm_dir / "card0-HDMI-A-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n", encoding="utf-8")
    (conn / "edid").write_bytes(bytes(128))

    disp = inspect_display(drm_root=drm_dir, override_edid_path=None)
    assert disp.connected

    pi5_caps = HostCapabilities(
        board=BoardKind.PI_5,
        model_name="Raspberry Pi 5",
        has_v4l2_h264=False,
        has_v4l2_hevc=True,
        has_5ghz_wifi=True,
        supports_ht40=True,
        recommended_max_fps=60,
    )
    resp = build_wfd_m3_response(
        "wfd_video_formats\r\nwfd2_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n",
        PicastConfig(),
        pi5_caps,
        1028,
    )
    assert f"wfd_video_formats: {build_wfd_video_formats('1920x1080', 60)}" in resp
    assert "wfd_audio_codecs: AAC 00000001 00" in resp
    assert "wfd2_video_formats: 08 00 02 01 0001FFFF" in resp

    resp_30 = build_wfd_m3_response(
        "wfd2_video_formats\r\nwfd_video_formats\r\n",
        PicastConfig(max_fps="30"),
        pi5_caps,
        1028,
    )
    assert f"wfd_video_formats: {build_wfd_video_formats('1920x1080', 30)}" in resp_30
    assert "wfd2_video_formats: 08 00 02 01 00015555" in resp_30


def test_mice_source_ready_and_p2p_helpers(tmp_path: Path) -> None:
    import uuid

    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    txt = build_mice_txt_records(PicastConfig(device_name="picast"), uid)
    assert "rp=all" in txt
    assert "container_id={12345678-1234-5678-1234-567812345678}" in txt

    # MS-MICE v1 SOURCE_READY (cmd=1) + TLV tag=2 (RTSP port=7238).
    tlv = bytes([2, 0, 2]) + (7238).to_bytes(2, "big")
    frame = (4 + len(tlv)).to_bytes(2, "big") + bytes([1, 1]) + tlv
    assert parse_mice_source_ready_port(frame) == 7238
    assert parse_mice_source_ready_port(b"\x00\x04\x01\x02") is None

    iw_out = "\tInterface p2p-dev-wlan0\n\tInterface p2p-wlan0-0\n\tInterface wlan0\n"
    assert parse_orphan_p2p_interfaces(iw_out) == ["p2p-wlan0-0"]
    args = build_dnsmasq_args("p2p-wlan0-0", tmp_path / "leases")
    assert "--interface=p2p-wlan0-0" in args

    wpa_status = "bssid=02:00:00:00:00:01\nssid=ExampleNet\nfreq=2457\nwpa_state=COMPLETED\n"
    assert parse_station_freq(wpa_status) == 2457
    assert parse_station_freq("wpa_state=DISCONNECTED\n") is None
    assert parse_station_freq("") is None


def test_supplicant_config_build() -> None:
    conf = build_supplicant_config("ExampleNet", "hunter22", "picast")
    assert "ssid=\"ExampleNet\"" in conf
    assert "psk=\"hunter22\"" in conf
    assert "p2p_go_intent=15" in conf
    assert "device_type=7-0050F204-1" in conf
    assert "DIR=/run/picast/wpa" in conf

    open_conf = build_supplicant_config("OpenNet", None, "picast")
    assert "key_mgmt=NONE" in open_conf
    assert "psk=" not in open_conf


def test_pin_per_reboot_persistence_and_splash(tmp_path: Path) -> None:
    pin_file = tmp_path / "pin"
    mgr1 = PinManager(PinMode.PER_REBOOT, runtime_path=pin_file)
    p1 = mgr1.initialize()
    assert p1 is not None and len(p1) == 8 and p1.isdigit()

    mgr2 = PinManager(PinMode.PER_REBOOT, runtime_path=pin_file)
    assert mgr2.initialize() == p1
    assert mgr2.on_session_ended() == p1

    mgr3 = PinManager(PinMode.PER_SESSION, runtime_path=pin_file)
    first = mgr3.initialize()
    second = mgr3.on_session_ended()
    assert first is not None and second is not None and len(generate_wps_pin()) == 8

    ansi = format_ansi_splash(
        SplashState(
            device_name="LIVINGROOM",
            transport_label="P2P + MICE",
            pin_display=p1,
            status_line="READY",
        )
    )
    assert "P I C A S T" in ansi
    assert "LIVINGROOM" in ansi
    assert "READY" in ansi
    assert "P2P + MICE" in ansi
    # The PIN is rendered as 7-row color-block glyphs, not literal digits.
    pin_blocks = render_color_block_text(f"{p1[:4]} {p1[4:]}", fg_code="38;2;255;210;82")
    assert all(row in ansi for row in pin_blocks)


_FB_BG_PIXEL = b"\x1a\x12\x0c\xff"
_FB_AMBER_PIXEL = b"\x52\xd2\xff\xff"
_FB_WHITE_PIXEL = b"\xf4\xed\xe8\xff"
_FB_SLATE_PIXEL = b"\xb8\xa3\x94\xff"
_FB_DIM_PIXEL = b"\x76\x66\x5a\xff"


_FB_BG_PIXEL_16 = _fb_pattern(_FB_BG_PIXEL, 16)


def _splash_canvas_state() -> SplashState:
    return SplashState(
        device_name="LIVINGROOM",
        transport_label="P2P + MICE",
        pin_display="12345678",
        status_line="READY",
    )


def _pixel_at(canvas: bytearray, width: int, x: int, y: int, bpp: int = 32) -> bytes:
    pp = bpp // 8
    off = (y * width + x) * pp
    return bytes(canvas[off : off + pp])


def _row_has(canvas: bytearray, width: int, y: int, pixel: bytes, bpp: int = 32) -> bool:
    pp = bpp // 8
    row = canvas[y * width * pp : (y + 1) * width * pp]
    return pixel in row


def test_build_fb_canvas_geometry_and_palette_1080p() -> None:
    canvas = build_fb_canvas(_splash_canvas_state(), 1920, 1080)
    assert len(canvas) == 1920 * 1080 * 4

    # Deep charcoal-navy field, no muddy brown remnants.
    assert _pixel_at(canvas, 1920, 0, 0) == _FB_BG_PIXEL
    assert _pixel_at(canvas, 1920, 1919, 1079) == _FB_BG_PIXEL
    assert _FB_BG_PIXEL in canvas[:1920]

    # Amber lives in the top third (accent rule) and middle band (PIN hero).
    assert any(_row_has(canvas, 1920, y, _FB_AMBER_PIXEL) for y in range(0, 1080 // 3))
    assert any(
        _row_has(canvas, 1920, y, _FB_AMBER_PIXEL) for y in range(1080 // 3, 2 * 1080 // 3)
    )
    # Warm-white device name in the lower-middle band.
    assert any(
        _row_has(canvas, 1920, y, _FB_WHITE_PIXEL) for y in range(1080 // 2, 3 * 1080 // 4)
    )
    # Muted-slate status in the bottom quarter.
    assert any(
        _row_has(canvas, 1920, y, _FB_SLATE_PIXEL) for y in range(3 * 1080 // 4, 1080)
    )
    # Dim wordmark in the top area.
    assert any(_row_has(canvas, 1920, y, _FB_DIM_PIXEL) for y in range(0, 1080 // 3))
    # Hero is by far the most present color on screen.
    assert canvas.count(_FB_AMBER_PIXEL) > canvas.count(_FB_WHITE_PIXEL)
    assert canvas.count(_FB_AMBER_PIXEL) > canvas.count(_FB_SLATE_PIXEL)


def test_build_fb_canvas_16bpp_rgb565() -> None:
    """16-bpp canvases are packed RGB565 (Pi Zero 2 W's vc4-hdmi framebuffer)."""
    width, height = 640, 480
    canvas = build_fb_canvas(_splash_canvas_state(), width, height, bpp=16)
    assert len(canvas) == width * height * 2
    assert _pixel_at(canvas, width, 0, 0, 16) == _FB_BG_PIXEL_16
    assert _pixel_at(canvas, width, width - 1, height - 1, 16) == _FB_BG_PIXEL_16
    # Amber hero still present in the middle band after 16-bit packing.
    amber16 = _fb_pattern(_FB_AMBER_PIXEL, 16)
    assert any(_row_has(canvas, width, y, amber16, 16) for y in range(height // 3, 2 * height // 3))
    # Unsupported depths are rejected rather than silently corrupting the frame.
    with pytest.raises(ValueError):
        build_fb_canvas(_splash_canvas_state(), width, height, bpp=8)


@pytest.mark.parametrize(
    ("width", "height"), [(640, 480), (1280, 720), (1920, 1080)]
)
def test_build_fb_canvas_scales_across_resolutions(width: int, height: int) -> None:
    canvas = build_fb_canvas(_splash_canvas_state(), width, height)
    assert len(canvas) == width * height * 4
    assert _pixel_at(canvas, width, 0, 0) == _FB_BG_PIXEL
    assert _pixel_at(canvas, width, width - 1, height - 1) == _FB_BG_PIXEL
    # PIN hero and status must survive every supported resolution.
    assert any(
        _row_has(canvas, width, y, _FB_AMBER_PIXEL) for y in range(height // 3, 2 * height // 3)
    )
    assert any(
        _row_has(canvas, width, y, _FB_SLATE_PIXEL) for y in range(3 * height // 4, height)
    )


def test__scale_color_identity_half_clamp_alpha() -> None:
    # A factor near 1.0 must be (visually) identity.
    amber = _scale_color(b"\x52\xd2\xff\xff", 1.0)
    assert amber == b"\x52\xd2\xff\xff"
    # 0.5 halves every RGB channel; alpha untouched.
    assert _scale_color(b"\x52\xd2\xff\xff", 0.5) == b"\x29\x69\x80\xff"
    # Clamping at 255 for overflow.
    assert _scale_color(b"\xff\xff\xff\xff", 2.0) == b"\xff\xff\xff\xff"
    # 0 factor zeroes RGB, keeps alpha.
    assert _scale_color(b"\x52\xd2\xff\xff", 0.0) == b"\x00\x00\x00\xff"


def test_build_fb_canvas_blink_phase() -> None:
    """The WIN + K band blinks hard between warm white and dim gray."""
    state = _splash_canvas_state()
    bright = build_fb_canvas(state, 1280, 720, phase=0.0)
    dim = build_fb_canvas(state, 1280, 720, phase=0.5)
    assert bright != dim

    # Locate the WIN + K band at full brightness: pixels equal to _FB_DEVICE_COLOR.
    white_offsets = {i for i in range(0, len(bright), 4) if bright[i : i + 4] == _FB_DEVICE_COLOR}
    assert white_offsets

    dim_offset = _scale_color(_FB_DEVICE_COLOR, _BLINK_DIM)
    for i in white_offsets:
        # Only WIN + K pixels dim; device-name pixels (also white) stay bright.
        if bright[i : i + 4] != dim[i : i + 4]:
            assert dim[i : i + 4] == dim_offset

    diff_offsets = {i for i in range(0, len(bright), 4) if bright[i : i + 4] != dim[i : i + 4]}
    assert diff_offsets
    assert diff_offsets <= white_offsets

    # Binary, not a fade: every phase in the first half renders identically,
    # as does every phase in the second half.
    for early, late in ((0.05, 0.49), (0.55, 0.99)):
        assert build_fb_canvas(state, 1280, 720, phase=early) == build_fb_canvas(
            state, 1280, 720, phase=late
        )

    # Corner background pixels unchanged in both renders.
    for x, y in ((0, 0), (1279, 719)):
        assert _pixel_at(bright, 1280, x, y) == _FB_BG_PIXEL
        assert _pixel_at(dim, 1280, x, y) == _FB_BG_PIXEL


def test_build_fb_canvas_stars_mid_life() -> None:
    """A deterministic seeded star changes bg pixels to the expected scaled tint."""
    width, height = 640, 480
    field = StarField(width, height, random.Random(7), max_stars=4)
    star = Star(x=123, y=45, size_px=1, age_s=0.5, lifetime_s=2.0, peak=0.4, born=0.0)
    field.stars.append(star)

    canvas = bytearray(_fb_background(width, height))
    field.draw(canvas, width, height, now_s=0.5)

    brightness = 0.4 * math.sin(math.pi * 0.5 / 2.0)
    expected = _scale_color(_FB_STAR_COLOR, brightness)
    off = (45 * width + 123) * 4
    assert bytes(canvas[off : off + 4]) == expected


def test_build_fb_canvas_stars_bounds() -> None:
    """A star at x=width-1 with size_px=2 must not write past the canvas."""
    width, height = 640, 480
    field = StarField(width, height, random.Random(3), max_stars=2)
    field.stars.append(
        Star(x=width - 1, y=height - 1, size_px=2, age_s=1.0, lifetime_s=2.0, peak=0.45, born=0.0)
    )

    canvas = bytearray(_fb_background(width, height))
    field.draw(canvas, width, height, now_s=1.0)
    assert len(canvas) == width * height * 4
    assert _pixel_at(canvas, width, width - 1, height - 1) != _FB_BG_PIXEL


def test_starfield_tick_spawns_expiry_and_birth_dimness() -> None:
    """tick() drops expired stars, spawns up to max_stars, born stars render invisible."""
    rng = random.Random(1234)
    field = StarField(640, 480, rng, max_stars=3, spawn_per_tick=2)
    # Each tick spawns up to `spawn_per_tick`, so two ticks fill the cap.
    field.tick(0.0)
    assert len(field.stars) == 2
    field.tick(0.0)
    assert len(field.stars) == 3
    for star in field.stars:
        assert 0 <= star.x < 640
        assert 0 <= star.y < 480
        assert star.size_px in (2, 3)
        assert 1.2 <= star.lifetime_s <= 2.6
        assert 0.45 <= star.peak <= 0.95
        assert star.born == 0.0

    # A brightness-zero draw at exactly born == now leaves the background untouched.
    bg = bytearray(_fb_background(640, 480))
    field.draw(bg, 640, 480, now_s=0.0)
    assert bg == bytearray(_fb_background(640, 480))

    # Let everything expire; only spawn_per_tick new stars should appear.
    field.tick(10.0)
    assert len(field.stars) == 2
    assert all(star.born == 10.0 for star in field.stars)

    # A deterministic mid-life star survives the tick and fades smoothly.
    field.stars.clear()
    field.stars.append(Star(x=10, y=10, size_px=1, age_s=0.0, lifetime_s=2.0, peak=0.4, born=9.0))
    field.tick(10.0)
    assert len(field.stars) == 3
    assert field.stars[0].x == 10
    assert field.stars[0].age_s == 1.0

    canvas = bytearray(_fb_background(640, 480))
    field.draw(canvas, 640, 480, now_s=10.0)
    assert _pixel_at(canvas, 640, 10, 10) == _scale_color(_FB_STAR_COLOR, 0.4)


def test_starfield_default_max_stars_caps() -> None:
    assert StarField(640, 480, random.Random(5)).max_stars == 16
    assert StarField(1920, 1080, random.Random(5)).max_stars == (1920 * 1080) // 30_000


def test_build_fb_canvas_defaults_static(tmp_path: Path) -> None:
    """Defaults (phase=0, no stars) render byte-identical to the pre-animation layout."""
    state = _splash_canvas_state()
    c1 = build_fb_canvas(state, 1920, 1080)
    c2 = build_fb_canvas(state, 1920, 1080, phase=0.0, starfield=None)
    assert c1 == c2


def test_cmdline_kms_hotplug_toggle() -> None:
    orig = "console=serial0,115200 root=PARTUUID=1234 rw rootwait\n"
    enabled = update_cmdline_kms_hotplug(orig, enable=True, mode="1920x1080@60D")
    assert "vc4.force_hotplug=1" in enabled
    assert "video=HDMI-A-1:1920x1080@60D" in enabled
    hdmi2 = update_cmdline_kms_hotplug(
        orig, enable=True, mode="1920x1080@60D", connector="HDMI-A-2"
    )
    assert "video=HDMI-A-2:1920x1080@60D" in hdmi2
    disabled = update_cmdline_kms_hotplug(enabled, enable=False, mode="1920x1080@60D")
    assert "vc4.force_hotplug" not in disabled
    assert "console=serial0,115200" in disabled
    with pytest.raises(ValueError):
        update_cmdline_kms_hotplug(orig, enable=True, mode="bad mode; rm -rf")


class _FakeWpaServer:
    """Minimal `AF_UNIX`/`SOCK_DGRAM` stand-in for a wpa_ctrl control socket."""

    def __init__(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self._client_addr: tuple[str, int] | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                data, addr = self.sock.recvfrom(4096)
                self._client_addr = addr
                cmd = data.decode("utf-8", errors="replace").strip()
                verb = cmd.split(" ", 1)[0]
                if not verb.isupper():
                    self.sock.sendto(b"UNKNOWN COMMAND\n", addr)
                elif cmd == "PING":
                    self.sock.sendto(b"PONG\n", addr)
                elif cmd == "STATUS":
                    self.sock.sendto(b"wpa_state=COMPLETED\n", addr)
                elif cmd.startswith("SET "):
                    self.sock.sendto(b"OK\n" if cmd == "SET wifi_display 1" else b"FAIL\n", addr)
                else:
                    self.sock.sendto(b"OK\n", addr)
        except OSError:
            return  # socket closed by the test

    def emit(self, payload: bytes) -> None:
        """Push an unsolicited event datagram to the last connected client."""
        if self._client_addr is not None:
            self.sock.sendto(payload, self._client_addr)

    def close(self) -> None:
        self.sock.close()
        self._thread.join(timeout=2.0)


def test_nm_keyfile_ssid_psk_parsing(tmp_path: Path) -> None:
    text = (
        "[connection]\n"
        "id=home\n"
        "\n"
        "[wifi]\n"
        'ssid="Home Net"\n'
        "mode=infrastructure\n"
        "\n"
        "[802-11-wireless-security]\n"
        'psk="secret pass"\n'
        "\n"
        "[ipv4]\n"
        "method=auto\n"
    )
    parsed = parse_nm_keyfile(text)
    assert parsed["wifi"]["ssid"] == "Home Net"
    assert parsed["802-11-wireless-security"]["psk"] == "secret pass"
    assert parsed["connection"]["id"] == "home"

    conn_dir = tmp_path / "system-connections"
    conn_dir.mkdir()
    # First keyfile lacks the keys entirely; parser must keep scanning.
    (conn_dir / "guest.nmconnection").write_text(
        "[connection]\nid=guest\n[wifi]\nmode=infrastructure\n", encoding="utf-8"
    )
    (conn_dir / "home.nmconnection").write_text(text, encoding="utf-8")
    assert scan_nm_keyfiles((str(conn_dir),), ("wifi", "802-11-wireless"), "ssid") == "Home Net"
    assert (
        scan_nm_keyfiles((str(conn_dir),), ("802-11-wireless-security",), "psk")
        == "secret pass"
    )
    # PSK must not be picked up from a foreign group, and absent dirs yield None.
    assert scan_nm_keyfiles((str(conn_dir),), ("wifi",), "psk") is None
    assert scan_nm_keyfiles((str(tmp_path / "absent"),), ("wifi",), "ssid") is None


def test_read_host_regdom_and_supplicant_country(tmp_path: Path) -> None:
    regdom = tmp_path / "ieee80211_regdom"
    regdom.write_text("DE\n", encoding="utf-8")
    assert read_host_regdom(regdom) == "DE"
    # World regulatory domain ("00") and missing sysfs both default to US.
    regdom.write_text("00", encoding="utf-8")
    assert read_host_regdom(regdom) == "US"
    assert read_host_regdom(tmp_path / "missing") == "US"

    assert "country=DE\n" in build_supplicant_config("ExampleNet", None, "picast", country="DE")
    assert "country=US\n" in build_supplicant_config("ExampleNet", None, "picast")


def test_wpa_ctrl_client_roundtrip(tmp_path: Path) -> None:
    server = _FakeWpaServer(tmp_path / "wlan0")
    try:
        with WpaCtrlClient(tmp_path, "wlan0") as client:
            assert client.request("PING") == "PONG"
            # Lowercase verb must be uppercased on the wire while keeping args lowercase.
            assert client.request("status") == "wpa_state=COMPLETED"
            assert client.request("set wifi_display 1") == "OK"
            # Private client socket binds inside ctrl_dir with 0o600 (wpa_cli parity).
            locals_ = list(tmp_path.glob("picast-*"))
            assert len(locals_) == 1
            assert stat.S_IMODE(locals_[0].stat().st_mode) == 0o600
        # Closing unlinks the private socket file.
        assert list(tmp_path.glob("picast-*")) == []
    finally:
        server.close()


def test_wpa_ctrl_client_missing_socket(tmp_path: Path) -> None:
    with (
        pytest.raises(FileNotFoundError),
        WpaCtrlClient(tmp_path, "wlan0"),
    ):
        pass  # pragma: no cover - __enter__ raises before the body


def test_wpa_request_direct_and_missing(tmp_path: Path) -> None:
    server = _FakeWpaServer(tmp_path / "wlan0")
    try:
        assert wpa_request(tmp_path, "wlan0", "PING") == "PONG"
    finally:
        server.close()
    # Direct path fails on the absent socket; the wpa_cli fallback (when the
    # binary even exists) cannot connect either -> None, never an exception.
    assert wpa_request(tmp_path / "absent", "wlan0", "PING") is None


def test_wpa_ctrl_monitor_attach_and_events(tmp_path: Path) -> None:
    server = _FakeWpaServer(tmp_path / "global")
    try:

        async def scenario() -> None:
            monitor = WpaCtrlMonitor(tmp_path, "global")
            assert await monitor.attach()
            server.emit(b"<3>AP-STA-DISCONNECTED 02:00:00:00:00:01\n")
            event = await monitor.recv_event(timeout_s=2.0)
            assert event is not None and event.startswith("<3>AP-STA-DISCONNECTED")
            # Quiet socket: recv_event times out to None (periodic re-arm path).
            assert await monitor.recv_event(timeout_s=0.05) is None
            await monitor.close()
            assert list(tmp_path.glob("picast-mon-*")) == []

        asyncio.run(scenario())
    finally:
        server.close()


def test_wfd_m3_exact_param_set_matching() -> None:
    zero_caps = HostCapabilities(
        board=BoardKind.PI_ZERO_2_W,
        model_name="Raspberry Pi Zero 2 W",
        has_v4l2_h264=True,
        has_v4l2_hevc=False,
        has_5ghz_wifi=False,
        supports_ht40=False,
        recommended_max_fps=30,
    )
    # Requesting only the WFD 2.0 parameter must NOT emit the H.264 parameter
    # (and vice versa): matching is by exact line, never by substring.
    resp_wfd2 = build_wfd_m3_response("wfd2_video_formats\r\n", PicastConfig(), zero_caps, 1028)
    assert "wfd2_video_formats: none" in resp_wfd2
    assert "wfd_video_formats:" not in resp_wfd2

    resp_wfd1 = build_wfd_m3_response("wfd_video_formats\r\n", PicastConfig(), zero_caps, 1028)
    assert resp_wfd1.startswith("wfd_video_formats: ")
    assert "wfd2_video_formats" not in resp_wfd1

    # Empty / whitespace-only request bodies produce an empty response.
    assert build_wfd_m3_response("\r\n   \r\n", PicastConfig(), zero_caps, 1028) == ""


def test_audio_codec_default_and_validation() -> None:
    # Default matches the M3 advertisement `wfd_audio_codecs: AAC 00000001 00`.
    assert PicastConfig().audio_codec == "aac"
    assert PicastConfig(audio_codec="lpcm").validated().audio_codec == "lpcm"
    assert PicastConfig(audio_codec="AAC").validated().audio_codec == "aac"
    # Unknown values keep failing closed to LPCM (valid legacy default).
    assert PicastConfig(audio_codec="opus").validated().audio_codec == "lpcm"


def test_uibc_enabled_config_roundtrip(tmp_path: Path) -> None:
    assert PicastConfig().uibc_enabled is True
    cfg_path = tmp_path / "picast.conf"
    save_config(PicastConfig(uibc_enabled=False), cfg_path)
    assert load_config(cfg_path).uibc_enabled is False
    # Raw INI truthy spellings normalize through the shared bool parser.
    cfg_path.write_text("[picast]\nuibc_enabled = yes\n", encoding="utf-8")
    assert load_config(cfg_path).uibc_enabled is True


def test_parse_uibc_port_variants() -> None:
    body = (
        "wfd_trigger_method: SETUP\r\n"
        "wfd_uibc_capability: input_category_list=GENERIC, HIDC;"
        "generic_cap_list=Keyboard, Mouse;"
        "hidc_cap_list=Keyboard/USB, Mouse/USB;"
        "port=1234\r\n"
    )
    assert parse_uibc_port("SET_PARAMETER rtsp://x RTSP/1.0\r\nCSeq: 3\r\n\r\n" + body) == 1234
    # `port=none` (source not offering UIBC), blank, and out-of-range -> None.
    assert parse_uibc_port(body.replace("port=1234", "port=none")) is None
    assert parse_uibc_port(body.replace("port=1234", "port=")) is None
    assert parse_uibc_port(body.replace("port=1234", "port=70000")) is None
    # Missing wfd_uibc_capability line entirely -> None.
    assert parse_uibc_port("SET_PARAMETER RTSP/1.0\r\n\r\nwfd_trigger_method: SETUP\r\n") is None


def test_wfd_m3_uibc_capability_advertisement() -> None:
    zero_caps = HostCapabilities(
        board=BoardKind.PI_ZERO_2_W,
        model_name="Raspberry Pi Zero 2 W",
        has_v4l2_h264=True,
        has_v4l2_hevc=False,
        has_5ghz_wifi=False,
        supports_ht40=False,
        recommended_max_fps=30,
    )
    resp_on = build_wfd_m3_response(
        "wfd_uibc_capability\r\n",
        PicastConfig(uibc_enabled=True),
        zero_caps,
        1028,
        allow_uibc=True,
    )
    assert "wfd_uibc_capability: input_category_list=GENERIC, HIDC;" in resp_on
    assert "generic_cap_list=Keyboard, Mouse;" in resp_on
    assert "hidc_cap_list=Keyboard/USB, Mouse/USB;" in resp_on
    assert "port=none" in resp_on

    # With UIBC disabled (or default `allow_uibc=False`) the capability is `none`.
    resp_off = build_wfd_m3_response(
        "wfd_uibc_capability\r\n", PicastConfig(), zero_caps, 1028
    )
    assert resp_off == "wfd_uibc_capability: none\r\n"
    resp_disabled = build_wfd_m3_response(
        "wfd_uibc_capability\r\n",
        PicastConfig(uibc_enabled=False),
        zero_caps,
        1028,
        allow_uibc=False,
    )
    assert resp_disabled == "wfd_uibc_capability: none\r\n"


def test_hidc_descriptor_layout_and_key_table() -> None:
    assert len(HIDC_DESCRIPTORS) == 6
    for descriptor in HIDC_DESCRIPTORS:
        assert descriptor[:2] == b"\x10\x01"
        # Byte 2-3 is the descriptor's own length, matching upstream d2.py.
        assert int.from_bytes(descriptor[2:4], "big") == len(descriptor)
    assert LINUX_KEY_TO_HID[30] == 0x04  # KEY_A -> HID usage 'a'
    assert LINUX_KEY_TO_HID[14] == 0x2A  # KEY_BACKSPACE
    assert LINUX_KEY_TO_HID[106] == 0x4F  # KEY_RIGHT -> HID right arrow


def test_uibc_hidc_keyboard_encoding() -> None:
    state = UibcHidcState()
    # Press left shift (code 42): modifier bit 1<<1, usage 0 (upstream parity).
    pressed = state.translate(EV_KEY, 42, 1)[0]
    assert len(pressed) == 18
    expected = bytes.fromhex("00010012010000000929") + bytes((0x02, 0x00, 0x00)) + b"\x00" * 5
    assert pressed == expected
    # Press KEY_A (30) while shift held: usage 0x04 with sticky modifier mask.
    pressed_a = state.translate(EV_KEY, 30, 1)[0]
    assert pressed_a[10] == 0x02 and pressed_a[11] == 0x00 and pressed_a[12] == 0x04
    # Release KEY_A: usage resets to 0, modifier bitmap persists.
    released_a = state.translate(EV_KEY, 30, 0)[0]
    assert released_a[10] == 0x02 and released_a[12] == 0x00
    # Release left shift: modifier bit cleared.
    released_shift = state.translate(EV_KEY, 42, 0)[0]
    assert released_shift[10] == 0x00


def test_uibc_hidc_mouse_encoding() -> None:
    state = UibcHidcState()
    # BTN_LEFT press sets button bit 1 in the 16-byte mouse report.
    pkt = state.translate(EV_KEY, BTN_LEFT, 1)[0]
    assert len(pkt) == 16
    assert pkt[10] == 0x01
    assert pkt[14:] == b"\x00\x00"
    # REL_X clamps at +127 so fast motion never wraps to negative.
    pkt = state.translate(EV_REL, 0, 200)[0]
    assert pkt[10] == 0x01 and pkt[11] == 127
    # ... and at -127 (two's complement 0x81).
    pkt = state.translate(EV_REL, 0, -200)[0]
    assert pkt[11] == 0x81
    # REL_Y rides the third payload byte while dx stays 0.
    pkt = state.translate(EV_REL, 1, 5)[0]
    assert pkt[11] == 0 and pkt[12] == 5
    # REL_WHEEL (code 8) is signed: -3 -> 0xFD, +1 -> 0x01.
    pkt = state.translate(EV_REL, 8, -3)[0]
    assert pkt[13] == 0xFD
    pkt = state.translate(EV_REL, 8, 1)[0]
    assert pkt[13] == 0x01
    # BTN_RIGHT adds bit 2 alongside the still-held left button.
    pkt = state.translate(EV_KEY, BTN_RIGHT, 1)[0]
    assert pkt[10] == 0x03
    # Non-mouse-button key codes >= 272 (e.g. BTN_SIDE=275) emit nothing.
    assert state.translate(EV_KEY, 275, 1) == []
    # EV_SYN and unknown event types emit nothing.
    assert state.translate(0, 0, 0) == []


def test_uibc_hidc_encoder_helpers() -> None:
    kb = encode_keyboard_report(0x05, 0x04)
    assert kb == bytes.fromhex("00010012010000000929") + bytes((0x05, 0x00, 0x04)) + b"\x00" * 5
    assert len(kb) == 18
    mouse = encode_mouse_report(0x01, dx=3, dy=0, wheel=0)
    assert mouse == bytes.fromhex("00010010010100000628") + bytes((0x01, 3, 0, 0)) + b"\x00" * 2
    assert len(mouse) == 16
    # Wheel of exactly -1/+1 matches upstream's 0xFF/0x01 wire bytes.
    assert encode_mouse_report(0, wheel=-1)[13] == 0xFF
    assert encode_mouse_report(0, wheel=1)[13] == 0x01


def test_discover_hid_event_paths_matches_kbd_mouse_ignores_hdmi(tmp_path: Path) -> None:
    sys_root = tmp_path / "class" / "input"
    dev_root = tmp_path / "dev" / "input"

    def make_event(name: str, key: str, rel: str) -> None:
        caps = sys_root / name / "device" / "capabilities"
        caps.mkdir(parents=True)
        (caps / "key").write_text(key, encoding="ascii")
        (caps / "rel").write_text(rel, encoding="ascii")

    zero_word = "0000000000000000"
    # USB keyboard (padded 64-bit words): KEY_A = bit 30 in the rightmost word.
    make_event("event0", " ".join([zero_word] * 11) + " 0000000040000000", "0")
    # USB mouse: BTN_LEFT = bit 272 (64-bit word 4, bit 16) plus rel bits 0x3.
    mouse_key = "0000000000010000 " + " ".join(["0"] * 4)
    make_event("event1", mouse_key, " ".join([zero_word] * 11) + " 0000000000000003")
    # Same mouse rendered unpadded (kernel trims leading zero words).
    make_event("event2", "10000 0 0 0 0", "3")
    # vc4-hdmi style HDMI audio node: rel=3 but neither KEY_A nor BTN_LEFT.
    make_event("event3", "0", "3")

    dev_root.mkdir(parents=True)
    for name in ("event0", "event1", "event2"):
        (dev_root / name).touch()

    discovered = discover_hid_event_paths(sys_root, dev_root)
    assert discovered == [dev_root / "event0", dev_root / "event1", dev_root / "event2"]
    # Missing sysfs root (or missing /dev/input counterparts) discovers nothing.
    assert discover_hid_event_paths(tmp_path / "absent", dev_root) == []


def test_uibc_bridge_loopback_handshake_and_graceful_refusal(tmp_path: Path) -> None:
    async def scenario() -> None:
        received = bytearray()
        got_descriptors = asyncio.Event()
        total = sum(len(descriptor) for descriptor in HIDC_DESCRIPTORS)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            _ = writer
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                received.extend(chunk)
                if len(received) >= total:
                    got_descriptors.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        assert server.sockets is not None
        port = int(server.sockets[0].getsockname()[1])
        bridge = UibcHidcBridge(
            "127.0.0.1",
            port,
            sys_input_root=tmp_path / "absent-sys",
            dev_input_root=tmp_path / "absent-dev",
        )
        try:
            assert await bridge.start() is True
            await asyncio.wait_for(got_descriptors.wait(), timeout=3.0)
            # Exact upstream descriptor handshake (m1..m6) arrived over TCP.
            assert bytes(received) == b"".join(HIDC_DESCRIPTORS)
        finally:
            bridge.close()
            server.close()
            await server.wait_closed()

        # A refused TCP port degrades gracefully to False without raising.
        dead = UibcHidcBridge(
            "127.0.0.1",
            port,
            sys_input_root=tmp_path,
            dev_input_root=tmp_path,
            connect_attempts=2,
            connect_delay_s=0.01,
        )
        assert await dead.start() is False
        dead.close()

    asyncio.run(scenario())


class _StubEventLoop:
    """Minimal reader-recording loop stand-in for rescan tests.

    A real selector event loop cannot epoll regular tmp files (the fds here
    are plain files, not input char devices), so record registrations instead.
    """

    def __init__(self) -> None:
        self.registered: set[int] = set()

    def add_reader(self, fd: int, callback: Callable[..., object], *args: object) -> None:
        # Mimic the real loop: same fd may not be registered twice.
        assert fd not in self.registered, f"fd {fd} already registered"
        self.registered.add(fd)

    def remove_reader(self, fd: int) -> bool:
        if fd in self.registered:
            self.registered.remove(fd)
            return True
        return False


def test_uibc_bridge_rescan_grabs_hot_plugged_devices_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        sys_root = tmp_path / "class" / "input"
        dev_root = tmp_path / "dev" / "input"
        dev_root.mkdir(parents=True)

        def make_event(name: str, key: str, rel: str) -> None:
            caps = sys_root / name / "device" / "capabilities"
            caps.mkdir(parents=True)
            (caps / "key").write_text(key, encoding="ascii")
            (caps / "rel").write_text(rel, encoding="ascii")

        bridge = UibcHidcBridge("127.0.0.1", 1, sys_input_root=sys_root, dev_input_root=dev_root)
        # A real selector loop refuses to epoll regular tmp files, so use
        # a reader-recording stub instead of the running loop.
        stub_loop = _StubEventLoop()
        bridge._loop = cast(asyncio.AbstractEventLoop, stub_loop)
        bridge._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Nothing plugged at M4 time: the rescan finds nothing.
            assert bridge.rescan_for_new_devices() == 0

            # A keyboard and a mouse are hot-plugged after the negotiation.
            zero_word = "0000000000000000"
            # USB keyboard: KEY_A = bit 30 in the rightmost 64-bit word.
            make_event("event0", " ".join([zero_word] * 11) + " 0000000040000000", "0")
            # USB mouse: BTN_LEFT (word 4, bit 16) plus rel bits 0x3.
            make_event(
                "event1",
                "0000000000010000 0 0 0 0",
                " ".join([zero_word] * 11) + " 0000000000000003",
            )
            (dev_root / "event0").touch()
            (dev_root / "event1").touch()

            assert bridge.rescan_for_new_devices() == 2
            # Repeated rescans are no-ops: no duplicate grabs, count stable.
            assert bridge.rescan_for_new_devices() == 2
            assert bridge.rescan_for_new_devices() == 2
            assert len(bridge._devices) == 2
            assert {device.path for device in bridge._devices} == {
                dev_root / "event0",
                dev_root / "event1",
            }
            # Exactly one reader per grabbed device, kept registered.
            assert stub_loop.registered == {bridge._devices[0].fd, bridge._devices[1].fd}
        finally:
            bridge.close()
        assert stub_loop.registered == set()

    asyncio.run(scenario())


def test_fb_pattern_32bpp_identity() -> None:
    """32-bpp framebuffers receive BGRA pixels unchanged."""
    assert _fb_pattern(b"\x00\x11\x22\xff", 32) == b"\x00\x11\x22\xff"


def test_fb_pattern_16bpp_rgb565() -> None:
    """16-bpp (Pi Zero 2 W vc4-hdmi) packs little-endian RGB565."""
    assert _fb_pattern(b"\x00\x00\x00\xff", 16) == b"\x00\x00"  # black
    assert _fb_pattern(b"\xff\xff\xff\xff", 16) == b"\xff\xff"  # white
    assert _fb_pattern(b"\x00\x00\xff\xff", 16) == b"\x00\xf8"  # pure red
    assert _fb_pattern(_FB_BG_PIXEL, 16) == _FB_BG_PIXEL_16
    # Amber hero PIN color: 0xFE8A when packed little-endian.
    assert _fb_pattern(_FB_AMBER_PIXEL, 16) == b"\x8a\xfe"


def test_fb0_geometry_missing_device_returns_none() -> None:
    assert fb0_geometry(Path("/nonexistent/fb0")) is None
