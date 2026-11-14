"""Unit tests for the typed picast Python 3.13+ control plane."""

from __future__ import annotations

import asyncio
import socket
import stat
import threading
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
from src.supplicant_manager import (
    build_supplicant_config,
    parse_nm_keyfile,
    read_host_regdom,
    scan_nm_keyfiles,
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
