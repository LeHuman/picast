"""Main asyncio daemon coordinating Wi-Fi Direct, MICE, RTSP sessions, and splash UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field

from .config import PicastConfig, PinMode, TransportMode
from .mice_service import MiceService
from .platform import HostCapabilities, probe_host_capabilities
from .rtsp_session import handle_rtsp_connection
from .splash import PinManager, SplashState, render_splash
from .wifi_p2p import P2P_GATEWAY_IP, WifiP2pManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PicastDaemon:
    """Unified controller enforcing Session Lock across Wi-Fi Direct and MICE."""

    cfg: PicastConfig
    caps: HostCapabilities = field(default_factory=probe_host_capabilities)
    _session_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _pin_manager: PinManager = field(init=False)
    _p2p: WifiP2pManager | None = field(default=None, init=False)
    _mice: MiceService | None = field(default=None, init=False)
    _rtsp_server: asyncio.AbstractServer | None = field(default=None, init=False)
    _p2p_dial_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._pin_manager = PinManager(self.cfg.pin_mode, self.cfg.fixed_pin)

    def _transport_label(self) -> str:
        if self.cfg.transport_mode is TransportMode.BOTH:
            return "P2P + MICE"
        if self.cfg.transport_mode is TransportMode.MICE:
            return "MICE LAN"
        return "WI-FI DIRECT"

    def _pin_label(self) -> str:
        pin = self._pin_manager.current_pin
        if pin:
            return pin
        return "PBC AUTO" if self.cfg.pin_mode is PinMode.NONE else "--------"

    def _splash_state(self, status: str, ip_hint: str = "") -> SplashState:
        return SplashState(
            device_name=self.cfg.device_name,
            transport_label=self._transport_label(),
            pin_display=self._pin_label(),
            status_line=status,
            ip_hint=ip_hint,
        )

    def _refresh_splash(self, status: str, ip_hint: str = "") -> None:
        render_splash(self._splash_state(status, ip_hint))

    async def _refresh_splash_async(self, status: str, ip_hint: str = "") -> None:
        # The framebuffer path writes ~8.3 MB to /dev/fb0; keep it off the
        # asyncio event loop so RTSP keepalives are never stalled by redraws.
        await asyncio.to_thread(render_splash, self._splash_state(status, ip_hint))

    async def on_session_started(self, peer_ip: str) -> None:
        """Pause background discovery scans and update splash during an active cast."""
        if self._p2p is not None:
            await self._p2p.set_scan_suppression_async(True)
        await self._refresh_splash_async(f"CASTING FROM {peer_ip}", ip_hint=f"PEER {peer_ip}")

    async def on_session_ended(self, peer_ip: str) -> None:
        """Restore discovery listeners and apply configured PIN rotation after disconnect."""
        _ = peer_ip
        new_pin = self._pin_manager.on_session_ended()
        if self._p2p is not None:
            if new_pin and self.cfg.pin_mode is PinMode.PER_SESSION:
                await self._p2p.arm_wps_async()
            await self._p2p.set_scan_suppression_async(False)
        await self._refresh_splash_async("READY FOR WIN+K CONNECTION")

    async def _on_p2p_peer_connected(self, peer_ip: str, rtsp_port: int) -> None:
        """Spawn outbound RTSP dialer when a Wi-Fi Direct source associates to our GO.

        Per WFD §6.1 (and legacy lazycast `d2.py`), the Miracast Source (`Windows`,
        `192.168.173.80`) is the TCP `:7236` server; the Sink (`192.168.173.1`)
        must initiate the TCP connection once DHCP completes (~3 s after L2).
        """
        if self._session_lock.locked():
            return
        if self._p2p_dial_task is not None and not self._p2p_dial_task.done():
            return
        self._p2p_dial_task = asyncio.create_task(
            self._dial_and_serve_rtsp(peer_ip, rtsp_port, local_ip=P2P_GATEWAY_IP),
            name="picast-p2p-rtsp-dial",
        )

    async def _on_p2p_peer_disconnected(self) -> None:
        """Abort any active P2P RTSP session immediately when L2 drops (`AP-STA-DISCONNECTED`)."""
        if self._p2p_dial_task is not None and not self._p2p_dial_task.done():
            logger.info("P2P peer disconnected at L2; tearing down active RTSP session")
            self._p2p_dial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._p2p_dial_task
        self._p2p_dial_task = None

    async def _dial_and_serve_rtsp(
        self,
        peer_ip: str,
        rtsp_port: int,
        local_ip: str | None = None,
        attempts: int = 35,
        retry_delay_s: float = 0.15,
    ) -> None:
        """Dial the WFD source's RTSP server, then run WFD M1-M7 under Session Lock.

        Per WFD §6.1 (and legacy lazycast `d2.py`), the Miracast Source is the
        TCP `:7236` server; the Sink must initiate the connection. P2P dialing
        binds `192.168.173.1` and polls across DHCP setup (~3 s after L2);
        MICE uses short-lived retries without a bind address.
        """
        local_addr = (local_ip, 0) if local_ip else None
        logger.info("Dialing WFD source RTSP server at %s:%d", peer_ip, rtsp_port)
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        for attempt in range(1, attempts + 1):
            if self._stop_event.is_set() or self._session_lock.locked():
                return
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(peer_ip, rtsp_port, local_addr=local_addr),
                    timeout=1.5 if local_addr else 5.0,
                )
                logger.info(
                    "Connected outbound RTSP to %s:%d (attempt %d)",
                    peer_ip,
                    rtsp_port,
                    attempt,
                )
                break
            except (TimeoutError, OSError) as exc:
                logger.debug(
                    "RTSP dial %s:%d attempt %d failed: %s", peer_ip, rtsp_port, attempt, exc
                )
                await asyncio.sleep(retry_delay_s)

        if reader is None or writer is None:
            logger.warning("Timed out dialing WFD source at %s:%d", peer_ip, rtsp_port)
            return

        await handle_rtsp_connection(
            reader, writer, self.cfg, self.caps, self._session_lock, self
        )

    async def _handle_mice_source_ready(self, peer_ip: str, rtsp_port: int) -> None:
        """Connect back to a Windows MICE sender's RTSP server under the shared Session Lock."""
        if self._session_lock.locked():
            logger.info("Ignoring MICE trigger from %s; another session is active", peer_ip)
            return
        await self._dial_and_serve_rtsp(peer_ip, rtsp_port, attempts=3, retry_delay_s=0.15)

    async def run(self) -> None:
        """Start configured transports and RTSP :7236 server until SIGINT/SIGTERM."""
        logger.info(
            "Starting picast daemon (device=%s, board=%s, transport=%s)",
            self.cfg.device_name,
            self.caps.board.value,
            self.cfg.transport_mode.value,
        )
        self._pin_manager.initialize()
        await self._refresh_splash_async("STARTING WIRELESS DISPLAY")

        try:
            if self.cfg.transport_mode in (TransportMode.BOTH, TransportMode.P2P):
                self._p2p = WifiP2pManager(self.cfg, self.caps, self._pin_manager)
                await self._p2p.start(
                    self._on_p2p_peer_connected,
                    on_peer_disconnected=self._on_p2p_peer_disconnected,
                )

            if self.cfg.transport_mode in (TransportMode.BOTH, TransportMode.MICE):
                self._mice = MiceService(self.cfg)
                await self._mice.start(self._handle_mice_source_ready)

            self._rtsp_server = await asyncio.start_server(
                lambda r, w: handle_rtsp_connection(
                    r, w, self.cfg, self.caps, self._session_lock, self
                ),
                host="0.0.0.0",
                port=self.cfg.rtsp_port,
            )
            logger.info("RTSP receiver listening on :%d", self.cfg.rtsp_port)
            await self._refresh_splash_async("READY FOR WIN+K CONNECTION")

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, self._stop_event.set)

            await self._stop_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop RTSP, MICE, and Wi-Fi Direct subsystems deterministically."""
        logger.info("Shutting down picast daemon")
        if self._p2p_dial_task is not None and not self._p2p_dial_task.done():
            self._p2p_dial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._p2p_dial_task
        self._p2p_dial_task = None
        if self._rtsp_server is not None:
            try:
                self._rtsp_server.close()
                await self._rtsp_server.wait_closed()
            except Exception:
                logger.exception("Error stopping RTSP server")
            self._rtsp_server = None
        if self._mice is not None:
            try:
                await self._mice.stop()
            except Exception:
                logger.exception("Error stopping MICE service")
            self._mice = None
        if self._p2p is not None:
            try:
                await self._p2p.stop()
            except Exception:
                logger.exception("Error stopping Wi-Fi Direct manager")
            self._p2p = None
