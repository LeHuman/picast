"""High-legibility color-block TTY & framebuffer splash screen and WPS PIN policy."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import PinMode

logger = logging.getLogger(__name__)
DEFAULT_RUNTIME_PIN_PATH: Final[Path] = Path("/run/picast/pin")

_FONT_5X7: Final[dict[str, tuple[str, ...]]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00110", "01000", "10000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


def compute_wps_checksum_digit(seven_digits: str) -> str:
    """Compute the 8th WPS PIN check digit per Wi-Fi Simple Configuration spec."""
    val = int(seven_digits) * 10
    accum = 0
    accum += 3 * ((val // 10_000_000) % 10)
    accum += 1 * ((val // 1_000_000) % 10)
    accum += 3 * ((val // 100_000) % 10)
    accum += 1 * ((val // 10_000) % 10)
    accum += 3 * ((val // 1_000) % 10)
    accum += 1 * ((val // 100) % 10)
    accum += 3 * ((val // 10) % 10)
    return str((10 - (accum % 10)) % 10)


def generate_wps_pin() -> str:
    """Generate a cryptographically random valid 8-digit WPS PIN."""
    seven = f"{secrets.randbelow(10_000_000):07d}"
    return seven + compute_wps_checksum_digit(seven)


class PinManager:
    """Manages active WPS PIN across reboot, session, fixed, and PBC policies."""

    def __init__(
        self,
        mode: PinMode,
        fixed_pin: str = "12345670",
        runtime_path: Path = DEFAULT_RUNTIME_PIN_PATH,
    ) -> None:
        self._mode = mode
        self._fixed_pin = fixed_pin
        self._runtime_path = runtime_path
        self._current_pin: str | None = None

    def initialize(self) -> str | None:
        """Return initial active PIN (persisting `per-reboot` PIN in `/run/picast/pin`)."""
        if self._mode is PinMode.NONE:
            self._current_pin = None
            return None
        if self._mode is PinMode.FIXED:
            self._current_pin = self._fixed_pin
            return self._current_pin
        if self._mode is PinMode.PER_REBOOT:
            try:
                if self._runtime_path.exists():
                    existing = self._runtime_path.read_text(encoding="utf-8").strip()
                    if len(existing) == 8 and existing.isdigit():
                        self._current_pin = existing
                        return existing
            except OSError as exc:
                logger.warning("Failed reading cached reboot PIN %s: %s", self._runtime_path, exc)
            pin = generate_wps_pin()
            try:
                self._runtime_path.parent.mkdir(parents=True, exist_ok=True)
                self._runtime_path.write_text(pin + "\n", encoding="utf-8")
            except OSError as exc:
                logger.warning("Failed persisting reboot PIN %s: %s", self._runtime_path, exc)
            self._current_pin = pin
            return pin

        self._current_pin = generate_wps_pin()
        return self._current_pin

    def on_session_ended(self) -> str | None:
        """Rotate PIN after disconnect only when configured for `per-session` mode."""
        if self._mode is PinMode.PER_SESSION:
            self._current_pin = generate_wps_pin()
        return self._current_pin

    @property
    def current_pin(self) -> str | None:
        return self._current_pin


@dataclass(frozen=True, slots=True)
class SplashState:
    """Visual payload rendered to TTY1 / `/dev/fb0` while idle or connecting."""

    device_name: str
    transport_label: str
    pin_display: str
    status_line: str
    ip_hint: str = ""


def render_color_block_text(text: str, fg_code: str = "97", on_char: str = "██") -> list[str]:
    """Render uppercase ASCII text into crisp 7-row ANSI color-block banners."""
    cleaned = "".join(ch if ch in _FONT_5X7 else " " for ch in text.upper())
    rows: list[str] = []
    for row_idx in range(7):
        parts: list[str] = []
        for ch in cleaned:
            bits = _FONT_5X7[ch][row_idx]
            rendered = "".join(on_char if b == "1" else "  " for b in bits)
            parts.append(rendered)
        rows.append(f"\033[{fg_code}m" + "  ".join(parts) + "\033[0m")
    return rows


def format_ansi_splash(state: SplashState) -> str:
    """Build full-screen high-contrast ANSI color-block splash for `/dev/tty1`."""
    pin_formatted = (
        f"{state.pin_display[:4]} {state.pin_display[4:]}"
        if len(state.pin_display) == 8 and state.pin_display.isdigit()
        else state.pin_display
    )
    name_blocks = render_color_block_text(state.device_name[:12], fg_code="96")
    pin_blocks = render_color_block_text(pin_formatted, fg_code="93")

    lines = [
        "\033[2J\033[H\033[?25l",
        "\033[44;97m  PICAST  |  WIN + K WIRELESS DISPLAY RECEIVER  \033[0m",
        "",
        *name_blocks,
        "",
        f"\033[42;30m TRANSPORT: {state.transport_label} \033[0m  "
        + (f"\033[40;97m {state.ip_hint} \033[0m" if state.ip_hint else ""),
        "",
        "\033[90mWPS PIN:\033[0m",
        *pin_blocks,
        "",
        f"\033[97mSTATUS: {state.status_line}\033[0m",
    ]
    return "\n".join(lines) + "\n"


_FB_BG_CACHE: dict[tuple[int, int], bytes] = {}


def _fb_background(width: int, height: int) -> bytes:
    """Immutable full-screen background buffer, cached per resolution.

    Building `bg_pixel * (width * height)` on every redraw costs megabytes of
    allocation for an identical result; cache the template once per (w, h) and
    copy it into a fresh mutable `bytearray` per redraw.
    """
    key = (width, height)
    bg = _FB_BG_CACHE.get(key)
    if bg is None:
        bg_pixel = b"\x1e\x14\x0f\xff"
        bg = bg_pixel * (width * height)
        _FB_BG_CACHE[key] = bg
    return bg


def _write_fb0_splash(state: SplashState, fb_path: Path = Path("/dev/fb0")) -> bool:
    """Attempt pixel-exact 32-bpp BGRA color-block rendering directly to `/dev/fb0`."""
    sysfs_size = Path("/sys/class/graphics/fb0/virtual_size")
    sysfs_bpp = Path("/sys/class/graphics/fb0/bits_per_pixel")
    if not (fb_path.exists() and sysfs_size.exists() and sysfs_bpp.exists()):
        return False

    try:
        w_str, h_str = sysfs_size.read_text(encoding="utf-8").strip().split(",")
        width, height = int(w_str), int(h_str)
        bpp = int(sysfs_bpp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False

    if bpp != 32 or width < 640 or height < 480:
        return False

    stride = width * 4
    canvas = bytearray(_fb_background(width, height))

    def draw_block_text(text: str, y0: int, scale: int, color_bgra: bytes) -> None:
        cleaned = "".join(ch if ch in _FONT_5X7 else " " for ch in text.upper())
        total_cols = len(cleaned) * 6 - 1
        start_x = max(20, (width - total_cols * scale) // 2)
        for idx, ch in enumerate(cleaned):
            glyph = _FONT_5X7[ch]
            gx = start_x + idx * 6 * scale
            for r_i, row_bits in enumerate(glyph):
                for c_i, bit in enumerate(row_bits):
                    if bit != "1":
                        continue
                    px0 = gx + c_i * scale
                    py0 = y0 + r_i * scale
                    for dy in range(scale):
                        py = py0 + dy
                        if py >= height:
                            continue
                        off = py * stride + px0 * 4
                        span = min(scale, max(0, width - px0))
                        if span > 0:
                            canvas[off : off + span * 4] = color_bgra * span

    pin_text = (
        f"{state.pin_display[:4]} {state.pin_display[4:]}"
        if len(state.pin_display) == 8 and state.pin_display.isdigit()
        else state.pin_display
    )
    draw_block_text("PICAST", max(40, height // 10), max(6, width // 140), b"\xff\xd8\x48\xff")
    draw_block_text(
        state.device_name[:12], max(140, height // 4), max(8, width // 110), b"\xff\xff\xff\xff"
    )
    draw_block_text(
        pin_text, max(280, height // 2), max(12, width // 80), b"\x52\xd2\xff\xff"
    )
    draw_block_text(
        state.transport_label[:14],
        max(420, (height * 3) // 4),
        max(5, width // 170),
        b"\x9e\xe8\x44\xff",
    )

    try:
        with fb_path.open("r+b", buffering=0) as fb:
            fb.write(canvas)
        return True
    except OSError as exc:
        logger.debug("Framebuffer splash write skipped (%s): %s", fb_path, exc)
        return False


def render_splash(state: SplashState, tty_path: Path = Path("/dev/tty1")) -> None:
    """Render high-contrast color-block splash to `/dev/fb0` (if 32-bpp) or TTY1."""
    if _write_fb0_splash(state):
        return
    rendered = format_ansi_splash(state)
    try:
        tty_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        logger.debug("TTY splash write skipped (%s): %s", tty_path, exc)
