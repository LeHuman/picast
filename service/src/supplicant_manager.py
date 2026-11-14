"""Private wpa_supplicant lifecycle: escape NetworkManager P2P group reaping on Trixie.

Trixie's NetworkManager owns the system wpa_supplicant over D-Bus and tears down
autonomous P2P GO groups it did not initiate (`P2P: WPA supplicant notified a
group start but we are not trying to connect!`). Running a dedicated
wpa_supplicant for the P2P interface restores the legacy lazycast (Bullseye,
NM-less) behavior where the GO beacons indefinitely. Verified live on a Zero 2 W
(GO alive >30 s with own supplicant vs <1 s under NM supervision).
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)
CTRL_DIR = "/run/picast/wpa"
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IPV4_RE = r"\d+(?:\.\d+){3}"
CONF_PATH = Path("/run/picast/wpa/picast-wpa.conf")
PID_PATH = Path("/run/picast/wpa/wpa_supplicant.pid")
# Persistent on purpose: /run/picast is a systemd RuntimeDirectory (wiped on
# unit stop and on power-cycle), which destroyed the evidence of every lockout.
LOG_PATH = Path("/var/lib/picast/wpa_supplicant.log")
LOG_PREV_PATH = Path("/var/lib/picast/wpa_supplicant.log.prev")


def _nmcli_active_connection(interface: str) -> str | None:
    """Return the active NM connection name bound to `interface`, if any."""
    nmcli = shutil.which("nmcli")
    if not nmcli:
        return None
    try:
        res = subprocess.run(
            [nmcli, "-t", "-f", "NAME,DEVICE,TYPE", "connection", "show", "--active"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in res.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1] == interface and parts[2].startswith("802-11"):
            return parts[0]
    return None


_NM_CONN_DIRS = (
    "/etc/NetworkManager/system-connections/",
    "/run/NetworkManager/system-connections/",
)


def read_active_ssid(interface: str) -> str | None:
    """Read the station SSID from NM (active connection first, then keyfiles)."""
    conn = _nmcli_active_connection(interface)
    nmcli = shutil.which("nmcli")
    if conn and nmcli:
        try:
            res = subprocess.run(
                [nmcli, "-g", "802-11-wireless.ssid", "connection", "show", conn],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            res = None
        if res is not None and res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    for conn_dir in _NM_CONN_DIRS:
        if not Path(conn_dir).exists():
            continue
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            grep = subprocess.run(
                ["grep", "-rh", "^ssid=", conn_dir],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            for line in grep.stdout.splitlines():
                value = line.split("=", 1)[1].strip().strip('"')
                if value:
                    return value
    return None


def read_active_psk(interface: str) -> str | None:
    """Read the station PSK from NM (connection property, then keyfile fallback)."""
    conn = _nmcli_active_connection(interface)
    nmcli = shutil.which("nmcli")
    if conn and nmcli:
        try:
            res = subprocess.run(
                [
                    nmcli, "--show-secrets", "-g", "802-11-wireless-security.psk",
                    "connection", "show", conn,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            res = None
        if res is not None and res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    for conn_dir in _NM_CONN_DIRS:
        if not Path(conn_dir).exists():
            continue
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            grep = subprocess.run(
                ["grep", "-rh", "^psk=", conn_dir],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            for line in grep.stdout.splitlines():
                value = line.split("=", 1)[1].strip().strip('"')
                if value:
                    return value
    return None


def build_supplicant_config(
    ssid: str,
    psk: str | None,
    device_name: str,
    config_methods: str = "virtual_display keypad",
) -> str:
    """Build a complete wpa_supplicant config: station network + P2P/WFD identity.

    Mirrors the proven NM-free test configuration (GO intent 15, display device
    type). `config_methods` is chosen by PIN mode (`push_button` for zero-PIN,
    display/keypad otherwise). PSKs that are 64 hex chars are written unquoted (the
    precomputed PBKDF2 hash form); passphrases are backslash/quote-escaped.
    """
    if psk:
        if _HEX64_RE.fullmatch(psk):
            psk_line = f"\t\tpsk={psk}\n"
        else:
            escaped = psk.replace("\\", "\\\\").replace('"', '\\"')
            psk_line = f'\t\tpsk="{escaped}"\n'
        network = f'\tnetwork={{\n\t\tssid="{ssid}"\n{psk_line}\t}}\n'
    else:
        network = f'\tnetwork={{\n\t\tssid="{ssid}"\n\t\tkey_mgmt=NONE\n\t}}\n'
    return (
        "ctrl_interface=DIR=/run/picast/wpa GROUP=netdev\n"
        "country=US\n"
        f"device_name={device_name}\n"
        "device_type=7-0050F204-1\n"
        f"config_methods={config_methods}\n"
        "p2p_go_intent=15\n"
        f"{network}"
    )


def read_station_lease(interface: str) -> tuple[str, str | None]:
    """Capture the station's global IPv4 (addr/CIDR, default gateway) pre-stop.

    Stopping NetworkManager kills its DHCP client, so the lease must be
    re-applied manually after the private supplicant associates; a bare L2
    association leaves `wlan0` without an IPv4 -> SSH lockout.
    """
    ip_bin = shutil.which("ip")
    if not ip_bin:
        return "", None
    addr = ""
    gw: str | None = None
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        res = subprocess.run(
            [ip_bin, "-4", "-o", "addr", "show", "dev", interface, "scope", "global"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        match = re.search(rf"inet ({_IPV4_RE}/\d+)", res.stdout)
        if match:
            addr = match.group(1)
    if addr:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            res = subprocess.run(
                [ip_bin, "route", "show", "default", "dev", interface],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            match = re.search(rf"via ({_IPV4_RE})", res.stdout)
            if match:
                gw = match.group(1)
    return addr, gw


@dataclass(slots=True)
class SupplicantManager:
    """Runs a dedicated wpa_supplicant so NM cannot reap the autonomous P2P GO.

    Lifecycle: read NM credentials first, stop NetworkManager, launch the
    private supplicant, and hand back to NetworkManager on stop(). If NM
    credentials cannot be read, `start()` declines (returns False) so callers
    fall back to the system supplicant instead of orphaning the station.
    """

    interface: str = "wlan0"
    device_name: str = "picast"
    config_methods: str = "virtual_display keypad"
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _nm_stopped: bool = field(default=False, init=False)
    _wpa_stopped: bool = field(default=False, init=False)
    _lease_addr: str = field(default="", init=False)
    _lease_gw: str | None = field(default=None, init=False)

    def _ping(self) -> bool:
        cli = shutil.which("wpa_cli")
        if not cli:
            return False
        try:
            res = subprocess.run(
                [cli, "-p", CTRL_DIR, "-i", self.interface, "ping"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "PONG" in res.stdout

    def _is_associated(self) -> bool:
        cli = shutil.which("wpa_cli")
        if not cli:
            return False
        try:
            res = subprocess.run(
                [cli, "-p", CTRL_DIR, "-i", self.interface, "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "wpa_state=COMPLETED" in res.stdout

    def _stop_system_network_stack(self) -> None:
        """Stop NM and the *system* wpa_supplicant (NM stop leaves it running,
        and two supplicants fighting over one radio breaks station association)."""
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return
        subprocess.run(
            [systemctl, "--no-ask-password", "stop", "NetworkManager"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20.0,
        )
        self._nm_stopped = True
        subprocess.run(
            [systemctl, "--no-ask-password", "stop", "wpa_supplicant"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20.0,
        )
        self._wpa_stopped = True
        # The netdev goes down when the system supplicant dies; raise it so the
        # private supplicant can scan/associate and the retained DHCP lease
        # stays operationally usable.
        ip_bin = shutil.which("ip")
        if ip_bin:
            subprocess.run(
                [ip_bin, "link", "set", self.interface, "up"],
                check=False,
                capture_output=True,
                timeout=5.0,
            )
        logger.info(
            "NetworkManager + system wpa_supplicant stopped; picast owns %s",
            self.interface,
        )

    def _apply_lease(self) -> None:
        """Re-apply the captured IPv4 lease (NM's DHCP client died with the stop).

        `ip addr/route replace` is idempotent: if NM's teardown did NOT flush
        the address, this is a no-op; if it did, it restores SSH reachability.
        """
        ip_bin = shutil.which("ip")
        if not ip_bin or not self._lease_addr:
            logger.warning("No station lease captured; wlan0 may lack an IPv4")
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [ip_bin, "addr", "replace", self._lease_addr, "dev", self.interface],
                check=False,
                capture_output=True,
                timeout=5.0,
            )
            if self._lease_gw:
                subprocess.run(
                    [
                        ip_bin, "route", "replace", "default",
                        "via", self._lease_gw, "dev", self.interface,
                    ],
                    check=False,
                    capture_output=True,
                    timeout=5.0,
                )
        logger.info(
            "Re-applied station lease %s%s",
            self._lease_addr,
            f" via {self._lease_gw}" if self._lease_gw else "",
        )

    def start(self) -> bool:
        """Take over the radio: credentials -> stop NM -> private supplicant."""
        ssid = read_active_ssid(self.interface)
        psk = read_active_psk(self.interface)
        if not ssid:
            logger.warning(
                "No active Wi-Fi association found on %s; keeping system supplicant",
                self.interface,
            )
            return False
        wpa = shutil.which("wpa_supplicant")
        if not wpa:
            logger.error("wpa_supplicant binary not found; cannot run private supplicant")
            return False

        # Capture the live lease BEFORE stopping NetworkManager: NM's teardown
        # kills its DHCP client, and the private supplicant only restores L2.
        self._lease_addr, self._lease_gw = read_station_lease(self.interface)

        systemctl = shutil.which("systemctl")
        if systemctl:
            self._stop_system_network_stack()

        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Defensive: kill any stray private supplicant from a previous run.
        with contextlib.suppress(OSError):
            if PID_PATH.exists():
                pid = PID_PATH.read_text(encoding="utf-8").strip()
                if pid.isdigit():
                    subprocess.run(
                        ["kill", pid],
                        check=False,
                        capture_output=True,
                        timeout=3.0,
                    )
                PID_PATH.unlink(missing_ok=True)
        CONF_PATH.write_text(
            build_supplicant_config(ssid, psk, self.device_name, self.config_methods),
            "utf-8",
        )
        CONF_PATH.chmod(0o600)
        # Keep exactly one previous run's log (bounded SD-card use).
        with contextlib.suppress(OSError):
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists():
                LOG_PATH.replace(LOG_PREV_PATH)
        try:
            # Foreground (NO -B): Popen must own the real process so stop() works;
            # with -B the parent exits instantly and the orphaned daemon keeps
            # holding the interface, breaking the system-stack restore.
            self._proc = subprocess.Popen(
                [
                    wpa,
                    "-t",
                    "-g", f"{CTRL_DIR}/global",
                    "-i", self.interface,
                    "-c", str(CONF_PATH),
                    "-P", str(PID_PATH),
                    "-f", str(LOG_PATH),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.error("Failed launching private wpa_supplicant: %s", exc)
            self._restore_nm()
            return False

        for _ in range(40):
            if self._ping():
                logger.info("Private wpa_supplicant ready (ssid=%s)", ssid)
                # Station MUST associate before the GO is created: the
                # autonomous GO inherits the *current* radio channel, and a
                # cross-channel GO on a single-radio chip permanently breaks
                # the station handshake (4-way WRONG_KEY loop, box unreachable).
                # A wrong PSK here fails closed via stop() -> system restore.
                if not self.wait_associated(timeout_s=30.0):
                    logger.error(
                        "Station failed to associate via private supplicant; "
                        "reverting to system network stack (keeping box reachable)"
                    )
                    self.stop(block=True)
                    return False
                logger.info("Station associated; safe to create the GO")
                # L3 restore: without this, the association gate passes on L2
                # while wlan0 has no IPv4 — the exact 797f7fa lockout mode.
                self._apply_lease()
                return True
            if self._proc.poll() is not None:
                logger.error(
                    "Private wpa_supplicant exited early (rc=%s)", self._proc.returncode
                )
                self._restore_nm()
                return False
            time.sleep(0.25)
        logger.warning("Private wpa_supplicant ctrl socket did not come up in 10s")
        self.stop(block=True)
        return False

    def wait_associated(self, timeout_s: float = 25.0) -> bool:
        """Wait until the station is fully associated (so GO freq can follow it)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._is_associated():
                logger.info("Station associated via private supplicant")
                return True
            if self._proc is not None and self._proc.poll() is not None:
                logger.error("Private wpa_supplicant died while associating")
                return False
            time.sleep(0.5)
        logger.warning("Station did not associate within %.0fs", timeout_s)
        return False

    def _restore_nm(self, block: bool = True) -> None:
        """Start the system wpa_supplicant + NetworkManager again.

        `block=False` is REQUIRED on the service-shutdown path: picast.service
        is ordered After= both units, and systemd runs a pending stop before
        any ordered start, so a blocking `systemctl start` issued while our own
        stop job is in progress waits on us while we wait on it. That deadlock
        ran into TimeoutStopSec and got the service SIGKILLed (seen live).
        Queued with --no-block, the starts run as soon as we exit.
        """
        systemctl = shutil.which("systemctl")
        if systemctl and (self._nm_stopped or self._wpa_stopped):
            base = [systemctl, "--no-ask-password"]
            if not block:
                base.append("--no-block")
            units = []
            if self._wpa_stopped:
                units.append("wpa_supplicant")
            if self._nm_stopped:
                units.append("NetworkManager")
            for unit in units:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(
                        [*base, "start", unit],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=20.0,
                    )
            logger.info(
                "System network stack restore %s (%s)",
                "completed" if block else "queued",
                " + ".join(units),
            )
        self._nm_stopped = False
        self._wpa_stopped = False

    def stop(self, block: bool = False) -> None:
        """Terminate the private supplicant and hand the radio back to NetworkManager.

        Default is the non-blocking restore (service shutdown). Callers that
        fall back to the system supplicant while the service keeps running
        pass `block=True` so the system stack is up before they continue.
        """
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logger.warning("Private supplicant resisted SIGTERM; escalating to SIGKILL")
                with contextlib.suppress(OSError):
                    self._proc.kill()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    self._proc.wait(timeout=3.0)
        self._proc = None
        with contextlib.suppress(OSError):
            PID_PATH.unlink(missing_ok=True)
        # Stale ctrl sockets/conf files block the system supplicant from
        # cleanly rebinding the interface after our instance dies.
        with contextlib.suppress(OSError):
            for stale in Path(CTRL_DIR).glob("*"):
                with contextlib.suppress(OSError):
                    stale.unlink(missing_ok=True)
        self._restore_nm(block=block)
        with contextlib.suppress(OSError):
            CONF_PATH.unlink(missing_ok=True)