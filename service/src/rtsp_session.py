"""Asyncio RTSP M1-M7 / WFD 2.0 state machine, TCP Content-Length framer, and IDR watchdog."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from .config import CodecPreference, PicastConfig, SyncMode
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

logger = logging.getLogger(__name__)
MAX_RTSP_BUFFER_BYTES: Final[int] = 65536
_CSEQ_RE = re.compile(rb"CSeq:\s*(\d+)", re.IGNORECASE)
_SESSION_RE = re.compile(r"Session:\s*([^\s;]+)", re.IGNORECASE)
_PRES_URL_RE = re.compile(r"wfd_presentation_URL:\s*([^\s\r\n]+)", re.IGNORECASE)
_TRANSPORT_PORTS_RE = re.compile(r"client_port=(\d+)(?:-(\d+))?")


class SessionEventSink(Protocol):
    """Lifecycle hooks fired by an active RTSP Miracast session."""

    async def on_session_started(self, peer_ip: str) -> None: ...
    async def on_session_ended(self, peer_ip: str) -> None: ...


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


@dataclass(slots=True)
class PlayerProcessRunner:
    """Launches and monitors the native C++26 `picast-player` executable."""

    binary_path: str = "picast-player"
    _proc: asyncio.subprocess.Process | None = None

    async def start(
        self,
        cfg: PicastConfig,
        rtp_port: int,
        video_codec: str,
        idr_port: int | None = None,
    ) -> bool:
        resolved = shutil.which(self.binary_path) or "/usr/bin/picast-player"
        if not Path(resolved).exists():
            logger.error("picast-player binary not found at %s", resolved)
            return False

        effective_idr_port = idr_port if idr_port is not None else cfg.idr_ipc_port
        args = [
            resolved,
            "--rtp-port",
            str(rtp_port),
            "--idr-port",
            str(effective_idr_port),
            "--latency-ms",
            str(cfg.jitter_ms),
            "--codec",
            video_codec,
            "--decoder",
            cfg.decoder,
            "--audio-sink",
            cfg.audio_sink.value,
            "--low-latency" if cfg.sync_mode is SyncMode.LOW_LATENCY else "--av-sync",
        ]
        if not cfg.audio_enabled:
            args.append("--no-audio")

        try:
            self._proc = await asyncio.create_subprocess_exec(*args)
            logger.info("Started picast-player (pid=%s, codec=%s)", self._proc.pid, video_codec)
            return True
        except OSError as exc:
            logger.error("Failed launching picast-player: %s", exc)
            self._proc = None
            return False

    async def wait_exit(self) -> int | None:
        if self._proc is None:
            return None
        return await self._proc.wait()

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                logger.warning("picast-player did not exit on SIGTERM; sending SIGKILL")
                self._proc.kill()
                await self._proc.wait()
        self._proc = None


class _IdrUdpProtocol(asyncio.DatagramProtocol):
    """Receives loopback `"idr_request"` datagrams from `picast-player`."""

    def __init__(self, on_idr: asyncio.Queue[str]) -> None:
        self._queue = on_idr

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _ = addr
        msg = data.decode("utf-8", errors="ignore").strip()
        if msg:
            self._queue.put_nowait(msg)


def build_wfd_m3_response(
    requested_body: str,
    cfg: PicastConfig,
    caps: HostCapabilities,
    rtp_port: int,
    allow_uibc: bool = False,
    display_info: DisplayTimingSummary | None = None,
) -> str:
    """Build WFA M3 `GET_PARAMETER` response with universal H.264 + optional WFD 2.0 HEVC.

    `requested_body` is parsed into an exact line `set[str]` (never substring
    matching, so `wfd2_video_formats` cannot falsely satisfy `wfd_video_formats`).
    `display_info` is optional so a caller can inspect the display once per RTSP
    connection instead of once per M3 request.
    """
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
        # codec; returning `wfd_audio_codecs: none` makes Windows 10 abort RTSP
        # at M3. Advertise AAC 48kHz 2ch (matching upstream `d2.py` GStreamer
        # mode): when `cfg.audio_enabled` is False, `picast-player --no-audio`
        # leaves the MPEG-TS audio PID unlinked so `alsasink` is never opened.
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


async def handle_rtsp_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: PicastConfig,
    caps: HostCapabilities,
    session_lock: asyncio.Lock,
    events: SessionEventSink | None = None,
) -> None:
    """Handle one Windows 10/11 Miracast RTSP session end-to-end under Session Lock."""
    peer = writer.get_extra_info("peername")
    peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
    raw_sock = writer.get_extra_info("socket")
    if cfg.tcp_nodelay and raw_sock is not None:
        with contextlib.suppress(OSError):
            raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    if session_lock.locked():
        logger.info("Rejecting incoming RTSP from %s; another session is active", peer_ip)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return

    async with session_lock:
        logger.info("RTSP session started from %s", peer_ip)
        if events:
            await events.on_session_started(peer_ip)

        # Inspect once per connection: M3 may arrive several times, and the DRM
        # sysfs EDID read does not change mid-negotiation.
        display_info = inspect_display()
        framer = RtspStreamFramer()
        runner = PlayerProcessRunner()
        negotiated_codec = "h264"
        negotiated_rtp_port = cfg.rtp_port
        presentation_url = f"rtsp://{peer_ip}/wfd1.0/streamid=0"
        session_id = "12345678"
        sink_cseq = 1
        player_exited = asyncio.Event()
        player_watch_task: asyncio.Task[None] | None = None

        idr_queue: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        idr_transport: asyncio.BaseTransport | None = None
        rtcp_transport: asyncio.BaseTransport | None = None
        bound_idr_port = cfg.idr_ipc_port
        if cfg.idr_on_loss:
            try:
                # Ephemeral loopback port (matching upstream `d2.py`) avoids
                # colliding with RTCP on `rtp_port + 1` (1029).
                idr_transport, _ = await loop.create_datagram_endpoint(
                    lambda: _IdrUdpProtocol(idr_queue),
                    local_addr=("127.0.0.1", 0),
                )
                sock_info = idr_transport.get_extra_info("sockname")
                if isinstance(sock_info, tuple) and len(sock_info) >= 2:
                    bound_idr_port = int(sock_info[1])
            except OSError as exc:
                logger.warning("Failed binding loopback IDR UDP: %s", exc)
                idr_transport = None

        def send_msg(text: str) -> None:
            writer.write(text.encode("utf-8"))

        async def _watch_player() -> None:
            rc = await runner.wait_exit()
            if rc is not None:
                logger.warning("picast-player exited unexpectedly (rc=%d)", rc)
                player_exited.set()

        async def idr_forwarder() -> None:
            nonlocal sink_cseq
            try:
                while True:
                    signal_msg = await idr_queue.get()
                    if signal_msg == "recv error":
                        logger.warning("picast-player reported receive error; ending session")
                        player_exited.set()
                        return
                    if signal_msg == "idr_request":
                        logger.debug("Forwarding wfd_idr_request to %s", peer_ip)
                        body = "wfd_idr_request\r\n"
                        send_msg(
                            f"SET_PARAMETER {presentation_url} RTSP/1.0\r\n"
                            f"CSeq: {sink_cseq}\r\n"
                            f"Session: {session_id}\r\n"
                            f"Content-Type: text/parameters\r\n"
                            f"Content-Length: {len(body)}\r\n\r\n"
                            f"{body}"
                        )
                        sink_cseq += 1
                        await writer.drain()
            except (ConnectionError, OSError) as exc:
                logger.debug("IDR forwarder stopped on socket close: %s", exc)

        idr_task = asyncio.create_task(idr_forwarder())
        # One persistent task per connection instead of a fresh `asyncio.Task`
        # per incoming chunk (read_task still rotates; exit_task must not, so
        # `player_exited.wait()` stays observable across loop iterations).
        exit_task: asyncio.Task[bool] | None = None

        try:
            while not player_exited.is_set():
                read_task = asyncio.create_task(reader.read(4096))
                if exit_task is None:
                    exit_task = asyncio.create_task(player_exited.wait())
                done, pending = await asyncio.wait(
                    {read_task, exit_task},
                    timeout=65.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if read_task in pending:
                    read_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await read_task

                if not done:
                    logger.info("RTSP session from %s timed out waiting for keepalive", peer_ip)
                    break
                if exit_task in done and player_exited.is_set():
                    break
                chunk = read_task.result()
                if not chunk:
                    logger.info("RTSP peer %s closed connection", peer_ip)
                    break

                for frame in framer.feed(chunk):
                    text = frame.decode("utf-8", errors="replace")
                    first_line = text.splitlines()[0] if text else ""
                    logger.info("RTSP <- %s", first_line)
                    cseq = extract_cseq(frame)

                    if text.startswith("OPTIONS"):
                        # Per WFD §6.1.2 (and upstream `d2.py`), a Sink's M1
                        # `Public:` header lists only GET_PARAMETER / SET_PARAMETER.
                        methods = "org.wfa.wfd1.0, SET_PARAMETER, GET_PARAMETER"
                        if resolve_wfd2_hevc_enabled(cfg, caps):
                            methods = "org.wfa.wfd2.0, " + methods
                        writer.write(
                            format_rtsp_response(cseq, extra_headers=[f"Public: {methods}"])
                        )
                        await writer.drain()
                        if sink_cseq == 1:
                            logger.info("RTSP -> M2 OPTIONS *")
                            send_msg(
                                f"OPTIONS * RTSP/1.0\r\nCSeq: {sink_cseq}\r\n"
                                f"Require: org.wfa.wfd1.0\r\n\r\n"
                            )
                            sink_cseq += 1
                    elif text.startswith("GET_PARAMETER"):
                        body_part = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
                        if body_part.strip():
                            params = [ln.strip() for ln in body_part.splitlines() if ln.strip()]
                            logger.info("RTSP M3 req params: %s", ", ".join(params))
                        resp_body = build_wfd_m3_response(
                            body_part, cfg, caps, negotiated_rtp_port, display_info=display_info
                        )
                        writer.write(format_rtsp_response(cseq, body=resp_body))
                    elif text.startswith("SET_PARAMETER"):
                        body_part = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
                        if body_part.strip():
                            params = [ln.strip() for ln in body_part.splitlines() if ln.strip()]
                            logger.info("RTSP SET_PARAMETER: %s", " | ".join(params))
                        if "wfd2_video_formats" in text and resolve_wfd2_hevc_enabled(cfg, caps):
                            negotiated_codec = "h265"
                        if "wfd_presentation_URL:" in text:
                            presentation_url = parse_presentation_url(text, peer_ip)
                        if "client_port=" in text:
                            negotiated_rtp_port = parse_client_rtp_port(text, cfg.rtp_port)
                        writer.write(format_rtsp_response(cseq))
                        if "wfd_trigger_method: SETUP" in text:
                            if rtcp_transport is None:
                                with contextlib.suppress(OSError):
                                    rtcp_transport, _ = await loop.create_datagram_endpoint(
                                        asyncio.DatagramProtocol,
                                        local_addr=("0.0.0.0", negotiated_rtp_port + 1),
                                    )
                            if await runner.start(
                                cfg, negotiated_rtp_port, negotiated_codec, bound_idr_port
                            ):
                                player_watch_task = asyncio.create_task(_watch_player())
                            rtcp_port = negotiated_rtp_port + 1
                            logger.info(
                                "RTSP -> M6 SETUP %s (client_port=%d-%d)",
                                presentation_url,
                                negotiated_rtp_port,
                                rtcp_port,
                            )
                            send_msg(
                                f"SETUP {presentation_url} RTSP/1.0\r\n"
                                f"CSeq: {sink_cseq}\r\n"
                                f"Transport: RTP/AVP/UDP;unicast;"
                                f"client_port={negotiated_rtp_port}-{rtcp_port}\r\n\r\n"
                            )
                            sink_cseq += 1
                        elif "wfd_trigger_method: TEARDOWN" in text:
                            return
                    elif text.startswith("RTSP/1.0 200") and "Transport:" in text:
                        session_id = extract_session_id(text, session_id)
                        logger.info("RTSP -> M7 PLAY (Session=%s)", session_id)
                        send_msg(
                            f"PLAY {presentation_url} RTSP/1.0\r\n"
                            f"User-Agent: MSMiracastSink/10.00.19041.2486\r\n"
                            f"CSeq: {sink_cseq}\r\n"
                            f"Session: {session_id}\r\n\r\n"
                        )
                        sink_cseq += 1
                    elif not text.startswith("RTSP/1.0 200"):
                        logger.warning("RTSP unhandled frame: %r", text[:300])
                    await writer.drain()
        except ValueError as exc:
            logger.warning("Dropping malformed RTSP stream from %s: %s", peer_ip, exc)
        except (ConnectionError, OSError) as exc:
            logger.info("RTSP connection ended (%s): %s", peer_ip, exc)
        except Exception:
            logger.exception("Unexpected exception during RTSP session with %s", peer_ip)
        finally:
            for bg_task in (player_watch_task, idr_task):
                if bg_task is not None and not bg_task.done():
                    bg_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await bg_task
            if exit_task is not None and not exit_task.done():
                exit_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await exit_task
            if rtcp_transport is not None:
                rtcp_transport.close()
            if idr_transport is not None:
                idr_transport.close()
            await runner.stop()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            if events:
                await events.on_session_ended(peer_ip)
            logger.info("RTSP session cleaned up for %s", peer_ip)
