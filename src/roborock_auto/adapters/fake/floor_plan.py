"""The fake robot's invented apartment and the routes it drives through it.

Pure geometry in plan units (1 unit = 10 cm, top-left origin, y down). Rooms are
rectilinear polygons; every room door opens onto the Hallway (the Bathroom through the
Laundry), so any route between rooms runs door -> Hallway -> door and stays indoors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

Pt = tuple[float, float]
Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

PLAN_SIZE = 92
LANE_SPACING = 2.5  # ~25 cm between vacuum lanes, about what the real robot leaves
WALL_CLEARANCE = 2.0
CARPET_CLEARANCE = 2.0
HALL_Y = 42.0  # the Hallway's centre line, the corridor every inter-room trip uses
DOCK: Pt = (44.0, 89.5)  # robot centre when docked; the charger sits on the wall below
DOCK_EXIT: Pt = (44.0, 53.0)  # straight up from the dock, clear of the living-room rug
LIVING_ROOM = 16


@dataclass(frozen=True)
class PlanRoom:
    segment_id: int
    name: str
    outline: tuple[Pt, ...]
    color: tuple[int, int, int]
    # Waypoints from the Hallway centre line into the room, ending inside it.
    approach: tuple[Pt, ...]

    @property
    def bounds(self) -> Rect:
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        return min(xs), min(ys), max(xs), max(ys)


# The same four fills the live map palette uses, assigned so neighbours never match.
_BLUE = (156, 202, 255)
_TEAL = (22, 213, 203)
_SALMON = (255, 175, 151)
_YELLOW = (255, 221, 94)

ROOMS: tuple[PlanRoom, ...] = (
    PlanRoom(16, "Living Room", ((0, 48), (58, 48), (58, 92), (10, 92), (10, 84), (0, 84)),
             _TEAL, ((27, 48), (27, 50))),
    PlanRoom(17, "Kitchen", ((58, 48), (92, 48), (92, 76), (74, 76), (74, 92), (58, 92)),
             _SALMON, ((69, 48), (69, 50))),
    PlanRoom(18, "Bedroom", ((0, 0), (40, 0), (40, 36), (14, 36), (14, 32), (0, 32)),
             _BLUE, ((34, 36), (34, 34))),
    PlanRoom(19, "Hallway",
             ((14, 36), (92, 36), (92, 48), (14, 48)),
             _YELLOW, ()),
    PlanRoom(20, "Study", ((60, 0), (92, 0), (92, 24), (80, 24), (80, 36), (60, 36)),
             _BLUE, ((70, 36), (70, 34))),
    PlanRoom(21, "Bathroom", ((40, 0), (60, 0), (60, 22), (40, 22)),
             _TEAL, ((53, 36), (53, 30), (50, 24), (50, 20))),
    PlanRoom(22, "Laundry", ((40, 22), (60, 22), (60, 36), (40, 36)),
             _SALMON, ((53, 36), (53, 34))),
)
ROOMS_BY_ID = {room.segment_id: room for room in ROOMS}
# Visiting order, like the real robot's own room ordering: a loop from the dock that
# leaves the Hallway for last, since every other trip crosses it anyway.
TOUR = (16, 17, 20, 22, 21, 18, 19)

CARPETS: tuple[Rect, ...] = (
    (14, 57, 38, 78),  # living-room rug
    (6, 6, 26, 22),    # bedside rug
)

# Openings in walls, as axis-aligned segments; every other room edge is drawn as wall.
DOORS: tuple[Rect, ...] = (
    (18, 48, 38, 48),  # Living Room archway
    (64, 48, 74, 48),  # Kitchen
    (58, 56, 58, 86),  # open-plan Living Room / Kitchen
    (30, 36, 38, 36),  # Bedroom
    (50, 36, 57, 36),  # Laundry
    (46, 22, 54, 22),  # Bathroom, through the Laundry
    (66, 36, 74, 36),  # Study
)


def _intervals_at(outline: tuple[Pt, ...], y: float) -> list[tuple[float, float]]:
    """Inside spans of a rectilinear polygon along the horizontal line at y."""
    xs = sorted(
        a[0]
        for a, b in zip(outline, outline[1:] + outline[:1], strict=True)
        if a[0] == b[0] and min(a[1], b[1]) <= y < max(a[1], b[1])
    )
    return list(zip(xs[::2], xs[1::2], strict=True))


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]):
    return [
        (max(a0, b0), min(a1, b1))
        for a0, a1 in a for b0, b1 in b if max(a0, b0) < min(a1, b1)
    ]


def _subtract(spans: list[tuple[float, float]], cut: tuple[float, float]):
    out = []
    for x0, x1 in spans:
        if cut[1] <= x0 or cut[0] >= x1:
            out.append((x0, x1))
            continue
        if cut[0] > x0:
            out.append((x0, cut[0]))
        if cut[1] < x1:
            out.append((cut[1], x1))
    return out


def _lanes(room: PlanRoom, avoid_carpet: bool) -> list[list[tuple[float, float]]]:
    """Per lane, the x spans to sweep, top lane first."""
    _, y0, _, y1 = room.bounds
    lanes = []
    y = y0 + WALL_CLEARANCE
    while y <= y1 - WALL_CLEARANCE:
        # Spans clear of walls above and below too, so a lane never grazes an inner corner.
        spans = _intersect(
            _intervals_at(room.outline, y - WALL_CLEARANCE + 0.01),
            _intervals_at(room.outline, y + WALL_CLEARANCE - 0.01),
        )
        spans = [(a + WALL_CLEARANCE, b - WALL_CLEARANCE) for a, b in spans]
        if avoid_carpet:
            for cx0, cy0, cx1, cy1 in CARPETS:
                if cy0 - CARPET_CLEARANCE < y < cy1 + CARPET_CLEARANCE:
                    spans = _subtract(spans, (cx0 - CARPET_CLEARANCE, cx1 + CARPET_CLEARANCE))
        spans = [s for s in spans if s[1] - s[0] > 0.5]
        if spans:
            lanes.append((y, spans))
        y += LANE_SPACING
    return [[(a, b, lane_y) for a, b in spans] for lane_y, spans in lanes]


def _carpet_detour(x_from: float, x_to: float, y: float) -> list[Pt]:
    """Go around the carpet a split lane straddles, along whichever long side is nearer."""
    for cx0, cy0, cx1, cy1 in CARPETS:
        if min(x_from, x_to) < cx0 and max(x_from, x_to) > cx1 and cy0 - 3 < y < cy1 + 3:
            side = cy0 - CARPET_CLEARANCE if y - cy0 < cy1 - y else cy1 + CARPET_CLEARANCE
            return [(x_from, side), (x_to, side)]
    return []


def _sweep(room: PlanRoom, avoid_carpet: bool, entry: Pt) -> list[Pt]:
    """Boustrophedon from the lane farthest from the entry back towards it, so the robot
    finishes beside the door. Moves between lanes and to/from the door run along a lane or
    straight across lanes, never diagonally through the room."""
    lanes = _lanes(room, avoid_carpet)
    if not lanes:
        return []
    top, bottom = lanes[0][0][2], lanes[-1][0][2]
    if abs(entry[1] - top) < abs(entry[1] - bottom):
        lanes.reverse()
    first = lanes[0]
    left_to_right = abs(entry[0] - first[0][0]) <= abs(entry[0] - first[-1][1])
    points: list[Pt] = []
    if _spans_contain(first, entry[0]):
        points.append((entry[0], first[0][2]))
    for spans in lanes:
        ordered = spans if left_to_right else [(b, a, y) for a, b, y in reversed(spans)]
        start_x, y = ordered[0][0], ordered[0][2]
        if points and points[-1][1] != y:
            end_x = points[-1][0]
            corner = (end_x, y) if _spans_contain(spans, end_x) else (start_x, points[-1][1])
            points.append(corner)
        for i, (a, b, lane_y) in enumerate(ordered):
            if i:
                points.extend(_carpet_detour(points[-1][0], a, lane_y))
            points.extend([(a, lane_y), (b, lane_y)])
        left_to_right = not left_to_right
    if _spans_contain(lanes[-1], entry[0]):
        points.append((entry[0], lanes[-1][0][2]))
    return points


def _spans_contain(spans: list[tuple[float, float, float]], x: float) -> bool:
    return any(a <= x <= b for a, b, _ in spans)


def _leave(room: PlanRoom) -> list[Pt]:
    return [*reversed(room.approach), (room.approach[0][0], HALL_Y)] if room.approach else []


def _enter(room: PlanRoom) -> list[Pt]:
    return [(room.approach[0][0], HALL_Y), *room.approach] if room.approach else []


def _crosses(a: Pt, b: Pt, rect: Rect) -> bool:
    x0, y0, x1, y1 = rect
    n = max(1, math.ceil(math.dist(a, b) / 0.25))
    return any(
        x0 < a[0] + (b[0] - a[0]) * k / n < x1 and y0 < a[1] + (b[1] - a[1]) * k / n < y1
        for k in range(n + 1)
    )


def _around_carpets(route: list[Pt]) -> list[Pt]:
    """Re-route any leg that would cut across a carpet via the carpet's corners."""
    out = route[:1]
    for b in route[1:]:
        a = out[-1]
        for cx0, cy0, cx1, cy1 in CARPETS:
            if not _crosses(a, b, (cx0, cy0, cx1, cy1)):
                continue
            c = CARPET_CLEARANCE
            corners = [(cx0 - c, cy0 - c), (cx1 + c, cy0 - c), (cx1 + c, cy1 + c), (cx0 - c, cy1 + c)]
            rect = (cx0, cy0, cx1, cy1)
            detours = [[k] for k in corners] + [[k, m] for k in corners for m in corners if k != m]
            via = min(
                (w for w in detours
                 if not any(_crosses(p, q, rect) for p, q in zip([a, *w], [*w, b], strict=True))),
                key=lambda w: sum(math.dist(p, q) for p, q in zip([a, *w], [*w, b], strict=True)),
            )
            out.extend(via)
            break
        out.append(b)
    return out


