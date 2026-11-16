"""Asyncio RTSP M1-M7 / WFD 2.0 session orchestration and IDR watchdog."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import PicastConfig, SyncMode
from .edid import inspect_display
from .platform import HostCapabilities, resolve_wfd2_hevc_enabled
from .uibc import UibcHidcBridge
from .wfd_protocol import (
    RtspStreamFramer,
    build_wfd_m3_response,
    extract_cseq,
    extract_session_id,
    format_rtsp_response,
    is_wfd2_hevc_selected,
    parse_client_rtp_port,
    parse_presentation_url,
    parse_uibc_port,
)

__all__ = [
    "PlayerProcessRunner",
    "RtspStreamFramer",
    "SessionEventSink",
    "build_wfd_m3_response",
    "extract_cseq",
    "extract_session_id",
    "format_rtsp_response",
    "handle_rtsp_connection",
    "is_wfd2_hevc_selected",
    "parse_client_rtp_port",
    "parse_presentation_url",
    "parse_uibc_port",
]

logger = logging.getLogger(__name__)


class SessionEventSink(Protocol):
    """Lifecycle hooks fired by an active RTSP Miracast session."""

    async def on_session_started(self, peer_ip: str) -> None: ...
    async def on_session_ended(self, peer_ip: str) -> None: ...


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
        idr_port: int = 0,
    ) -> bool:
        resolved = shutil.which(self.binary_path) or "/usr/bin/picast-player"
        if not Path(resolved).exists():
            logger.error("picast-player binary not found at %s", resolved)
            return False

        args = [
            resolved,
            "--rtp-port",
            str(rtp_port),
            "--idr-port",
            str(idr_port),
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
        uibc_bridge: UibcHidcBridge | None = None
        uibc_task: asyncio.Task[None] | None = None

        idr_queue: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        idr_transport: asyncio.BaseTransport | None = None
        rtcp_transport: asyncio.BaseTransport | None = None
        bound_idr_port = 0
        if cfg.idr_on_loss:
            try:
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
                            body_part,
                            cfg,
                            caps,
                            negotiated_rtp_port,
                            allow_uibc=cfg.uibc_enabled,
                            display_info=display_info,
                        )
                        writer.write(format_rtsp_response(cseq, body=resp_body))
                    elif text.startswith("SET_PARAMETER"):
                        body_part = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
                        if body_part.strip():
                            params = [ln.strip() for ln in body_part.splitlines() if ln.strip()]
                            logger.info("RTSP SET_PARAMETER: %s", " | ".join(params))
                        if is_wfd2_hevc_selected(text) and resolve_wfd2_hevc_enabled(cfg, caps):
                            negotiated_codec = "h265"
                        if "wfd_presentation_URL:" in text:
                            presentation_url = parse_presentation_url(text, peer_ip)
                        if "client_port=" in text:
                            negotiated_rtp_port = parse_client_rtp_port(text, cfg.rtp_port)
                        writer.write(format_rtsp_response(cseq))
                        if cfg.uibc_enabled and uibc_bridge is None and uibc_task is None:
                            uibc_port = parse_uibc_port(text)
                            if uibc_port is not None:
                                logger.info(
                                    "RTSP M4 offers UIBC HIDC on %s:%d; starting input bridge",
                                    peer_ip,
                                    uibc_port,
                                )
                                uibc_bridge = UibcHidcBridge(peer_ip, uibc_port)
                                uibc_task = asyncio.create_task(
                                    uibc_bridge.run(), name=f"uibc-hidc-{peer_ip}"
                                )
                        if "wfd_trigger_method: SETUP" in text:
                            if rtcp_transport is None:
                                with contextlib.suppress(OSError):
                                    rtcp_transport, _ = await loop.create_datagram_endpoint(
                                        asyncio.DatagramProtocol,
                                        local_addr=("0.0.0.0", negotiated_rtp_port + 1),
                                    )
                            if not await runner.start(
                                cfg, negotiated_rtp_port, negotiated_codec, bound_idr_port
                            ):
                                logger.error(
                                    "Aborting RTSP session with %s: picast-player failed to start",
                                    peer_ip,
                                )
                                await writer.drain()
                                return
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
            for bg_task in (player_watch_task, idr_task, uibc_task):
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
            if uibc_bridge is not None:
                uibc_bridge.close()
            await runner.stop()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            if events:
                await events.on_session_ended(peer_ip)
            logger.info("RTSP session cleaned up for %s", peer_ip)
