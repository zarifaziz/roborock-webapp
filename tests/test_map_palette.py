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

WALL = (111, 125, 137, 255)
OBSTACLE = (90, 95, 102, 140)


def expected_room_colors() -> dict[str | int, tuple]:
    """Every vendor room slot, cycled over the reference colour pool, cached as str and int."""
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
        SupportedColor.CARPETS: (142, 175, 191),
        SupportedColor.MAP_INSIDE: (156, 202, 255),
        SupportedColor.MAP_WALL: WALL,
        SupportedColor.MAP_WALL_V2: WALL,
        SupportedColor.GREY_WALL: WALL,
        SupportedColor.OBSTACLE: OBSTACLE,
        SupportedColor.OBSTACLE_WITH_PHOTO: OBSTACLE,
        SupportedColor.IGNORED_OBSTACLE: OBSTACLE,
        SupportedColor.IGNORED_OBSTACLE_WITH_PHOTO: OBSTACLE,
        SupportedColor.PATH: (255, 255, 255, 235),
        SupportedColor.MOP_PATH: (235, 255, 255, 80),
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


def test_room_palette_matches_mobile_reference():
    assert ROOM_COLOR_POOL == (
        (156, 202, 255),
        (22, 213, 203),
        (255, 175, 151),
        (255, 221, 94),
    )


def test_path_widths_scale_with_map_geometry():
    import roborock.map.map_parser as vendor_map_parser
    from roborock.map.map_parser import MapParserConfig
    from vacuum_map_parser_base.config.size import Size, Sizes

    install_calm_palette()
    for scale in (4, 8):
        _, sizes, _ = vendor_map_parser._create_rendering_components(
            MapParserConfig(map_scale=scale)
        )
        expected = {key: value * scale for key, value in Sizes.SIZES.items()}
        expected.update({Size.PATH_WIDTH: 0.45 * scale, Size.MOP_PATH_WIDTH: 6 * scale})
        assert {key: sizes.get_size(key) for key in Size} == expected


def render_trace(*, mop_segments):
    from PIL import Image
    from roborock.map.map_parser import MapParserConfig, _create_image_generator
    from vacuum_map_parser_base.config.drawable import Drawable
    from vacuum_map_parser_base.config.image_config import ImageConfig
    from vacuum_map_parser_base.map_data import ImageData, MapData, Path, Point

    install_calm_palette()
    map_data = MapData()
    map_data.image = ImageData(
        0,
        0,
        0,
        40,
        60,
        ImageConfig(scale=8),
        Image.new("RGBA", (480, 320), (156, 202, 255, 255)),
        lambda point: point,
    )
    map_data.path = Path(2, 2, 0, [[Point(10, 20), Point(50, 20)]])
    if mop_segments is not None:
        map_data.mop_path = Path(
            None,
            None,
            None,
            [[Point(x, 20) for x in segment] for segment in mop_segments],
        )
    _create_image_generator(
        MapParserConfig(
            map_scale=8,
            drawables=[Drawable.MOP_PATH, Drawable.PATH],
        )
    ).draw_map(map_data)
    return map_data.image.data


def test_rendered_mopping_swath_sits_under_a_fine_white_trace():
    image = render_trace(mop_segments=[[10, 50]])
    room = (156, 202, 255, 255)
    mop = (181, 219, 255, 255)
    trace = (249, 252, 255, 255)
    assert [image.getpixel((240, y)) for y in range(320)] == (
        [room] * 129 + [mop] * 22 + [trace] * 3 + [mop] * 23 + [room] * 143
    )


def test_missing_mop_telemetry_never_invents_mopping_coverage():
    image = render_trace(mop_segments=None)
    room = (156, 202, 255, 255)
    trace = (247, 251, 255, 255)
    assert [image.getpixel((240, y)) for y in range(320)] == (
        [room] * 151 + [trace] * 3 + [room] * 166
    )


def test_disconnected_mop_segments_do_not_paint_the_gap():
    image = render_trace(mop_segments=[[10, 20], [40, 50]])
    room = (156, 202, 255, 255)
    trace = (247, 251, 255, 255)
    assert [image.getpixel((240, y)) for y in range(320)] == (
        [room] * 151 + [trace] * 3 + [room] * 166
    )
