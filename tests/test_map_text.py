"""The agent-readable text map renderer."""

from __future__ import annotations

from roborock_auto.adapters.map_text import TextRoom, render_map_text

_QUADRANTS = [
    TextRoom("Living Room", (0.0, 0.0, 0.5, 0.5)),
    TextRoom("Kitchen", (0.5, 0.0, 1.0, 0.5)),
    TextRoom("Bedroom", (0.0, 0.5, 0.5, 1.0)),
    TextRoom("Hallway", (0.5, 0.5, 1.0, 1.0)),
]


def _legend_pct(out: str, name: str) -> int:
    line = next(ln for ln in out.splitlines() if f" {name} — " in ln)
    return int(line.split("—")[1].strip().removesuffix("% covered"))


def test_render_reports_robot_room_and_per_room_coverage():
    robot = (0.25, 0.25)  # top-left quadrant = Living Room
    # A block of driven path covering (most of) the Living Room quadrant only.
    path = [(x / 20, y / 20) for x in range(10) for y in range(10)]
    out = render_map_text(_QUADRANTS, robot, path, cols=40)

    assert "@ = robot (in Living Room)" in out
    assert "@" in out and ":" in out
    # Every room appears in the legend with a letter marker.
    for i, room in enumerate(_QUADRANTS):
        assert f"  {'ABCD'[i]}  {room.name} — " in out
    # The driven quadrant is covered; the others are not.
    assert _legend_pct(out, "Living Room") > 40
    assert _legend_pct(out, "Kitchen") == 0
    assert _legend_pct(out, "Hallway") == 0


def test_render_handles_no_rooms():
    assert render_map_text([], None, [], cols=40) == "(no map yet)"


def test_render_without_robot_or_path_still_shows_layout():
    out = render_map_text(_QUADRANTS, None, [], cols=40)
    assert "(in " not in out  # no robot location reported
    assert "Living Room — 0% covered" in out
