"""Pure-Python WFD UIBC HIDC keyboard/mouse passthrough (upstream ``d2.py`` parity).

Zero external dependencies: instead of ``python3-evdev`` or X11, this module
reads raw ``/dev/input/event*`` devices in non-blocking mode via ``os.open``,
translates ``input_event`` records (``struct.Struct("llHHi")``) into WFD UIBC
HIDC reports, and forwards them over the TCP socket negotiated during the WFD
M3/M4 handshake. Devices are discovered through ``/sys/class/input`` sysfs
capability bitmaps (so HDMI-audio nodes like ``vc4-hdmi`` are never grabbed)
and exclusively claimed with ``EVIOCGRAB`` so the local desktop ignores input
while mirroring.

Wire format (matching upstream ``d2.py``):
* Keyboard report (18 bytes): ``00010012010000000929 <mod> 00 <usage> 0000000000``
* Mouse report (16 bytes): ``00010010010100000628 <btn> <dx> <dy> <wheel> 0000``

Unlike upstream, ``dx``/``dy``/``wheel`` are clamped to ``[-127, 127]`` before
two's-complement encoding so fast pointer motion never wraps around.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Linux input subsystem constants (linux/input-event-codes.h subset).
EV_SYN: Final[int] = 0x00
EV_KEY: Final[int] = 0x01
EV_REL: Final[int] = 0x02

KEY_A: Final[int] = 30  # presence of KEY_A identifies real keyboards in sysfs
BTN_LEFT: Final[int] = 272
BTN_RIGHT: Final[int] = 273
BTN_MIDDLE: Final[int] = 274
REL_X: Final[int] = 0
REL_Y: Final[int] = 1
REL_WHEEL: Final[int] = 8

EVIOCGRAB: Final[int] = 0x40044590  # _IOC(_IOC_WRITE, 'E', 0x90, sizeof(int))

# 64-bit /dev/input/input_event layout: tv_sec, tv_usec, type, code, value.
# (Matches 32-bit layouts too: struct "l" scales with the platform like the
# kernel's input_event timeval does.)
INPUT_EVENT: Final[struct.Struct] = struct.Struct("llHHi")
_READ_CHUNK_BYTES: Final[int] = 4096
_MAX_TX_BUF_BYTES: Final[int] = 65536

# HIDC packet framing headers (upstream ``m7`` prefixes from d2.py).
_KEYBOARD_HEADER: Final[bytes] = bytes.fromhex("00010012010000000929")
_MOUSE_HEADER: Final[bytes] = bytes.fromhex("00010010010100000628")

# Linux keycodes that update the HIDC keyboard modifier bitmap. Upstream
# d2.py: L-Ctrl=29, L-Shift=42, L-Alt=56, L-Meta=125, R-Ctrl=97, R-Shift=54,
# R-Alt=100, R-Meta=126 (0x7e).
MODIFIER_KEY_BITS: Final[dict[int, int]] = {
    29: 0x01,  # KEY_LEFTCTRL
    42: 0x02,  # KEY_LEFTSHIFT
    56: 0x04,  # KEY_LEFTALT
    125: 0x08,  # KEY_LEFTMETA
    97: 0x10,  # KEY_RIGHTCTRL
    54: 0x20,  # KEY_RIGHTSHIFT
    100: 0x40,  # KEY_RIGHTALT
    126: 0x80,  # KEY_RIGHTMETA
}

# The exact six HIDC report descriptors sent right after TCP connect, from
# upstream d2.py lines 314-319 (m1..m6). Byte 2-3 of each is its own length.
HIDC_DESCRIPTORS: Final[tuple[bytes, ...]] = (
    # Upstream d2.py m1.
    bytes.fromhex(
        "1001003e2ab6010101003305010902a10185280901a100050919012908150025019508750181020501093009"
        "3109380a38021581257f750895048106c0c0"),
    # Upstream d2.py m2.
    bytes.fromhex(
        "1001004c3ce2010001004105010906a1018529050719e029e715002501750195088102950175088103950575"
        "01050819012905910295017503910395067508150025650507190029658100c0"),
    # Upstream d2.py m3.
    bytes.fromhex(
        "100100243ce20107010019050c0901a101852a19002aff00150026ff00950175108100c0"),
    # Upstream d2.py m4.
    bytes.fromhex(
        "1001027a3ce2010301026f050d0904a1018513050d095495017508150025638102550c66011047ffff000027"
        "ffff00007510950109568102050d0922a1021500250135004500550065007501950109428102094781029504"
        "81030600ff09109501750281027508050d095126630081027510550e651127ff7f0000464006094881020949"
        "8102050109308102468403050109318102050d651447a08c000027a08c0000093f8102c0050d0922a1021500"
        "25013500450055006500750195010942810209478102950481030600ff09109501750281027508050d095126"
        "630081027510550e651127ff7f00004640060948810209498102050109308102468403050109318102050d65"
        "1447a08c000027a08c0000093f8102c0050d0922a10215002501350045005500650075019501094281020947"
        "8102950481030600ff09109501750281027508050d095126630081027510550e651127ff7f00004640060948"
        "810209498102050109308102468403050109318102050d651447a08c000027a08c0000093f8102c0050d0922"
        "a102150025013500450055006500750195010942810209478102950481030600ff0910950175028102750805"
        "0d095126630081027510550e651127ff7f000046400609488102094981020501093081024684030501093181"
        "02050d651447a08c000027a08c0000093f8102c0050d0922a102150025013500450055006500750195010942"
        "810209478102950481030600ff09109501750281027508050d095126630081027510550e651127ff7f000046"
        "40060948810209498102050109308102468403050109318102050d651447a08c000027a08c0000093f8102c0"
        "050d85120955950175101500266400b102c0"),
    # Upstream d2.py m5.
    bytes.fromhex(
        "1001000e3ce20103000003120a00"),
    # Upstream d2.py m6.
    bytes.fromhex(
        "100100d0422801060100c4050d0902a1018514050d0920a10009420944093c09450932150025017501950581"
        "0295038103050127ff7f000075109501550e6533093035004640068142093146840381426500050d09308102"
        "0600ff09118102091281027520050609228102c0c0050d0902a1018515050d0920a10009420944093c094509"
        "321500250175019505810295038103050127ff7f000075109501550e65330930350046400681420931468403"
        "81426500050d093081020600ff09118102091281027520050609228102c0c000"),
)

# The exact Linux keycode -> USB HID Usage ID translation table from upstream
# d2.py lines 66-67 (``tohid``), indexed by Linux keycode 0..127.
LINUX_KEY_TO_HID: Final[tuple[int, ...]] = (
    # keycodes 0x00-0x07
    0, 41, 30, 31, 32, 33, 34, 35,
    # keycodes 0x08-0x0F
    36, 37, 38, 39, 45, 46, 42, 43,
    # keycodes 0x10-0x17
    20, 26, 8, 21, 23, 28, 24, 12,
    # keycodes 0x18-0x1F
    18, 19, 47, 48, 40, 0, 4, 22,
    # keycodes 0x20-0x27
    7, 9, 10, 11, 13, 14, 15, 51,
    # keycodes 0x28-0x2F
    52, 53, 0, 49, 29, 27, 6, 25,
    # keycodes 0x30-0x37
    5, 17, 16, 54, 55, 56, 0, 85,
    # keycodes 0x38-0x3F
    0, 44, 57, 58, 59, 60, 61, 62,
    # keycodes 0x40-0x47
    63, 64, 65, 66, 67, 83, 71, 95,
    # keycodes 0x48-0x4F
    96, 97, 86, 92, 93, 94, 87, 89,
    # keycodes 0x50-0x57
    90, 91, 98, 99, 0, 0, 0, 68,
    # keycodes 0x58-0x5F
    69, 0, 0, 0, 0, 0, 0, 0,
    # keycodes 0x60-0x67
    88, 0, 84, 70, 0, 0, 74, 82,
    # keycodes 0x68-0x6F
    75, 80, 79, 77, 81, 78, 73, 76,
    # keycodes 0x70-0x77
    0, 127, 129, 128, 0, 0, 0, 72,
    # keycodes 0x78-0x7F
    0, 0, 0, 0, 0, 0, 0, 101,
)


def _to_signed_byte(value: int) -> int:
    """Clamp ``value`` to [-127, 127] and encode as a two's-complement byte."""
    return max(-127, min(127, int(value))) & 0xFF


