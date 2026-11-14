"""Unit tests for the typed picast Python 3.13+ control plane."""

from __future__ import annotations

from pathlib import Path

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
)
from src.splash import (
    PinManager,
    SplashState,
    format_ansi_splash,
    generate_wps_pin,
    render_color_block_text,
)
from src.supplicant_manager import build_supplicant_config
from src.wifi_p2p import build_dnsmasq_args, parse_orphan_p2p_interfaces, parse_station_freq


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
    assert "PICAST" in ansi
    # The PIN is rendered as 7-row color-block glyphs, not literal digits.
    pin_blocks = render_color_block_text(f"{p1[:4]} {p1[4:]}", fg_code="93")
    assert all(row in ansi for row in pin_blocks)


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
