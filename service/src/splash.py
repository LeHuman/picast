"""High-legibility color-block TTY & framebuffer splash screen and WPS PIN policy."""

from __future__ import annotations

import logging
import math
import random
import secrets
import threading
import time
from collections.abc import Callable
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
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


# Splash palette — deep charcoal-navy field with a warm amber hero.
# FB colors are BGRA byte tuples; `_fb_pattern` packs them into the framebuffer's
# native layout (BGRA at 32-bpp, little-endian RGB565 at 16-bpp).
# ANSI values are matching 24-bit truecolor SGR codes for `/dev/tty1`.
_FB_BG_PIXEL: Final[bytes] = b"\x1a\x12\x0c\xff"  # RGB(12, 18, 26) deep charcoal navy
_FB_WORDMARK_COLOR: Final[bytes] = b"\x76\x66\x5a\xff"  # RGB(90, 102, 118) dim slate
_FB_ACCENT_COLOR: Final[bytes] = b"\x52\xd2\xff\xff"  # RGB(255, 210, 82) warm amber
_FB_PIN_COLOR: Final[bytes] = _FB_ACCENT_COLOR  # hero: amber, brightest element
_FB_DEVICE_COLOR: Final[bytes] = b"\xf4\xed\xe8\xff"  # RGB(232, 237, 244) warm white
_FB_STATUS_COLOR: Final[bytes] = b"\xb8\xa3\x94\xff"  # RGB(148, 163, 184) muted slate
_FB_TRANSPORT_COLOR: Final[bytes] = b"\x76\x66\x5a\xff"  # dimmest slate footer
_FB_STAR_COLOR: Final[bytes] = b"\xc8\xaa\x96\xff"  # RGB(150,170,200) faint cool starlight

_ANSI_RESET: Final[str] = "\033[0m"
_ANSI_WORDMARK: Final[str] = "\033[38;2;90;102;118m"
_ANSI_ACCENT: Final[str] = "\033[38;2;255;210;82m"
_ANSI_PIN_CODE: Final[str] = "38;2;255;210;82"
_ANSI_DEVICE: Final[str] = "\033[38;2;232;237;244m"
_ANSI_STATUS: Final[str] = "\033[38;2;148;163;184m"
_ANSI_TRANSPORT: Final[str] = "\033[38;2;90;102;118m"


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


def _format_pin_text(pin_display: str) -> str:
    """Group an 8-digit WPS PIN as `NNNN NNNN` for couch-distance legibility."""
    if len(pin_display) == 8 and pin_display.isdigit():
        return f"{pin_display[:4]} {pin_display[4:]}"
    return pin_display


def format_ansi_splash(state: SplashState) -> str:
    """Build full-screen ANSI splash for `/dev/tty1` mirroring the fb0 hierarchy.

    Same five-element stack as `build_fb_canvas`: dim `PICAST` wordmark, amber
    rule, the PIN as the hero, the device name under it, and status/transport
    pinned to the bottom.
    """
    pin_blocks = render_color_block_text(
        _format_pin_text(state.pin_display), fg_code=_ANSI_PIN_CODE
    )
    # The device name is redundant when it matches the wordmark; the WIN + K
    # shortcut is the actionable hint and always shown.
    device_name = state.device_name.upper()[:12]
    extra_name = device_name if device_name != "PICAST" else ""
    lines = [
        "\033[2J\033[H\033[?25l",
        "",
        f"{_ANSI_WORDMARK}P I C A S T{_ANSI_RESET}",
        f"{_ANSI_ACCENT}{'─' * 22}{_ANSI_RESET}",
        "",
        *pin_blocks,
        "",
        f"{_ANSI_DEVICE}WIN + K{_ANSI_RESET}",
    ]
    if extra_name:
        lines.append(f"{_ANSI_DEVICE}{extra_name}{_ANSI_RESET}")
    lines += [
        "",
        "",
        f"{_ANSI_STATUS}{state.status_line[:32]}{_ANSI_RESET}",
        f"{_ANSI_TRANSPORT}{state.transport_label[:16]}{_ANSI_RESET}",
    ]
    return "\n".join(lines) + "\n"


def _scale_color(color: bytes, factor: float) -> bytes:
    """Scale the B/G/R channels of a BGRA pixel by `factor` (clamped 0..255); alpha untouched."""
    return bytes(
        (
            min(255, max(0, round(color[0] * factor))),
            min(255, max(0, round(color[1] * factor))),
            min(255, max(0, round(color[2] * factor))),
            color[3],
        )
    )