def encode_keyboard_report(modifier_mask: int, usage_id: int) -> bytes:
    """Build the 18-byte HIDC keyboard report ``...0929 <mod> 00 <usage> 0*5``."""
    return _KEYBOARD_HEADER + bytes(
        (modifier_mask & 0xFF, 0x00, usage_id & 0xFF)
    ) + b"\x00" * 5


def encode_mouse_report(
    button_mask: int, dx: int = 0, dy: int = 0, wheel: int = 0
) -> bytes:
    """Build the 16-byte HIDC mouse report ``...0628 <btn> <dx> <dy> <wheel> 0000``."""
    return _MOUSE_HEADER + bytes(
        (
            button_mask & 0xFF,
            _to_signed_byte(dx),
            _to_signed_byte(dy),
            _to_signed_byte(wheel),
        )
    ) + b"\x00" * 2


class UibcHidcState:
    """Per-device Linux ``input_event`` -> HIDC report translator.

    Mirrors upstream ``d2.py`` ``hidcprocessing``: modifier keys update a sticky
    bitmap, every key event emits a keyboard report (release encoded as usage
    0), mouse buttons 272-274 map to bits 1/2/4, and relative motion is sent as
    single-axis mouse reports.
    """

    def __init__(self) -> None:
        self.keyboard_mask: int = 0
        self.mouse_mask: int = 0

    def translate(self, event_type: int, code: int, value: int) -> list[bytes]:
        """Translate one ``input_event`` into zero or more HIDC report packets."""
        if event_type == EV_SYN:
            return []
        if event_type == EV_KEY:
            if code < BTN_LEFT:
                return self._translate_key(code, value)
            if code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                bit = 1 << (code - BTN_LEFT)
                if value:
                    self.mouse_mask |= bit
                else:
                    self.mouse_mask &= ~bit
                return [encode_mouse_report(self.mouse_mask)]
            return []
        if event_type == EV_REL:
            if code == REL_X:
                return [encode_mouse_report(self.mouse_mask, dx=value)]
            if code == REL_Y:
                return [encode_mouse_report(self.mouse_mask, dy=value)]
            if code == REL_WHEEL:
                return [encode_mouse_report(self.mouse_mask, wheel=value)]
        return []

    def _translate_key(self, code: int, value: int) -> list[bytes]:
        modifier_bit = MODIFIER_KEY_BITS.get(code)
        if modifier_bit is not None:
            if value:
                self.keyboard_mask |= modifier_bit
            else:
                self.keyboard_mask &= ~modifier_bit
            usage = 0
        elif value and code < len(LINUX_KEY_TO_HID):
            usage = LINUX_KEY_TO_HID[code]
        else:
            usage = 0
        return [encode_keyboard_report(self.keyboard_mask, usage)]


