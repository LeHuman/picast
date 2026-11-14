"""Render PNG previews of the picast framebuffer splash without touching /dev/fb0.

Imports the service's splash module, builds the real BGRA canvas via
``build_fb_canvas`` for several ``SplashState`` variants, converts to RGB, and
writes PNGs (stdlib zlib/struct only — no Pillow).

Usage (WSL/Debian):
    cd /f/GitHub/lazycast/service && python3 ../tools/splash_preview.py OUTDIR

Preview mode (game attract screen flipbook):
    python3 ../tools/splash_preview.py --animate N OUTDIR
    Renders N frames as PNG flipbook: frame i uses phase=i/6 and advances the
    starfield's internal clock by 0.4 s each frame with a fixed seed.
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


def bgra_to_rgb(canvas: bytearray, width: int, height: int) -> bytes:
    out = bytearray(width * height * 3)
    for i in range(width * height):
        b = canvas[i * 4]
        g = canvas[i * 4 + 1]
        r = canvas[i * 4 + 2]
        out[i * 3] = r
        out[i * 3 + 1] = g
        out[i * 3 + 2] = b
    return bytes(out)


def main(outdir: Path, animate_n: int | None = None) -> int:
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
        # Game-attract animation flipbook: N frames, phase=i/N, starfield +0.4s each.
        rng = random.Random(123)  # deterministic per-run seed
        for frame_idx in range(animate_n):
            _state_name, state = states[frame_idx % len(states)]
            phase = frame_idx / float(animate_n)
            # One field, clock advanced 0.4 s per frame so stars twinkle forward.
            field = StarField(width, height, rng)
            field.tick(frame_idx * 0.4)
            canvas = build_fb_canvas(state, width, height, phase=phase, starfield=field)
            png_name = f"splash_anim_{frame_idx}.png"
            write_png(outdir / png_name, width, height, bgra_to_rgb(canvas, width, height))
            print(f"wrote {outdir / png_name}")
        return 0

    # Static rendering path (no animation).
    for name, state in states:
        w, h = (640, 480) if name == "lowres" else (width, height)
        canvas = build_fb_canvas(state, w, h)
        assert len(canvas) == w * h * 4, f"canvas size mismatch for {name}"
        write_png(outdir / f"splash_{name}_{w}x{h}.png", w, h, bgra_to_rgb(canvas, w, h))
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
    args = parser.parse_args()
    raise SystemExit(main(args.outdir, animate_n=args.animate))