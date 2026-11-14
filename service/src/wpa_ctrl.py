"""Direct `AF_UNIX`/`SOCK_DGRAM` wpa_ctrl client, replacing per-command `wpa_cli` forks.

wpa_supplicant exposes one UNIX datagram control socket per interface under
`<ctrl_dir>/<iface>` (plus `<ctrl_dir>/global`). Speaking the protocol directly
(`send` command, `recv` reply, client socket bound inside `ctrl_dir` with
`0o600` like `wpa_cli` does) avoids ~15+ `wpa_cli` subprocess forks during
startup, and an `ATTACH`ed monitor socket receives unsolicited events
(`P2P-DEVICE-FOUND`, `AP-STA-*`, `WPS-*`) without pipe-buffering artifacts.

Every entry point falls back to the `wpa_cli` binary when the direct socket is
missing or fails, so behavior degrades gracefully on exotic builds.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
_ATTACH_ACK_TIMEOUT_S = 2.0
_RECV_BUFSIZE = 4096
_REQUEST_BUFSIZE = 8192
_CLIENT_SEQ = itertools.count()


def format_wpa_ctrl_command(cmd: str) -> str:
    """Normalize a wpa_ctrl command so the leading verb is uppercase.

    `wpa_cli` uppercases its command verb internally (`status` -> `STATUS`,
    `set wifi_display 1` -> `SET wifi_display 1`), whereas `wpa_supplicant`'s
    raw `ctrl_iface` socket matches command verbs case-sensitively in uppercase
    while keeping arguments (`wifi_display`, `p2p-wlan0-0`, PINs) intact.
    """
    stripped = cmd.strip()
    verb, sep, rest = stripped.partition(" ")
    return f"{verb.upper()}{sep}{rest}"


class WpaCtrlClient:
    """One command over the UNIX datagram `<ctrl_dir>/<iface>` wpa_ctrl socket."""

    def __init__(self, ctrl_dir: str | os.PathLike[str], iface: str) -> None:
        self._ctrl_dir = Path(ctrl_dir)
        self._iface = iface
        self._sock: socket.socket | None = None
        self._local_path: Path | None = None

    def connect(self) -> None:
        """Bind a private `0o600` socket in `ctrl_dir` and connect to the target."""
        if self._sock is not None:
            return
        target = self._ctrl_dir / self._iface
        if not target.exists():
            raise FileNotFoundError(f"wpa_ctrl socket missing: {target}")
        self._ctrl_dir.mkdir(parents=True, exist_ok=True)
        # `wpa_cli` binds its client socket in the same directory so the kernel
        # labels it for the `GROUP=netdev` access check; 0o600 mirrors that.
        # Include an atomic counter so concurrent worker threads never collide.
        local = self._ctrl_dir / f"picast-{os.getpid()}-{next(_CLIENT_SEQ)}-{self._iface}"
        with contextlib.suppress(OSError):
            local.unlink()  # stale file from a recycled PID would break bind()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.bind(str(local))
            os.chmod(local, 0o600)
            sock.connect(str(target))
            sock.settimeout(_ATTACH_ACK_TIMEOUT_S)
        except OSError:
            sock.close()
            with contextlib.suppress(OSError):
                local.unlink()
            raise
        self._sock = sock
        self._local_path = local

    def request(self, cmd: str, timeout_s: float = 2.0) -> str:
        """Send `cmd` (with uppercase verb) and return the stripped reply text."""
        self.connect()
        sock = self._sock
        if sock is None:  # pragma: no cover - connect() guarantees a socket
            raise ConnectionError("wpa_ctrl client is not connected")
        wire_cmd = format_wpa_ctrl_command(cmd)
        sock.settimeout(timeout_s)
        sock.send(wire_cmd.encode("utf-8"))
        reply = sock.recv(_REQUEST_BUFSIZE)
        text = reply.decode("utf-8", errors="replace").strip()
        if text == "UNKNOWN COMMAND":
            logger.warning("wpa_ctrl %s rejected unknown command %r", self._iface, wire_cmd)
        return text

    def close(self) -> None:
        """Close the socket and unlink the private client socket file."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._local_path is not None:
            with contextlib.suppress(OSError):
                self._local_path.unlink(missing_ok=True)
            self._local_path = None

    def __enter__(self) -> WpaCtrlClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class WpaCtrlMonitor:
    """Async event monitor attached to `<ctrl_dir>/global` (or any iface socket)."""

    def __init__(self, ctrl_dir: str | os.PathLike[str], iface: str = "global") -> None:
        self._ctrl_dir = Path(ctrl_dir)
        self._iface = iface
        self._sock: socket.socket | None = None
        self._local_path: Path | None = None

    async def attach(self) -> bool:
        """Bind, `ATTACH`, and confirm the `OK` reply; False when unavailable."""
        target = self._ctrl_dir / self._iface
        if not target.exists():
            return False
        loop = asyncio.get_running_loop()
        try:
            self._ctrl_dir.mkdir(parents=True, exist_ok=True)
            local = self._ctrl_dir / f"picast-mon-{os.getpid()}"
            with contextlib.suppress(OSError):
                local.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.bind(str(local))
                os.chmod(local, 0o600)
                sock.connect(str(target))
                sock.setblocking(False)
            except OSError:
                sock.close()
                with contextlib.suppress(OSError):
                    local.unlink()
                raise
            await loop.sock_sendall(sock, b"ATTACH")
            reply = await asyncio.wait_for(
                loop.sock_recv(sock, _RECV_BUFSIZE), timeout=_ATTACH_ACK_TIMEOUT_S
            )
            if not reply.startswith(b"OK"):
                sock.close()
                with contextlib.suppress(OSError):
                    local.unlink()
                return False
        except (OSError, TimeoutError):
            return False
        self._sock = sock
        self._local_path = local
        return True

    async def recv_event(self, timeout_s: float) -> str | None:
        """Await one event line, or None on timeout / detached socket."""
        sock = self._sock
        if sock is None:
            return None
        loop = asyncio.get_running_loop()
        try:
            raw = await asyncio.wait_for(
                loop.sock_recv(sock, _RECV_BUFSIZE), timeout=timeout_s
            )
        except TimeoutError:
            return None
        return raw.decode("utf-8", errors="replace").strip()

    async def close(self) -> None:
        """Best-effort `DETACH`, then close and unlink the monitor socket."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.send(b"DETACH")
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._local_path is not None:
            with contextlib.suppress(OSError):
                self._local_path.unlink(missing_ok=True)
            self._local_path = None


def request_via_cli(
    ctrl_dir: str | os.PathLike[str] | None,
    iface: str,
    cmd: str,
    timeout_s: float = 3.0,
) -> str | None:
    """Run one `wpa_cli` command as a subprocess fallback; None on any failure."""
    cli = shutil.which("wpa_cli")
    if cli is None:
        return None
    argv = [cli]
    if ctrl_dir is not None:
        argv += ["-p", str(ctrl_dir)]
    argv += ["-i", iface, *cmd.split()]
    try:
        res = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("wpa_cli -i %s %s timed out/failed: %s", iface, cmd, exc)
        return None
    if res.returncode != 0:
        logger.debug(
            "wpa_cli -i %s %s exited %d: %s", iface, cmd, res.returncode, res.stderr.strip()
        )
        return res.stdout.strip() or None
    return res.stdout.strip()


def wpa_request(
    ctrl_dir: str | os.PathLike[str] | None,
    iface: str,
    cmd: str,
    timeout_s: float = 3.0,
) -> str | None:
    """One control-plane command: direct datagram socket first, `wpa_cli` fallback."""
    if ctrl_dir is not None:
        try:
            with WpaCtrlClient(ctrl_dir, iface) as client:
                return client.request(cmd, timeout_s)
        except OSError as exc:
            logger.debug("Direct wpa_ctrl %s %s failed (%s); trying wpa_cli", iface, cmd, exc)
    return request_via_cli(ctrl_dir, iface, cmd, timeout_s)
