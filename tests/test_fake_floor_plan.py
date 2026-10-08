"""The fake robot's routes: every leg stays indoors, and Avoid keeps it off the carpets."""

from roborock_auto.adapters.fake.floor_plan import (
    CARPETS,
    DOCK,
    ROOMS,
    cleaning_route,
    densify,
    walk,
)


def _on_carpet(p) -> bool:
    return any(x0 < p[0] < x1 and y0 < p[1] < y1 for x0, y0, x1, y1 in CARPETS)


def _on_floor(p) -> bool:
    x0, y0, x1, y1 = 0, 0, 92, 92
    return x0 <= p[0] <= x1 and y0 <= p[1] <= y1 and any(
        rx0 <= p[0] <= rx1 and ry0 <= p[1] <= ry1
        for rx0, ry0, rx1, ry1 in (room.bounds for room in ROOMS)
    )


def test_avoid_never_drives_onto_a_carpet():
    route = densify(cleaning_route(None, avoid_carpet=True), step=0.25)
    assert [p for p in route if _on_carpet(p)] == []


def test_rise_vacuums_the_carpets():
    route = densify(cleaning_route([16, 18], avoid_carpet=False), step=0.25)
    assert all(any(x0 < x < x1 and y0 < y < y1 for x, y in route) for x0, y0, x1, y1 in CARPETS)


def test_route_starts_and_ends_on_the_dock_and_stays_on_the_floor():
    route = cleaning_route(None, avoid_carpet=True)
    assert (route[0], route[-1]) == (DOCK, DOCK)
    assert all(_on_floor(p) for p in densify(route))


def test_unknown_rooms_are_ignored():
    assert cleaning_route([99], avoid_carpet=True) == cleaning_route([], avoid_carpet=True)


def test_walk_returns_the_driven_prefix_and_position():
    route = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    assert walk(route, 0.0) == ([(0.0, 0.0), (0.0, 0.0)], (0.0, 0.0))
    assert walk(route, 0.75) == ([(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)], (10.0, 5.0))
    assert walk(route, 1.0) == (route, (10.0, 10.0))