def _bitmap_bit_set(content: str, bit: int, word_bits: int | None = None) -> bool:
    """Check one bit inside a sysfs capabilities hex bitmap.

    Sysfs prints capability bitmaps as MSB-first space-separated hex words, so
    bit ``N`` lives in ``words[-1 - (N // word_bits)]``. ``word_bits`` is the
    platform machine word (64 on aarch64/x86_64, 32 on armhf), derived at
    runtime from ``struct.calcsize("l")`` so it always matches the kernel that
    printed the file. Zero words above the highest set word are trimmed by the
    kernel, which right-indexing from the end tolerates for both padded and
    trimmed renderings.
    """
    if word_bits is None:
        word_bits = struct.calcsize("l") * 8
    words = content.split()
    if not words:
        return False
    index = len(words) - 1 - (bit // word_bits)
    if index < 0:
        return False
    try:
        word_value = int(words[index], 16)
    except ValueError:
        return False
    return bool((word_value >> (bit % word_bits)) & 1)


def discover_hid_event_paths(
    sys_input_root: Path = Path("/sys/class/input"),
    dev_input_root: Path = Path("/dev/input"),
) -> list[Path]:
    """Return ``/dev/input/event*`` paths belonging to real keyboards or mice.

    A device counts as a keyboard when its ``capabilities/key`` bitmap has
    ``KEY_A`` (bit 30) and as a mouse when it has ``BTN_LEFT`` (bit 272) plus
    ``EV_REL`` bits 0x3 (REL_X|REL_Y). Nodes such as ``vc4-hdmi`` (which expose
    ``rel=3`` but neither ``KEY_A`` nor ``BTN_LEFT``) are therefore ignored.
    """
    if not sys_input_root.is_dir():
        return []

    event_paths: list[Path] = []
    for entry in sorted(sys_input_root.iterdir()):
        name = entry.name
        if not name.startswith("event") or not name[len("event"):].isdigit():
            continue
        capabilities_dir = entry / "device" / "capabilities"
        try:
            key_bits = (capabilities_dir / "key").read_text(encoding="ascii", errors="replace")
            rel_bits = (capabilities_dir / "rel").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        is_keyboard = _bitmap_bit_set(key_bits, KEY_A)
        is_mouse = (
            _bitmap_bit_set(key_bits, BTN_LEFT)
            and _bitmap_bit_set(rel_bits, REL_X)
            and _bitmap_bit_set(rel_bits, REL_Y)
        )
        if (is_keyboard or is_mouse) and (dev_input_root / name).exists():
            event_paths.append(dev_input_root / name)
    return event_paths


@dataclass(slots=True)
class _GrabbedInputDevice:
    """An exclusively-grabbed ``/dev/input/event*`` descriptor plus state."""

    path: Path
    fd: int
    state: UibcHidcState


class UibcHidcBridge:
    """Connects to the WFD source's UIBC HIDC TCP port and streams HID reports.

    Lifecycle: ``start()`` connects (short retry loop), sends the six HIDC
    descriptors with 10 ms gaps, opens and ``EVIOCGRAB``s every discovered
    keyboard/mouse, and registers ``loop.add_reader`` callbacks to translate
    ``input_event`` records and push them over TCP. ``stop()``/``run()`` tear
    everything down; every failure path (no input devices, refused TCP, EOF)
    degrades gracefully to a log message without raising.
    """

    def __init__(
        self,
        peer_ip: str,
        port: int,
        *,
        sys_input_root: Path = Path("/sys/class/input"),
        dev_input_root: Path = Path("/dev/input"),
        connect_attempts: int = 5,
        connect_delay_s: float = 0.1,
        connect_timeout_s: float = 1.0,
        descriptor_gap_s: float = 0.01,
        hotplug_rescan_s: float = 2.0,
    ) -> None:
        self._peer_ip = peer_ip
        self._port = port
        self._sys_input_root = sys_input_root
        self._dev_input_root = dev_input_root
        self._connect_attempts = connect_attempts
        self._connect_delay_s = connect_delay_s
        self._connect_timeout_s = connect_timeout_s
        self._descriptor_gap_s = descriptor_gap_s
        self._hotplug_rescan_s = hotplug_rescan_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sock: socket.socket | None = None
        self._devices: list[_GrabbedInputDevice] = []
        self._tx_buf = bytearray()
        self._writer_active = False
        self._closed = False
        self._done = asyncio.Event()

    async def run(self) -> None:
        """Start the bridge and serve until cancelled or the TCP link drops."""
        try:
            if await self.start():
                # Periodically re-scan for newly plugged keyboards/mice:
                # devices hot-plugged after the M4 negotiation would otherwise
                # never be forwarded.
                while True:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._done.wait()), timeout=self._hotplug_rescan_s
                        )
                        break  # _done set
                    except TimeoutError:
                        self.rescan_for_new_devices()
        finally:
            self.close()

    def rescan_for_new_devices(self) -> int:
        """Re-scan sysfs for hot-plugged keyboards/mice and grab new ones.

        Returns the current device count. Safe to call repeatedly; only
        paths not already grabbed are opened.
        """
        if self._closed or self._loop is None or self._sock is None:
            return 0
        before = len(self._devices)
        known = {device.path for device in self._devices}
        for path in discover_hid_event_paths(self._sys_input_root, self._dev_input_root):
            if path not in known:
                self._open_and_grab(path, self._loop)
        if len(self._devices) != before:
            logger.info("UIBC HIDC bridge now forwarding %d input device(s)", len(self._devices))
        return len(self._devices)

    async def start(self) -> bool:
        """Connect, handshake, and grab input devices. False on graceful failure."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        sock = await self._connect()
        if sock is None:
            return False
        self._sock = sock

        for descriptor in HIDC_DESCRIPTORS:
            try:
                await asyncio.wait_for(loop.sock_sendall(sock, descriptor), timeout=2.0)
            except (OSError, TimeoutError) as exc:
                logger.warning("UIBC HIDC descriptor send to %s failed: %s", self._peer_ip, exc)
                self.close()
                return False
            await asyncio.sleep(self._descriptor_gap_s)

        for path in discover_hid_event_paths(self._sys_input_root, self._dev_input_root):
            self._open_and_grab(path, loop)
        logger.info(
            "UIBC HIDC bridge to %s:%d forwarding %d input device(s)",
            self._peer_ip,
            self._port,
            len(self._devices),
        )
        return True

    def close(self) -> None:
        """Unregister readers/writer, release EVIOCGRAB, close fds and TCP. Idempotent."""
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        if self._writer_active and loop is not None and self._sock is not None:
            with contextlib.suppress(KeyError, ValueError, RuntimeError):
                loop.remove_writer(self._sock.fileno())
        self._writer_active = False
        self._tx_buf.clear()
        for device in list(self._devices):
            if loop is not None:
                with contextlib.suppress(KeyError, ValueError, RuntimeError):
                    loop.remove_reader(device.fd)
            self._ungrab_and_close(device.fd)
        self._devices.clear()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        self._done.set()

    async def _connect(self) -> socket.socket | None:
        """Open the TCP session with a short bounded retry loop."""
        address = (self._peer_ip, self._port)
        last_error: OSError | TimeoutError | None = None
        for attempt in range(1, self._connect_attempts + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setblocking(False)
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().sock_connect(sock, address),
                    timeout=self._connect_timeout_s,
                )
            except (OSError, TimeoutError) as exc:
                last_error = exc
                sock.close()
                if attempt < self._connect_attempts:
                    await asyncio.sleep(self._connect_delay_s)
                continue
            return sock
        logger.warning(
            "UIBC HIDC connect to %s:%d failed after %d attempts: %s",
            self._peer_ip,
            self._port,
            self._connect_attempts,
            last_error,
        )
        return None

    def _open_and_grab(self, path: Path, loop: asyncio.AbstractEventLoop) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            logger.debug("UIBC HIDC cannot open %s: %s", path, exc)
            return
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as exc:
            # Non-fatal: without CAP syscall rights we still receive events
            # (they are merely echoed to the local desktop as well).
            logger.debug("EVIOCGRAB failed on %s: %s", path, exc)
        device = _GrabbedInputDevice(path=path, fd=fd, state=UibcHidcState())
        self._devices.append(device)
        loop.add_reader(fd, self._on_input_ready, fd, device.state)

    def _enqueue_tx(self, packet: bytes) -> bool:
        """Send non-blocking or queue remaining bytes via `loop.add_writer`."""
        sock = self._sock
        loop = self._loop
        if sock is None or loop is None:
            return False
        if not self._tx_buf:
            try:
                sent = sock.send(packet)
            except BlockingIOError:
                sent = 0
            except OSError as exc:
                logger.debug("UIBC HIDC send error (%s); shutting bridge down", exc)
                self.close()
                return False
            if sent == len(packet):
                return True
            packet = packet[sent:]
        if len(self._tx_buf) + len(packet) > _MAX_TX_BUF_BYTES:
            logger.debug("UIBC HIDC TX queue full; dropping packet under backpressure")
            return True
        self._tx_buf.extend(packet)
        if not self._writer_active:
            try:
                loop.add_writer(sock.fileno(), self._on_tx_ready)
                self._writer_active = True
            except (OSError, ValueError, RuntimeError) as exc:
                logger.debug("UIBC HIDC add_writer failed (%s); shutting bridge down", exc)
                self.close()
                return False
        return True

    def _on_tx_ready(self) -> None:
        """Flush buffered HIDC packets when TCP socket becomes writable."""
        sock = self._sock
        loop = self._loop
        if sock is None or loop is None or self._closed:
            return
        while self._tx_buf:
            try:
                sent = sock.send(self._tx_buf)
            except BlockingIOError:
                return
            except OSError as exc:
                logger.debug("UIBC HIDC TX flush error (%s); shutting bridge down", exc)
                self.close()
                return
            if sent <= 0:
                self.close()
                return
            del self._tx_buf[:sent]
        if self._writer_active:
            with contextlib.suppress(KeyError, ValueError, RuntimeError):
                loop.remove_writer(sock.fileno())
            self._writer_active = False

    def _on_input_ready(self, fd: int, state: UibcHidcState) -> None:
        if self._sock is None:
            return
        while True:
            try:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
            except BlockingIOError:
                return
            except OSError:
                self._release_device(fd)
                return
            if not chunk:
                # Device unplugged: drop just this reader and keep going.
                self._release_device(fd)
                return
            usable = len(chunk) - (len(chunk) % INPUT_EVENT.size)
            for _tv_sec, _tv_usec, event_type, code, value in INPUT_EVENT.iter_unpack(
                chunk[:usable]
            ):
                for packet in state.translate(event_type, code, value):
                    if not self._enqueue_tx(packet):
                        return

    def _release_device(self, fd: int) -> None:
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(KeyError, ValueError, RuntimeError):
                loop.remove_reader(fd)
        self._ungrab_and_close(fd)
        self._devices = [device for device in self._devices if device.fd != fd]

    @staticmethod
    def _ungrab_and_close(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, EVIOCGRAB, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
