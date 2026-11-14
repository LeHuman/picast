"""Wi-Fi Direct (P2P GO) manager wrapping `wpa_cli`, `iw`, and `dnsmasq` cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import PicastConfig, PinMode
from .platform import (
    HostCapabilities,
    apply_runtime_wifi_tuning,
    resolve_p2p_ht40_allowed,
    resolve_p2p_oper_freq,
)
from .splash import PinManager
from .supplicant_manager import CTRL_DIR, SupplicantManager
from .wpa_ctrl import WpaCtrlMonitor, wpa_request

logger = logging.getLogger(__name__)
P2P_GATEWAY_IP = "192.168.173.1"
P2P_CLIENT_IP = "192.168.173.80"
_P2P_IFACE_RE = re.compile(r"Interface\s+(p2p-[^\s]+)")
_FREQ_RE = re.compile(r"^freq=(\d+)$", re.MULTILINE)
_WFD_DEV_INFO_RE = re.compile(r"wfd_dev_info=0x([0-9a-fA-F]{12})")
# `wps_registrar_add_pin()` in hostap registers a hardcoded 120s
# (`WPS_PBC_WALK_TIME`) `wps_registrar_set_selected_timeout` that clears
# `reg->selected_registrar = 0` in the GO Beacon/Probe Response WPS IE even
# when `timeout=0`. Refreshing every 60s (and immediately on `P2P-PROV-DISC-*`)
# keeps `Selected Registrar = 1` continuously so Windows WCN never rejects the
# GO BSS with "The specific network is not available (RSSI: 255)".
_WPS_SEL_REG_REFRESH_S = 60.0
_WPS_REARM_EVENTS = (
    "P2P-PROV-DISC-SHOW-PIN",
    "P2P-PROV-DISC-ENTER-PIN",
    "P2P-PROV-DISC-PBC-REQ",
    "WPS-TIMEOUT",
    "WPS-FAIL",
    "AP-STA-DISCONNECTED",
)


def parse_station_freq(wpa_status_output: str) -> int | None:
    """Extract the station's current operating frequency from `wpa_cli status`."""
    match = _FREQ_RE.search(wpa_status_output)
    return int(match.group(1)) if match else None


def parse_orphan_p2p_interfaces(iw_dev_output: str) -> list[str]:
    """Extract created GO `p2p-*` netdevs from `iw dev`, ignoring persistent `p2p-dev-*`."""
    return [
        iface
        for iface in _P2P_IFACE_RE.findall(iw_dev_output)
        if not iface.startswith("p2p-dev-")
    ]


def build_dnsmasq_args(p2p_iface: str, lease_file: Path) -> list[str]:
    """Build deterministic single-IP DHCP server arguments for Windows Wi-Fi Direct."""
    return [
        "dnsmasq",
        "--no-daemon",
        "--bind-interfaces",
        f"--interface={p2p_iface}",
        "--dhcp-range=192.168.173.80,192.168.173.80,255.255.255.0,5m",
        "--dhcp-option=3,192.168.173.1",
        "--dhcp-option=6,192.168.173.1",
        f"--dhcp-leasefile={lease_file}",
        "--no-resolv",
        "--no-hosts",
    ]


