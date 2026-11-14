"""First-class MICE (Miracast over Infrastructure) mDNS publisher & MS-MICE listener."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .config import PicastConfig

logger = logging.getLogger(__name__)
DEFAULT_UUID_PATH: Final[Path] = Path("/var/lib/picast/uuid")
MICE_CMD_SOURCE_READY: Final[int] = 1
MICE_TLV_RTSP_PORT: Final[int] = 2


def get_or_create_receiver_uuid(uuid_path: Path = DEFAULT_UUID_PATH) -> uuid.UUID:
    """Load persistent receiver UUID (used in MICE `_display._tcp` TXT records)."""
    try:
        if uuid_path.exists():
            return uuid.UUID(uuid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        logger.warning("Regenerating invalid/unreadable receiver UUID (%s): %s", uuid_path, exc)

    generated = uuid.uuid4()
    try:
        uuid_path.parent.mkdir(parents=True, exist_ok=True)
        uuid_path.write_text(str(generated) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed saving receiver UUID to %s: %s", uuid_path, exc)
    return generated


def build_mice_txt_records(cfg: PicastConfig, receiver_id: uuid.UUID) -> list[str]:
    """Construct `_display._tcp` TXT key=value properties per MS-MICE specification.

    MS-MICE v1 requires the `container_id` GUID key for sink advertisement;
    `rp=all` marks full role policy for Windows 10/11 sink discovery.
    """
    return [
        "rp=all",
        f"container_id={{{str(receiver_id).upper()}}}",
        f"id={receiver_id}",
        f"name={cfg.device_name}",
    ]


def parse_mice_source_ready_port(frame: bytes, default_port: int = 7236) -> int | None:
    """Parse MS-MICE v1 header + TLVs and return sender RTSP port for `SOURCE_READY`."""
    if len(frame) < 4:
        return None
    msg_len = int.from_bytes(frame[0:2], "big")
    cmd = frame[3]
    if cmd != MICE_CMD_SOURCE_READY:
        return None

    limit = min(len(frame), max(4, msg_len))
    offset = 4
    while offset + 3 <= limit:
        tag = frame[offset]
        tlv_len = int.from_bytes(frame[offset + 1 : offset + 3], "big")
        val_start = offset + 3
        val_end = val_start + tlv_len
        if val_end > limit:
            break
        if tag == MICE_TLV_RTSP_PORT and tlv_len == 2:
            return int.from_bytes(frame[val_start:val_end], "big")
        offset = val_end
    return default_port


@dataclass(slots=True)
class MiceService:
    """Manages Avahi `_display._tcp` mDNS announcement and TCP :7250 MICE triggers."""

    cfg: PicastConfig
    uuid_path: Path = DEFAULT_UUID_PATH
    _avahi_proc: asyncio.subprocess.Process | None = field(default=None, init=False)
    _server: asyncio.AbstractServer | None = field(default=None, init=False)
    _on_source_ready: Callable[[str, int], Awaitable[None]] | None = field(
        default=None, init=False
    )

    async def start(
        self,
        on_source_ready: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> None:
        """Start Avahi `_display._tcp` advertisement and TCP MICE control socket."""
        self._on_source_ready = on_source_ready
        receiver_id = get_or_create_receiver_uuid(self.uuid_path)
        avahi_publish = shutil.which("avahi-publish-service")
        if avahi_publish:
            txt_records = build_mice_txt_records(self.cfg, receiver_id)
            try:
                # Per [MS-MICE] 3.1.3 the DNS-SD SRV port MUST point at the
                # sink's MS-MICE control channel (`mice_port`, default 7250):
                # the source connects there to deliver SOURCE_READY carrying
                # its own RTSP listener port; the sink then dials the source
                # outbound for the RTSP/WFD session (see daemon callback).
                self._avahi_proc = await asyncio.create_subprocess_exec(
                    avahi_publish,
                    self.cfg.device_name,
                    "_display._tcp",
                    str(self.cfg.mice_port),
                    *txt_records,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(
                    "Published MICE mDNS service '%s' control :%d",
                    self.cfg.device_name,
                    self.cfg.mice_port,
                )
            except OSError as exc:
                logger.warning("Failed launching avahi-publish-service: %s", exc)
        else:
            logger.warning("avahi-publish-service not found; LAN MICE discovery disabled")

        self._server = await asyncio.start_server(
            self._handle_mice_control,
            host="0.0.0.0",
            port=self.cfg.mice_port,
        )
        logger.info("MICE TCP control listener active on :%d", self.cfg.mice_port)

    async def _handle_mice_control(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept MS-MICE `SOURCE_READY` TLV and initiate outbound RTSP to the Windows PC."""
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
        raw_sock = writer.get_extra_info("socket")
        if self.cfg.tcp_nodelay and raw_sock is not None:
            with contextlib.suppress(OSError):
                raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            frame = await asyncio.wait_for(reader.read(1024), timeout=5.0)
            rtsp_port = parse_mice_source_ready_port(frame, self.cfg.rtsp_port)
            if rtsp_port is not None and peer_ip and self._on_source_ready is not None:
                logger.info("MICE SOURCE_READY from %s -> RTSP :%d", peer_ip, rtsp_port)
                await self._on_source_ready(peer_ip, rtsp_port)
        except TimeoutError:
            logger.debug("Timed out reading MICE control frame from %s", peer_ip or "unknown")
        except OSError as exc:
            logger.warning("MICE socket error from %s: %s", peer_ip or "unknown", exc)
        except Exception:
            logger.exception("Unexpected error handling MICE control connection")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def stop(self) -> None:
        """Stop MICE TCP listener and unpublish mDNS record."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except OSError as exc:
                logger.debug("Error closing MICE TCP server: %s", exc)
            self._server = None
        if self._avahi_proc is not None and self._avahi_proc.returncode is None:
            self._avahi_proc.terminate()
            try:
                await asyncio.wait_for(self._avahi_proc.wait(), timeout=2.0)
            except TimeoutError:
                logger.warning("avahi-publish-service did not exit on SIGTERM; sending SIGKILL")
                self._avahi_proc.kill()
                await self._avahi_proc.wait()
        self._avahi_proc = None
