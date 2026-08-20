"""Render the map as compact text an agent (or a terminal) can read cheaply.

A PNG costs thousands of tokens to "look at"; this draws the same information — room
layout, where the robot is, and where it has driven (coverage) — as a small ASCII grid
plus a legend. Pure and normalized (0..1 coords), so the live adapter and the fake feed it
the same way and it is unit-testable without a device.
"""

from __future__ import annotations

from dataclasses import dataclass

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class TextRoom:
    name: str
    bbox: tuple[float, float, float, float]  # normalized x0,y0,x1,y1


def _bounds(rooms: list[TextRoom], path: list[tuple[float, float]]):
    xs = [b for r in rooms for b in (r.bbox[0], r.bbox[2])] + [p[0] for p in path]
    ys = [b for r in rooms for b in (r.bbox[1], r.bbox[3])] + [p[1] for p in path]
    if not xs or not ys:
        return 0.0, 0.0, 1.0, 1.0
    # A hair of padding so content on the edge is not clipped.
    return max(0.0, min(xs) - 0.01), max(0.0, min(ys) - 0.01), min(1.0, max(xs) + 0.01), min(1.0, max(ys) + 0.01)


def render_map_text(
    rooms: list[TextRoom],
    robot: tuple[float, float] | None,
    path: list[tuple[float, float]],
    *,
    cols: int = 56,
) -> str:
    """Grid legend: '@' robot, ':' driven path (coverage), a letter marks each room's centre
    (see legend), ' ' un-driven floor. The view is cropped to the content so empty map margin
    is dropped. Coverage per room = fraction of that room's grid cells the path has touched."""
    if not rooms:
        return "(no map yet)"

    x0, y0, x1, y1 = _bounds(rooms, path)
    span_x = max(x1 - x0, 1e-6)
    span_y = max(y1 - y0, 1e-6)
    # Characters are about twice as tall as wide, so halve the row count to keep the aspect.
    rows = max(3, round(cols * (span_y / span_x) * 0.5))

    def cell(px: float, py: float) -> tuple[int, int] | None:
        c = int((px - x0) / span_x * (cols - 1))
        r = int((py - y0) / span_y * (rows - 1))
        if 0 <= c < cols and 0 <= r < rows:
            return c, r
        return None

    grid = [[" "] * cols for _ in range(rows)]

    # Coverage: every driven point marks its cell. Also count hits per room for the legend.
    covered: set[tuple[int, int]] = set()
    for p in path:
        rc = cell(p[0], p[1])
        if rc:
            covered.add(rc)
            grid[rc[1]][rc[0]] = ":"

    # Room centre markers + coverage percentage.
    legend = []
    for i, room in enumerate(rooms):
        letter = _LETTERS[i % len(_LETTERS)]
        bx0, by0, bx1, by1 = room.bbox
        cells = [
            (c, r)
            for c in range(cols)
            for r in range(rows)
            if bx0 <= x0 + (c / (cols - 1)) * span_x <= bx1
            and by0 <= y0 + (r / (rows - 1)) * span_y <= by1
        ]
        pct = round(100 * sum(1 for cc in cells if cc in covered) / len(cells)) if cells else 0
        centre = cell((bx0 + bx1) / 2, (by0 + by1) / 2)
        if centre:
            grid[centre[1]][centre[0]] = letter
        legend.append(f"  {letter}  {room.name} — {pct}% covered")

    robot_room = None
    if robot is not None:
        rc = cell(robot[0], robot[1])
        if rc:
            grid[rc[1]][rc[0]] = "@"
        robot_room = _room_at(rooms, robot)

    body = "\n".join("".join(row).rstrip() for row in grid)
    head = f"@ = robot{f' (in {robot_room})' if robot_room else ''} · : = cleaned path"
    return f"{head}\n{body}\n" + "\n".join(legend)


def _room_at(rooms: list[TextRoom], pt: tuple[float, float]) -> str | None:
    # Room bboxes overlap, so pick the SMALLEST box that contains the point — the most
    # specific room, not just the first (which is often the big central room).
    hits = [
        r for r in rooms
        if r.bbox[0] <= pt[0] <= r.bbox[2] and r.bbox[1] <= pt[1] <= r.bbox[3]
    ]
    if not hits:
        return None
    return min(hits, key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1])).name
