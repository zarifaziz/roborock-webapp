"""Draw the fake floor plan in the live map's style: room fills, walls, carpet hatching,
the vacuum trail, translucent mop coverage, the charger and the robot."""

from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageDraw

from roborock_auto.adapters.fake.floor_plan import (
    CARPETS,
    DOCK,
    DOORS,
    PLAN_SIZE,
    ROOMS,
    Pt,
)

PX_PER_UNIT = 10
MARGIN = 3
IMAGE_SIZE = (PLAN_SIZE + 2 * MARGIN) * PX_PER_UNIT
ROBOT_COLOR = (10, 132, 255)

# Drawn at twice the size and downsampled, so edges and trails are anti-aliased.
_SS = 2
_WALL = (111, 125, 137, 255)
_CARPET = (90, 120, 140, 110)
_TRAIL = (255, 255, 255, 235)
_MOP = (235, 255, 255, 95)
_CHARGER = (108, 194, 113, 255)
_ROBOT_RADIUS = 1.75  # 35 cm across
_MOP_WIDTH = 3.0      # 30 cm swath


def to_image(p: Pt) -> tuple[int, int]:
    """Plan units -> pixel of the delivered PNG."""
    return int((p[0] + MARGIN) * PX_PER_UNIT), int((p[1] + MARGIN) * PX_PER_UNIT)


def _s(p: Pt) -> tuple[float, float]:
    return (p[0] + MARGIN) * PX_PER_UNIT * _SS, (p[1] + MARGIN) * PX_PER_UNIT * _SS


def _units(n: float) -> int:
    return max(1, round(n * PX_PER_UNIT * _SS))


def _wall_segments():
    """Every room edge minus the door openings that lie on it."""
    for room in ROOMS:
        pts = room.outline
        for a, b in zip(pts, pts[1:] + pts[:1], strict=True):
            axis = 0 if a[1] == b[1] else 1  # 0: horizontal edge, varies in x
            fixed = a[1 - axis]
            lo, hi = sorted((a[axis], b[axis]))
            spans = [(lo, hi)]
            for d in DOORS:
                d_fixed, d_lo, d_hi = (d[1], d[0], d[2]) if axis == 0 else (d[0], d[1], d[3])
                if d[1 - axis] != d[3 - axis] or d_fixed != fixed:
                    continue
                spans = [
                    piece for s0, s1 in spans
                    for piece in ((s0, min(s1, d_lo)), (max(s0, d_hi), s1)) if piece[1] > piece[0]
                ]
            for s0, s1 in spans:
                yield ((s0, fixed), (s1, fixed)) if axis == 0 else ((fixed, s0), (fixed, s1))


def _hatch(size: int) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    step = _units(0.9)
    for c in range(-size, size, step):
        draw.line([(c, size), (c + size, 0)], fill=_CARPET, width=_units(0.18))
    return layer


def render(trail: list[Pt], robot: Pt, *, mop: bool, mop_lifted_on_carpet: bool) -> bytes:
    size = IMAGE_SIZE * _SS
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for room in ROOMS:
        draw.polygon([_s(p) for p in room.outline], fill=(*room.color, 255))

    carpet_mask = Image.new("L", (size, size), 0)
    carpet_draw = ImageDraw.Draw(carpet_mask)
    for x0, y0, x1, y1 in CARPETS:
        carpet_draw.rectangle([_s((x0, y0)), _s((x1, y1))], fill=255)
    image.paste(Image.alpha_composite(image, _hatch(size)), mask=carpet_mask)
    for x0, y0, x1, y1 in CARPETS:
        draw.rectangle([_s((x0, y0)), _s((x1, y1))], outline=_CARPET, width=_units(0.25))

    if len(trail) > 1:
        line = [_s(p) for p in trail]
        if mop:
            swath = Image.new("L", (size, size), 0)
            ImageDraw.Draw(swath).line(line, fill=_MOP[3], width=_units(_MOP_WIDTH), joint="curve")
            if mop_lifted_on_carpet:
                swath = ImageChops.subtract(swath, carpet_mask)
            tint = Image.new("RGBA", (size, size), _MOP)
            tint.putalpha(swath)
            image = Image.alpha_composite(image, tint)
            draw = ImageDraw.Draw(image)
        draw.line(line, fill=_TRAIL, width=_units(0.2), joint="curve")

    wall = _units(0.45)
    for a, b in _wall_segments():
        (ax, ay), (bx, by) = _s(a), _s(b)
        draw.rectangle(
            [min(ax, bx) - wall / 2, min(ay, by) - wall / 2,
             max(ax, bx) + wall / 2, max(ay, by) + wall / 2],
            fill=_WALL,
        )

    cx, cy = _s((DOCK[0], DOCK[1] + 2.0))
    half_w, half_h = _units(1.6), _units(0.7)
    draw.rounded_rectangle([cx - half_w, cy - half_h, cx + half_w, cy + half_h],
                           radius=_units(0.4), fill=_CHARGER)

    heading = _heading(trail)
    rx, ry = _s(robot)
    r = _units(_ROBOT_RADIUS)
    draw.ellipse([rx - r, ry - r, rx + r, ry + r], fill=(*ROBOT_COLOR, 255),
                 outline=(255, 255, 255, 255), width=_units(0.3))
    tx, ty = rx + math.cos(heading) * r * 0.5, ry + math.sin(heading) * r * 0.5
    t = _units(0.45)
    draw.ellipse([tx - t, ty - t, tx + t, ty + t], fill=(255, 255, 255, 255))

    out = io.BytesIO()
    image.resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.LANCZOS).save(out, format="PNG")
    return out.getvalue()


def _heading(trail: list[Pt]) -> float:
    for a, b in zip(reversed(trail[:-1]), reversed(trail[1:]), strict=True):
        if a != b:
            return math.atan2(b[1] - a[1], b[0] - a[0])
    return -math.pi / 2  # docked, facing into the room
