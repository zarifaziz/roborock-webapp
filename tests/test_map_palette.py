"""Pins the calm map palette the live adapter installs into the vendor renderer.

The palette is the design contract for the served map PNG: one snapshot of the
complete room-colour mapping and one of the element-colour overrides. If the pool
or a colour changes, these dicts change with it.
"""

from __future__ import annotations

from vacuum_map_parser_base.config.color import ColorsPalette, SupportedColor

from roborock_auto.adapters.roborock_cloud.palette import (
    ROOM_COLOR_POOL,
    build_palette,
    install_calm_palette,
)

WALL = (110, 114, 120, 255)
OBSTACLE = (90, 95, 102, 140)


def expected_room_colors() -> dict[str | int, tuple]:
    """Every vendor room slot, cycled over the 8-colour pool, cached as str and int."""
    expected: dict[str | int, tuple] = {}
    for slot in range(1, len(ColorsPalette.ROOM_COLORS) + 1):
        color = ROOM_COLOR_POOL[(slot - 1) % len(ROOM_COLOR_POOL)]
        expected[str(slot)] = color
        expected[slot] = color
    return expected


def test_room_colors_cycle_the_calm_pool_over_all_slots():
    palette = build_palette(show_background=False)
    assert palette.cached_room_colors == expected_room_colors()


def test_element_colors_snapshot():
    palette = build_palette(show_background=False)
    overridden = {
        SupportedColor.MAP_OUTSIDE: (0, 0, 0, 0),
        SupportedColor.MAP_WALL: WALL,
        SupportedColor.MAP_WALL_V2: WALL,
        SupportedColor.GREY_WALL: WALL,
        SupportedColor.OBSTACLE: OBSTACLE,
        SupportedColor.OBSTACLE_WITH_PHOTO: OBSTACLE,
        SupportedColor.IGNORED_OBSTACLE: OBSTACLE,
        SupportedColor.IGNORED_OBSTACLE_WITH_PHOTO: OBSTACLE,
        SupportedColor.PATH: (61, 74, 92, 165),
        SupportedColor.MOP_PATH: (255, 255, 255, 56),
        SupportedColor.NO_GO_ZONES: (224, 108, 108, 38),
        SupportedColor.NO_GO_ZONES_OUTLINE: (224, 108, 108, 216),
        SupportedColor.NO_MOPPING_ZONES: (154, 134, 232, 36),
        SupportedColor.NO_MOPPING_ZONES_OUTLINE: (154, 134, 232, 216),
        SupportedColor.NO_CARPET_ZONES: (134, 160, 232, 36),
        SupportedColor.NO_CARPET_ZONES_OUTLINE: (134, 160, 232, 216),
        SupportedColor.VIRTUAL_WALLS: (224, 108, 108, 255),
        SupportedColor.ZONES: (10, 132, 255, 38),
        SupportedColor.ZONES_OUTLINE: (10, 132, 255, 220),
        SupportedColor.CHARGER: (108, 194, 113, 255),
        SupportedColor.CHARGER_OUTLINE: (108, 194, 113, 255),
        SupportedColor.ROBO: (10, 132, 255, 255),
    }
    expected = {**ColorsPalette.COLORS, **overridden}
    assert {color: palette.get_color(color) for color in expected} == expected


def test_install_patches_vendor_factory_with_calm_palette():
    import roborock.map.map_parser as vendor_map_parser
    from roborock.map.map_parser import MapParserConfig

    install_calm_palette()
    install_calm_palette()  # idempotent: never double-wraps

    config = MapParserConfig(map_scale=8, show_background=False)
    palette, _sizes, image_config = vendor_map_parser._create_rendering_components(config)
    assert palette.cached_room_colors == expected_room_colors()
    assert palette.get_color(SupportedColor.MAP_OUTSIDE) == (0, 0, 0, 0)
    # Vendor still owns geometry: the scale reached the image config untouched.
    assert image_config.scale == 8


def test_hidden_rooms_and_walls_stay_transparent():
    palette = build_palette(show_walls=False, show_rooms=False)
    transparent = (0, 0, 0, 0)
    assert set(palette.cached_room_colors.values()) == {transparent}
    assert palette.get_color(SupportedColor.MAP_WALL) == transparent
    assert palette.get_color(SupportedColor.MAP_WALL_V2) == transparent
    assert palette.get_color(SupportedColor.GREY_WALL) == transparent
