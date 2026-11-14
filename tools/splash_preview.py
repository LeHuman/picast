"""Render PNG previews of the picast framebuffer splash without touching /dev/fb0.

Imports the service's splash module, builds the real native-format canvas via
``build_fb_canvas`` for several ``SplashState`` variants, converts to RGB, and
writes PNGs (stdlib zlib/struct only — no Pillow).

Usage (WSL/Debian):
    cd /f/GitHub/lazycast/service && python3 ../tools/splash_preview.py OUTDIR

Preview mode (game attract screen flipbook):
    python3 ../tools/splash_preview.py --animate N OUTDIR
    Renders N frames as a PNG flipbook spanning one full blink cycle; the shared
    starfield's clock advances 1.4/N seconds per frame with a fixed seed.
"""

from __future__ import annotations

import argparse
import random
import struct
import sys
import zlib
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE_ROOT))

from src.splash import SplashState, StarField, build_fb_canvas  # noqa: E402

BLINK_PERIOD_S = 1.0  # keep in sync with run_splash_animation's default


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    """Write a minimal 8-bit RGB PNG from raw top-down RGB rows."""
    raw = b"".join(
        b"\x00" + rgba[(y * width) * 3 : ((y + 1) * width) * 3] for y in range(height)
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def native_to_rgb(canvas: bytearray, width: int, height: int, bpp: int) -> bytes:
    """Decode a native framebuffer canvas (BGRA or RGB565) into top-down RGB."""
    out = bytearray(width * height * 3)
    if bpp == 32:
        for i in range(width * height):
            out[i * 3] = canvas[i * 4 + 2]
            out[i * 3 + 1] = canvas[i * 4 + 1]
            out[i * 3 + 2] = canvas[i * 4]
        return bytes(out)
    if bpp != 16:
        raise ValueError(f"unsupported bpp: {bpp}")
    for i in range(width * height):
        value = canvas[i * 2] | (canvas[i * 2 + 1] << 8)
        r5 = (value >> 11) & 0x1F
        g6 = (value >> 5) & 0x3F
        b5 = value & 0x1F
        out[i * 3] = (r5 << 3) | (r5 >> 2)
        out[i * 3 + 1] = (g6 << 2) | (g6 >> 4)
        out[i * 3 + 2] = (b5 << 3) | (b5 >> 2)
    return bytes(out)


def main(outdir: Path, animate_n: int | None = None, bpp: int = 16) -> int:
    outdir.mkdir(parents=True, exist_ok=True)

    states = [
        ("idle", SplashState("PICAST", "P2P + MICE", "54747666", "READY")),
        ("starting", SplashState("PICAST", "P2P + MICE", "54747666", "STARTING")),
        (
            "casting",
            SplashState("PICAST", "P2P + MICE", "54747666", "CASTING FROM 192.168.173.80"),
        ),
        ("lowres", SplashState("PICAST", "P2P + MICE", "54747666", "READY")),
    ]
    width, height = 1920, 1080

    if animate_n is not None:
        # Game-attract flipbook spanning one blink cycle; one shared starfield so
        # stars age (a fresh field per frame would render every star at age 0).
        rng = random.Random(123)  # deterministic per-run seed
        field = StarField(width, height, rng, bpp=bpp)
        # Warm the field to steady state (stars mid-life) before frame 0; a cold
        # field renders every star at age 0, which is invisible by design.
        for warm_i in range(30):
            field.tick(-3.0 + warm_i * 0.1)
        step_s = BLINK_PERIOD_S / max(1, animate_n)
        for frame_idx in range(animate_n):
            elapsed = frame_idx * step_s
            phase = (elapsed / BLINK_PERIOD_S) % 1.0
            _state_name, state = states[0]
            field.tick(elapsed)
            canvas = build_fb_canvas(
                state, width, height, phase=phase, starfield=field, bpp=bpp
            )
            png_name = f"splash_anim_{frame_idx}.png"
            write_png(
                outdir / png_name, width, height, native_to_rgb(canvas, width, height, bpp)
            )
            print(f"wrote {outdir / png_name}  stars={len(field.stars)}")
        return 0

    # Static rendering path (no animation).
    for name, state in states:
        w, h = (640, 480) if name == "lowres" else (width, height)
        canvas = build_fb_canvas(state, w, h, bpp=bpp)
        assert len(canvas) == w * h * (bpp // 8), f"canvas size mismatch for {name}"
        write_png(
            outdir / f"splash_{name}_{w}x{h}.png",
            w,
            h,
            native_to_rgb(canvas, w, h, bpp),
        )
        print(f"wrote {outdir / f'splash_{name}_{w}x{h}.png'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Render PNG previews of the picast framebuffer splash."
    )
    parser.add_argument(
        "outdir",
        type=Path,
        nargs="?",
        default=Path("previews"),
        help="Output directory.",
    )
    parser.add_argument(
        "--animate",
        type=int,
        metavar="N",
        const=6,
        nargs="?",
        default=None,
        help="Render N frames as a game-attract flipbook (default: static previews).",
    )
    parser.add_argument(
        "--bpp",
        type=int,
        choices=(16, 32),
        default=16,
        help="Framebuffer depth to render (default: 16, matching picast's fb0).",
    )
    args = parser.parse_args()
    raise SystemExit(main(args.outdir, animate_n=args.animate, bpp=args.bpp))