def cleaning_route(segment_ids: list[int] | None, avoid_carpet: bool) -> list[Pt]:
    """Dock -> each room swept in lanes -> dock. None means the whole home."""
    ids = [i for i in TOUR if segment_ids is None or i in segment_ids]
    route: list[Pt] = [DOCK, DOCK_EXIT]
    here = ROOMS_BY_ID[LIVING_ROOM]
    for room in (ROOMS_BY_ID[i] for i in ids):
        if room is not here:
            route += _leave(here) + _enter(room)
            here = room
        route += _sweep(room, avoid_carpet, route[-1])
    home = ROOMS_BY_ID[LIVING_ROOM]
    if here is not home:
        route += _leave(here) + _enter(home)
    route += [DOCK_EXIT, DOCK]
    return _around_carpets(route) if avoid_carpet else route


def walk(route: list[Pt], fraction: float) -> tuple[list[Pt], Pt]:
    """The driven prefix of a route at a fraction of its length, and where the robot is."""
    if fraction >= 1.0:
        return list(route), route[-1]
    lengths = [math.dist(a, b) for a, b in pairwise(route)]
    remaining = max(0.0, min(1.0, fraction)) * sum(lengths)
    trail = [route[0]]
    for (a, b), length in zip(pairwise(route), lengths, strict=True):
        if remaining >= length:
            trail.append(b)
            remaining -= length
            continue
        t = remaining / length
        trail.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        break
    return trail, trail[-1]


def densify(points: list[Pt], step: float = 1.0) -> list[Pt]:
    """Points every `step` units along a polyline, for coverage counting."""
    out = points[:1]
    for a, b in pairwise(points):
        n = max(1, math.ceil(math.dist(a, b) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
    return out