@dataclass(slots=True)
class Star:
    """One twinkling attract-screen star: sine-faded in and out over `lifetime_s`."""

    x: int
    y: int
    size_px: int
    age_s: float
    lifetime_s: float
    peak: float
    born: float = 0.0


class StarField:
    """Deterministic star population for the splash attract screen.

    Stars spawn at random positions and fade with `peak * sin(pi * age / lifetime)`,
    so brightness is exactly zero at birth and death — they appear and vanish
    smoothly with no pop-in or pop-out.
    """

    def __init__(
        self,
        width: int,
        height: int,
        rng: random.Random,
        *,
        max_stars: int | None = None,
        spawn_per_tick: int = 3,
        bpp: int = 32,
    ) -> None:
        self.width = width
        self.height = height
        self.rng = rng
        self.max_stars = (
            max_stars if max_stars is not None else max(16, (width * height) // 30_000)
        )
        self.spawn_per_tick = spawn_per_tick
        self.bpp = bpp
        self.now_s = 0.0
        self.stars: list[Star] = []

    def tick(self, now_s: float) -> None:
        """Drop expired stars and spawn up to `spawn_per_tick` replacements."""
        self.now_s = now_s
        alive: list[Star] = []
        for star in self.stars:
            star.age_s = now_s - star.born
            if star.age_s < star.lifetime_s:
                alive.append(star)
        self.stars = alive
        for _ in range(self.spawn_per_tick):
            if len(self.stars) >= self.max_stars:
                break
            self.stars.append(
                Star(
                    x=self.rng.randint(0, self.width - 1),
                    y=self.rng.randint(0, self.height - 1),
                    size_px=3 if self.rng.random() < 0.15 else 2,
                    age_s=0.0,
                    lifetime_s=self.rng.uniform(1.2, 2.6),
                    peak=self.rng.uniform(0.45, 0.95),
                    born=now_s,
                )
            )

    def draw(
        self, canvas: bytearray, width: int, height: int, now_s: float | None = None
    ) -> None:
        """Composite all stars under the text layer using the same bounds-checked
        inner-loop style as `build_fb_canvas`; `now_s` defaults to the last `tick`."""
        t = self.now_s if now_s is None else now_s
        bytes_per_px = self.bpp // 8
        stride = width * bytes_per_px
        for star in self.stars:
            age = max(0.0, min(t - star.born, star.lifetime_s))
            brightness = star.peak * math.sin(math.pi * age / star.lifetime_s)
            if brightness <= 0.0:
                continue
            px0 = star.x
            if px0 < 0 or px0 >= width:
                continue
            span = min(star.size_px, width - px0)
            if span <= 0:
                continue
            pixel = _fb_pattern(_scale_color(_FB_STAR_COLOR, brightness), self.bpp)
            row = pixel * span
            span_bytes = span * bytes_per_px
            for dy in range(star.size_px):
                py = star.y + dy
                if py < 0 or py >= height:
                    continue
                off = py * stride + px0 * bytes_per_px
                canvas[off : off + span_bytes] = row


_FB_BG_CACHE: dict[tuple[int, int, int], bytes] = {}


def _fb_background(width: int, height: int, bpp: int = 32) -> bytes:
    """Immutable full-screen background buffer, cached per resolution and format.

    Building the background on every redraw costs megabytes of allocation for an
    identical result; cache the template once per (w, h, bpp) and copy it into a
    fresh mutable `bytearray` per redraw.
    """
    key = (width, height, bpp)
    bg = _FB_BG_CACHE.get(key)
    if bg is None:
        bg = _fb_pattern(_FB_BG_PIXEL, bpp) * (width * height)
        _FB_BG_CACHE[key] = bg
    return bg


def build_fb_canvas(
    state: SplashState,
    width: int,
    height: int,
    *,
    phase: float = 0.0,
    starfield: StarField | None = None,
    bpp: int = 32,
) -> bytearray:
    """Pure, side-effect-free splash canvas builder for one framebuffer redraw.

    Renders the shared splash hierarchy directly into the framebuffer's native
    pixel format — 32-bpp BGRA, or 16-bpp little-endian RGB565 (the vc4-hdmi
    framebuffer on Pi Zero 2 W). Writing native pixels avoids a per-pixel
    conversion pass over ~2M pixels, which is unusably slow in pure Python.

    All positions are derived from `width`/`height` fractions so 640x480,
    1280x720, and 1920x1080 stay balanced with no hardcoded pixel offsets.

    `phase` in [0.0, 1.0) softly pulses the WIN + K shortcut only (attract-screen
    blink; phase 0.0 = full warm white, phase 0.5 = 30%). `starfield`, when given,
    is composited first so stars twinkle under the text. Defaults keep static
    renders identical to the pre-animation layout.
    """
    if bpp not in (16, 32):
        raise ValueError(f"unsupported framebuffer depth: {bpp}")
    bytes_per_px = bpp // 8
    stride = width * bytes_per_px
    canvas = bytearray(_fb_background(width, height, bpp))

    if starfield is not None:
        starfield.draw(canvas, width, height)

    def draw_block_text(text: str, y0: int, scale: int, color_bgra: bytes) -> None:
        cleaned = "".join(ch if ch in _FONT_5X7 else " " for ch in text.upper())
        total_cols = len(cleaned) * 6 - 1
        start_x = max(0, (width - total_cols * scale) // 2)
        pixel = _fb_pattern(color_bgra, bpp)
        for idx, ch in enumerate(cleaned):
            glyph = _FONT_5X7[ch]
            gx = start_x + idx * 6 * scale
            for r_i, row_bits in enumerate(glyph):
                for c_i, bit in enumerate(row_bits):
                    if bit != "1":
                        continue
                    px0 = gx + c_i * scale
                    if px0 >= width:
                        continue
                    span = min(scale, width - px0)
                    if span <= 0:
                        continue
                    py0 = y0 + r_i * scale
                    row = pixel * span
                    span_bytes = span * bytes_per_px
                    for dy in range(scale):
                        py = py0 + dy
                        if py < 0 or py >= height:
                            continue
                        off = py * stride + px0 * bytes_per_px
                        canvas[off : off + span_bytes] = row

    def draw_rule(y0: int, thickness: int, rule_width: int, color_bgra: bytes) -> None:
        x0 = max(0, (width - rule_width) // 2)
        span = min(rule_width, width - x0)
        if span <= 0:
            return
        row = _fb_pattern(color_bgra, bpp) * span
        for dy in range(thickness):
            py = y0 + dy
            if py < 0 or py >= height:
                continue
            off = py * stride + x0 * bytes_per_px
            canvas[off : off + span * bytes_per_px] = row

    # 1. Dim wordmark in the upper area, never glued to the top edge.
    wordmark_scale = max(2, width // 240)
    wordmark_y = height // 5
    draw_block_text("PICAST", wordmark_y, wordmark_scale, _FB_WORDMARK_COLOR)

    # 2. Thin amber accent rule between wordmark and PIN — exactly as wide as
    # the wordmark ("PICAST": 6 glyphs -> 6*6-1 columns) for optical alignment.
    rule_y = wordmark_y + 7 * wordmark_scale + height // 30
    rule_width = (len("PICAST") * 6 - 1) * wordmark_scale
    draw_rule(rule_y, max(2, height // 540), rule_width, _FB_ACCENT_COLOR)

    # 3. The PIN: dead-center hero, brightest and largest element.
    pin_scale = max(4, width // 60)
    pin_y = max(0, (height - 7 * pin_scale) // 2 - height // 16)
    draw_block_text(_format_pin_text(state.pin_display), pin_y, pin_scale, _FB_PIN_COLOR)

    # 4. The actionable shortcut directly under the hero. The device name is
    # redundant when it matches the wordmark, so it is only drawn when distinct.
    # Only the WIN + K line pulses with `phase`; nothing else blinks.
    device_scale = max(3, width // 160)
    winkey_y = pin_y + 7 * pin_scale + height // 18
    blink = 0.18 + 0.82 * (0.5 + 0.5 * math.cos(2 * math.pi * phase))
    draw_block_text("WIN + K", winkey_y, device_scale, _scale_color(_FB_DEVICE_COLOR, blink))
    device_name = state.device_name.strip().upper()[:12]
    if device_name and device_name != "PICAST":
        device_y = winkey_y + 7 * device_scale + height // 40
        draw_block_text(device_name, device_y, device_scale, _FB_DEVICE_COLOR)

    # 5. Bottom stack: status line plus tiny transport footer, lifted from the
    # bottom edge to close the hollow band under the hero cluster.
    status_scale = max(2, width // 240)
    status_y = height - height // 5
    draw_block_text(state.status_line[:32], status_y, status_scale, _FB_STATUS_COLOR)

    transport_scale = max(2, width // 300)
    transport_y = status_y + 7 * status_scale + height // 40
    draw_block_text(state.transport_label[:16], transport_y, transport_scale, _FB_TRANSPORT_COLOR)

    return canvas


def fb0_geometry(fb_path: Path = Path("/dev/fb0")) -> tuple[int, int, int] | None:
    """Probe for a usable `/dev/fb0` of at least 640x480; `(width, height, bpp)` or None.

    Single source of truth for the sysfs probe used by both the static splash
    write and the animation loop. Supports 32-bpp BGRA and 16-bpp RGB565 (the
    vc4-hdmi framebuffer on Pi Zero 2 W is 16-bpp).
    """
    sysfs_size = Path("/sys/class/graphics/fb0/virtual_size")
    sysfs_bpp = Path("/sys/class/graphics/fb0/bits_per_pixel")
    if not (fb_path.exists() and sysfs_size.exists() and sysfs_bpp.exists()):
        return None

    try:
        w_str, h_str = sysfs_size.read_text(encoding="utf-8").strip().split(",")
        width, height = int(w_str), int(h_str)
        bpp = int(sysfs_bpp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None

    if bpp not in (16, 32) or width < 640 or height < 480:
        return None
    return width, height, bpp


def _fb_pattern(color_bgra: bytes, bpp: int) -> bytes:
    """Return the per-pixel byte pattern for the framebuffer's native format.

    32-bpp: 4-byte BGRA as-is (driver ignores alpha). 16-bpp: little-endian
    RGB565 — the vc4-hdmi framebuffer on Pi Zero 2 W is 16-bpp.
    """
    if bpp == 32:
        return color_bgra
    b, g, r, _a = color_bgra
    value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return bytes((value & 0xFF, value >> 8))


def _write_fb0_splash(state: SplashState, fb_path: Path = Path("/dev/fb0")) -> bool:
    """Attempt pixel-exact rendering directly to `/dev/fb0` (32-bpp BGRA or 16-bpp RGB565).

    Only the geometry probe and the write happen here; all canvas-building lives
    in the pure `build_fb_canvas` so layouts are testable without a framebuffer.
    """
    geometry = fb0_geometry(fb_path)
    if geometry is None:
        return False
    width, height, bpp = geometry

    canvas = build_fb_canvas(state, width, height, bpp=bpp)

    try:
        with fb_path.open("r+b", buffering=0) as fb:
            fb.write(canvas)
        return True
    except OSError as exc:
        logger.debug("Framebuffer splash write skipped (%s): %s", fb_path, exc)
        return False


def run_splash_animation(
    state_factory: Callable[[], SplashState],
    stop_event: threading.Event,
    *,
    tick_s: float = 0.10,
    blink_period_s: float = 1.0,
    rng: random.Random | None = None,
) -> None:
    """Blocking game-attract loop for a daemon thread: pulsing WIN + K plus twinkling stars.

    Rebuilds and rewrites the full framebuffer every tick into its native pixel
    format (no conversion pass) — ~4.1 MB per frame at 1080p 16-bpp — so a 0.1 s
    tick keeps the twinkle smooth on a Zero 2 W. Without a usable fb0 this returns
    immediately; the TTY fallback never animates.
    """
    geometry = fb0_geometry()
    if geometry is None:
        return
    width, height, bpp = geometry
    field = StarField(
        width, height, rng if rng is not None else random.Random(), bpp=bpp
    )
    t0 = time.monotonic()

    while not stop_event.is_set():
        now = time.monotonic()
        phase = ((now - t0) / blink_period_s) % 1.0
        field.tick(now)
        canvas = build_fb_canvas(
            state_factory(), width, height, phase=phase, starfield=field, bpp=bpp
        )
        try:
            with Path("/dev/fb0").open("r+b", buffering=0) as fb:
                fb.write(canvas)
        except OSError as exc:
            logger.debug("Splash animation write failed; stopping (%s)", exc)
            return
        stop_event.wait(tick_s)


def render_splash(state: SplashState, tty_path: Path = Path("/dev/tty1")) -> None:
    """Render high-contrast color-block splash to `/dev/fb0` (16- or 32-bpp) or TTY1."""
    if _write_fb0_splash(state):
        return
    rendered = format_ansi_splash(state)
    try:
        tty_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        logger.debug("TTY splash write skipped (%s): %s", tty_path, exc)
