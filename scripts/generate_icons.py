"""Generate the PWA icons with the standard library only (Pillow is not installed).

Renders a robot-vacuum-top-view glyph — a white disc with a lidar turret and a docked
charge bar — on the app's accent blue, then writes real PNG files (zlib + struct) into
the static icons directory. Run from the repo root:

    uv run python scripts/generate_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "src" / "roborock_auto" / "adapters" / "web" / "static" / "icons"

ACCENT = (0, 113, 227)  # --accent from index.html's light theme
WHITE = (255, 255, 255)


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 per scanline
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))  # RGBA8
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _smoothstep(edge: float, x: float) -> float:
    """0 -> 1 over one pixel around `edge`, for cheap anti-aliasing."""
    t = min(1.0, max(0.0, x - edge + 0.5))
    return t * t * (3 - 2 * t)


def _render(size: int, *, full_bleed: bool, glyph_scale: float) -> bytes:
    """One frame: accent background (rounded unless full_bleed), robot glyph centred."""
    c = size / 2
    glyph_r = size * glyph_scale  # robot disc radius
    corner = 0.0 if full_bleed else size * 0.22
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            # Background coverage: rounded-rect via corner-circle distance.
            if full_bleed:
                bg_a = 1.0
            else:
                qx = max(abs(px - c) - (c - corner), 0.0)
                qy = max(abs(py - c) - (c - corner), 0.0)
                bg_a = 1.0 - _smoothstep(corner, (qx * qx + qy * qy) ** 0.5)
            # Glyph, drawn over the background: white disc, accent lidar turret,
            # a white charge bar under the disc.
            d = ((px - c) ** 2 + (py - c) ** 2) ** 0.5
            disc = 1.0 - _smoothstep(glyph_r, d)
            turret = 1.0 - _smoothstep(glyph_r * 0.34, d)
            dot = 1.0 - _smoothstep(glyph_r * 0.13, d)
            bar = 0.0
            if abs(py - (c + glyph_r * 1.32)) < glyph_r * 0.10 and abs(px - c) < glyph_r * 0.62:
                bar = 1.0
            r, g, b = ACCENT
            for glyph_a, colour in ((disc, WHITE), (turret, ACCENT), (dot, WHITE), (bar, WHITE)):
                r = r + (colour[0] - r) * glyph_a
                g = g + (colour[1] - g) * glyph_a
                b = b + (colour[2] - b) * glyph_a
            row += bytes((round(r), round(g), round(b), round(bg_a * 255)))
        rows.append(bytes(row))
    return _png(size, size, rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        # Regular icons: rounded-rect tile with transparency around it.
        ("icon-192.png", 192, False, 0.30),
        ("icon-512.png", 512, False, 0.30),
        # Maskable: full-bleed background, glyph inside the 80% safe zone.
        ("icon-maskable-512.png", 512, True, 0.26),
        # iOS Add to Home Screen: full-bleed square (iOS applies its own corner mask).
        ("apple-touch-icon.png", 180, True, 0.30),
    ]
    for name, size, full_bleed, scale in jobs:
        path = OUT_DIR / name
        path.write_bytes(_render(size, full_bleed=full_bleed, glyph_scale=scale))
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