@dataclass(slots=True)
class WifiP2pManager:
    """Lifecycle controller for Wi-Fi Direct (`p2p-dev-wlan0`) discovery and GO group setup."""

    cfg: PicastConfig
    caps: HostCapabilities
    pin_manager: PinManager
    _dnsmasq_proc: asyncio.subprocess.Process | None = field(default=None, init=False)
    _p2p_group_iface: str | None = field(default=None, init=False)
    _paused_for_session: bool = field(default=False, init=False)
    _supplicant: SupplicantManager | None = field(default=None, init=False)
    _ctrl_dir: str | None = field(default=None, init=False)
    _wps_keepalive_task: asyncio.Task[None] | None = field(default=None, init=False)
    _on_peer_connected: Callable[[str, int], Awaitable[None]] | None = field(
        default=None, init=False
    )

    def _config_methods(self) -> str:
        """WPS config methods by PIN mode, using tokens wpa_supplicant parses.

        `wps_config_methods_str2bin()` matches fixed substrings; `pbc` is not
        one of them and is silently dropped (push button is `push_button`), so
        the historical "virtual_display keypad pbc" only ever advertised
        display + keypad. PIN modes keep exactly that; zero-PIN mode advertises
        push button only.
        """
        if self.cfg.pin_mode is PinMode.NONE:
            return "push_button"
        return "virtual_display keypad"

    def _p2p_dev_iface(self) -> str:
        """Prefer the dedicated `p2p-dev-<iface>` control socket when present.

        On brcmfmac (`WPA_DRIVER_FLAGS_DEDICATED_P2P_DEVICE`), P2P management
        lives on `p2p-dev-wlan0` while `wlan0` is the station netdev (matching
        legacy lazycast `all.sh`). Station `status` and GO `wps_pin` pass an
        explicit `iface=` to target `wlan0` and `p2p-wl*-*` respectively.
        """
        if self._ctrl_dir:
            candidate = f"p2p-dev-{self.cfg.p2p_interface}"
            if (Path(self._ctrl_dir) / candidate).exists():
                return candidate
        return self.cfg.p2p_interface

    def _run_wpa(self, *args: str, iface: str | None = None) -> str:
        """One wpa_supplicant command: direct `wpa_ctrl` datagram, `wpa_cli` fallback."""
        target_iface = iface or self._p2p_dev_iface()
        reply = wpa_request(self._ctrl_dir, target_iface, " ".join(args), timeout_s=5.0)
        if reply is not None:
            return reply
        logger.warning("wpa_cli %s unavailable/failed on %s", args[0], target_iface)
        return ""

    def _list_p2p_interfaces(self) -> list[str]:
        iw_bin = shutil.which("iw")
        if not iw_bin:
            return []
        try:
            res = subprocess.run(
                [iw_bin, "dev"],
                check=False,
                timeout=5.0,
                capture_output=True,
                text=True,
            )
            return parse_orphan_p2p_interfaces(res.stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Failed listing P2P interfaces via iw dev: %s", exc)
            return []

    def cleanup_orphan_interfaces(self) -> None:
        """Delete stale kernel GO `p2p-*` interfaces that cause `-16 EBUSY` failures."""
        iw_bin = shutil.which("iw")
        if not iw_bin:
            return
        for iface in self._list_p2p_interfaces():
            logger.info("Removing stale P2P group interface %s", iface)
            try:
                subprocess.run(
                    [iw_bin, "dev", iface, "del"],
                    check=False,
                    timeout=5.0,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Failed deleting P2P interface %s: %s", iface, exc)

    def configure_wfd_ie(self) -> None:
        """Advertise Miracast WFD Information Elements and device naming via `wpa_cli`."""
        self._run_wpa("set", "wifi_display", "1")
        self._run_wpa("set", "device_name", self.cfg.device_name)
        self._run_wpa("set", "device_type", "7-0050F204-1")
        self._run_wpa("set", "config_methods", self._config_methods())
        # Deterministically defend the Group Owner role (Windows sends go_intent=14).
        # Losing GO negotiation would break the single-IP DHCP + source-IP model.
        self._run_wpa("set", "go_intent", "15")
        if self.cfg.p2p_ssid_postfix:
            self._run_wpa("set", "p2p_ssid_postfix", self.cfg.p2p_ssid_postfix)
        rtsp_hex = f"{self.cfg.rtsp_port:04x}"
        # Match upstream lazycast (all.sh commit 24c4c43): 0x0011 = Primary Sink
        # (0x0001) | Session Available (0x0010), WSD=0, HDCP=0, max 300 Mbps.
        # 0x0151 falsely advertised WFD Service Discovery + HDCP2.x.
        self._run_wpa("wfd_subelem_set", "0", f"00060011{rtsp_hex}012c")
        self._run_wpa("wfd_subelem_set", "1", "0006000000000000")
        self._run_wpa("wfd_subelem_set", "6", "000700000000000000")

    def arm_wps(self, group_iface: str | None = None) -> None:
        """Arm WPS PIN or PBC on the active P2P GO group interface only.

        Arming `wps_pin any` on the station interface (wlan0) makes
        wpa_supplicant scan and enroll against foreign WPS APs, dropping the
        station association; the PIN belongs on the `p2p-wl*` group netdev
        (legacy lazycast behavior).

        No timeout is passed on purpose: on an AP/GO interface the handler
        defaults to timeout=0, and `wps_registrar_add_pin()` only sets
        PIN_EXPIRES for a non-zero timeout, so the PIN stays armed until used.
        The result is logged because an unarmed registrar is otherwise silent.
        """
        pin = self.pin_manager.current_pin
        target = group_iface or self._p2p_group_iface
        if not target:
            logger.warning("No P2P GO group interface yet; WPS not armed")
            return
        if pin:
            result = self._run_wpa("wps_pin", "any", pin, iface=target)
            if result.strip() == pin:
                logger.info("WPS PIN armed on %s", target)
            else:
                logger.warning("wps_pin NOT armed on %s (reply: %r)", target, result)
        else:
            result = self._run_wpa("wps_pbc", iface=target)
            logger.info("WPS PBC arm on %s: %s", target, result or "no reply")

    async def arm_wps_async(self, group_iface: str | None = None) -> None:
        """Non-blocking wrapper arming WPS PIN/PBC off the event loop thread."""
        await asyncio.to_thread(self.arm_wps, group_iface)

    def _setup_p2p_group_sync(self) -> None:
        self.cleanup_orphan_interfaces()
        self.configure_wfd_ie()
        apply_runtime_wifi_tuning(self.cfg.p2p_interface, self.cfg.wifi_power_save)

        freq = resolve_p2p_oper_freq(self.cfg, self.caps)
        if freq < 5000:
            # Single-radio 2.4 GHz: a fixed GO channel that differs from the
            # station's channel makes brcmfmac tear the GO down instantly
            # (chanspec -52 / AP-DISABLED; beacon never goes out). Follow the
            # live station channel (legacy lazycast behavior), else let
            # wpa_supplicant choose (omit freq=).
            station_freq = parse_station_freq(
                self._run_wpa("status", iface=self.cfg.p2p_interface)
            )
            freq = station_freq if station_freq and 2400 < station_freq < 2500 else 0
        ht40_arg = ["ht40"] if resolve_p2p_ht40_allowed(self.cfg, self.caps) else []
        logger.info(
            "Starting P2P GO on %s (HT40=%s)",
            f"{freq} MHz" if freq else "auto channel",
            bool(ht40_arg),
        )
        # Briefly stop discovery so group creation starts from a quiet state.
        # In own-supplicant mode do NOT restart p2p_find afterwards: the proven
        # NM-free configuration relies on the autonomous GO beacon alone, and
        # discovery listen/scan cycling can starve the GO's WPS exchange.
        self._run_wpa("p2p_stop_find")
        group_args: list[str] = []
        if freq:
            group_args.append(f"freq={freq}")
        group_args.extend(ht40_arg)
        self._run_wpa("p2p_group_add", *group_args)
        if not self._supplicant:
            # System-supplicant mode (fallback): keep discovery cycles running
            # for Windows visibility (NM reaps the GO anyway).
            self._run_wpa("p2p_find")

    async def start(
        self,
        on_peer_connected: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> None:
        """Create P2P Autonomous GO group, arm WPS PIN/PBC, and launch DHCP `dnsmasq`."""
        self._on_peer_connected = on_peer_connected
        if self.cfg.use_own_supplicant:
            supplicant = SupplicantManager(
                interface=self.cfg.p2p_interface,
                device_name=self.cfg.device_name,
                config_methods=self._config_methods(),
            )
            if await asyncio.to_thread(supplicant.start):
                self._supplicant = supplicant
                self._ctrl_dir = CTRL_DIR
            else:
                logger.warning(
                    "Own-supplicant mode unavailable (association failed or no Wi-Fi); "
                    "falling back to system supplicant (NetworkManager may reap the group)"
                )
        await asyncio.to_thread(self._setup_p2p_group_sync)
        group_iface = await self._wait_for_group_interface()
        if group_iface:
            # Arming races supplicant socket binding: `iw dev` lists the netdev
            # before the GO's wpa_ctrl socket exists, and a `wps_pin` issued in
            # that window FAILS — leaving the registrar unarmed (Windows then
            # stalls at provisioning). Wait for the socket explicitly.
            await self._wait_for_group_ctrl_socket(group_iface)
            await asyncio.to_thread(self.arm_wps, group_iface)
            await self.start_dhcp(group_iface)
            self._wps_keepalive_task = asyncio.create_task(
                self._wps_keepalive_loop(group_iface),
                name="picast-wps-keepalive",
            )
        else:
            logger.warning("Timed out waiting for P2P GO netdev; DHCP server deferred")

    async def _wait_for_group_interface(self, timeout_s: float = 4.0) -> str | None:
        """Poll briefly for `wpa_supplicant` to materialize `p2p-wl*-*` group netdev."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            ifaces = await asyncio.to_thread(self._list_p2p_interfaces)
            if ifaces:
                logger.info("P2P GO interface ready: %s", ifaces[0])
                return ifaces[0]
            await asyncio.sleep(0.2)
        return None

    async def _wait_for_group_ctrl_socket(self, group_iface: str, timeout_s: float = 5.0) -> None:
        """Wait for the GO's wpa_ctrl UNIX socket to exist before arming WPS."""
        ctrl_dir = Path(self._ctrl_dir) if self._ctrl_dir else None
        if ctrl_dir is None:
            return
        socket_path = ctrl_dir / group_iface
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if socket_path.exists():
                logger.debug("GO ctrl socket ready: %s", socket_path)
                return
            await asyncio.sleep(0.25)
        logger.warning("GO ctrl socket %s never appeared; WPS arming may fail", socket_path)

    async def start_dhcp(self, p2p_iface: str) -> None:
        """Assign GO gateway IP `192.168.173.1/24` and run single-IP `dnsmasq`."""
        self._p2p_group_iface = p2p_iface
        ip_bin = shutil.which("ip")
        if ip_bin:
            def _assign_ip() -> None:
                try:
                    subprocess.run(
                        [ip_bin, "addr", "replace", "192.168.173.1/24", "dev", p2p_iface],
                        check=False,
                        timeout=5.0,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    logger.warning("Failed assigning GO IP on %s: %s", p2p_iface, exc)

            await asyncio.to_thread(_assign_ip)

        dnsmasq_bin = shutil.which("dnsmasq")
        if not dnsmasq_bin:
            logger.warning("dnsmasq not found; Windows Wi-Fi Direct DHCP unavailable")
            return
        lease_file = Path("/run/picast/dnsmasq.leases")
        try:
            lease_file.parent.mkdir(parents=True, exist_ok=True)
            args = build_dnsmasq_args(p2p_iface, lease_file)
            self._dnsmasq_proc = await asyncio.create_subprocess_exec(
                dnsmasq_bin,
                *args[1:],
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info("Started dnsmasq on %s", p2p_iface)
        except OSError as exc:
            logger.warning("Failed starting dnsmasq on %s: %s", p2p_iface, exc)

    def set_scan_suppression(self, active_session: bool) -> None:
        """Keep background Wi-Fi scan activity off the single radio while streaming."""
        if not self.cfg.suppress_bg_scans:
            return
        self._paused_for_session = active_session
        # Discovery (p2p_find) must never run alongside the autonomous GO on
        # brcmfmac; only the GO beacon is needed, so just re-arm WPS after a cast.
        if not active_session:
            self.arm_wps(self._p2p_group_iface)

    async def set_scan_suppression_async(self, active_session: bool) -> None:
        """Non-blocking scan suppression toggle off the asyncio event loop."""
        await asyncio.to_thread(self.set_scan_suppression, active_session)

    async def _wps_keepalive_loop(self, group_iface: str) -> None:
        """Keep `Selected Registrar = 1` alive and re-arm immediately on PD/WPS events.

        Uses the async `WpaCtrlMonitor` (`ATTACH`ed UNIX datagram socket on
        `<ctrl_dir>/global`) plus a 60 s periodic refresh well inside hostap's
        120 s `WPS_PBC_WALK_TIME`.
        """
        monitor: WpaCtrlMonitor | None = (
            WpaCtrlMonitor(self._ctrl_dir) if self._ctrl_dir else None
        )
        if monitor is not None and await monitor.attach():
            logger.info("Attached WPS event monitor to %s/global", self._ctrl_dir)
        else:
            monitor = None
        peer_rtsp_port = self.cfg.rtsp_port

        try:
            while True:
                raw = await (
                    monitor.recv_event(_WPS_SEL_REG_REFRESH_S)
                    if monitor is not None
                    else asyncio.sleep(_WPS_SEL_REG_REFRESH_S, result=None)
                )
                if raw is None:
                    # Refresh timeout (or no monitor socket): periodic re-arm.
                    if not self._paused_for_session:
                        await self.arm_wps_async(group_iface)
                    continue

                msg = raw
                # Strip optional `<ifname>` / `<level>` prefix from global ctrl_iface.
                if msg.startswith("IFNAME="):
                    _, _, msg = msg.partition(" ")
                if msg.startswith("<") and ">" in msg[:5]:
                    msg = msg.split(">", 1)[1].strip()

                if msg.startswith("P2P-DEVICE-FOUND"):
                    wfd_match = _WFD_DEV_INFO_RE.search(msg)
                    if wfd_match:
                        port = int(wfd_match.group(1)[4:8], 16)
                        if port > 0:
                            peer_rtsp_port = port

                if any(msg.startswith(ev) for ev in _WPS_REARM_EVENTS):
                    logger.info("P2P/WPS event (%s); refreshing WPS registrar", msg.split()[0])
                    if msg.startswith("AP-STA-DISCONNECTED"):
                        # A disconnected peer's advertised RTSP port must never
                        # leak into the `_on_peer_connected` of the NEXT peer.
                        peer_rtsp_port = self.cfg.rtsp_port
                    if msg.startswith("P2P-PROV-DISC-PBC-REQ"):
                        await asyncio.to_thread(self._run_wpa, "wps_pbc", iface=group_iface)
                    else:
                        await self.arm_wps_async(group_iface)
                elif msg.startswith("WPS-REG-SUCCESS"):
                    logger.info("P2P/WPS milestone: %s", msg)
                elif msg.startswith("AP-STA-CONNECTED"):
                    logger.info("P2P/WPS milestone: %s", msg)
                    if self._on_peer_connected is not None:
                        await self._on_peer_connected(P2P_CLIENT_IP, peer_rtsp_port)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WPS keepalive monitor failed")
        finally:
            if monitor is not None:
                await monitor.close()

    async def stop(self) -> None:
        """Tear down `dnsmasq`, WPS keepalive, and P2P group cleanly."""
        if self._wps_keepalive_task is not None:
            self._wps_keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wps_keepalive_task
            self._wps_keepalive_task = None
        if self._dnsmasq_proc is not None and self._dnsmasq_proc.returncode is None:
            self._dnsmasq_proc.terminate()
            try:
                await asyncio.wait_for(self._dnsmasq_proc.wait(), timeout=2.0)
            except TimeoutError:
                logger.warning("dnsmasq did not exit on SIGTERM; sending SIGKILL")
                self._dnsmasq_proc.kill()
                await self._dnsmasq_proc.wait()
        self._dnsmasq_proc = None

        def _teardown_sync() -> None:
            if self._p2p_group_iface:
                self._run_wpa("p2p_group_remove", self._p2p_group_iface)
                self._p2p_group_iface = None
            self.cleanup_orphan_interfaces()

        await asyncio.to_thread(_teardown_sync)

        if self._supplicant is not None:
            await asyncio.to_thread(self._supplicant.stop)
            self._supplicant = None
        self._ctrl_dir = None
