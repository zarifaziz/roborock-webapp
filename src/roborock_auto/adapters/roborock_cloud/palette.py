"""Mobile-style room colours and cleaning traces for the vendor map renderer.

The vendor's ``MapParserConfig`` exposes no palette hook: every colour is decided
inside the module-level ``_create_rendering_components`` in ``roborock.map.map_parser``.
So this module owns the ``ColorsPalette`` and installs it by patching that one
function, preserving the vendor's geometry and sizing cleaning traces to the map.
The adjacency-aware room recolouring in the vendor picks from the palette's room colours, so replacing
them here changes the whole pool.
"""

from __future__ import annotations

from typing import Any

from vacuum_map_parser_base.config.color import Color, ColorsPalette, SupportedColor
from vacuum_map_parser_base.config.size import Size, Sizes

_TRANSPARENT: Color = (0, 0, 0, 0)

# Room fill pool, applied cyclically over every vendor room slot so rooms with
# high segment ids still land in the same reference palette.
ROOM_COLOR_POOL: tuple[Color, ...] = (
    (156, 202, 255),
    (22, 213, 203),
    (255, 175, 151),
    (255, 221, 94),
)

_ROOM_SLOTS = len(ColorsPalette.ROOM_COLORS)

MAP_COLORS: dict[SupportedColor, Color] = {
    SupportedColor.CARPETS: (142, 175, 191),
    SupportedColor.MAP_INSIDE: (156, 202, 255),
    SupportedColor.MAP_WALL: (111, 125, 137, 255),
    SupportedColor.MAP_WALL_V2: (111, 125, 137, 255),
    SupportedColor.GREY_WALL: (111, 125, 137, 255),
    SupportedColor.OBSTACLE: (90, 95, 102, 140),
    SupportedColor.OBSTACLE_WITH_PHOTO: (90, 95, 102, 140),
    SupportedColor.IGNORED_OBSTACLE: (90, 95, 102, 140),
    SupportedColor.IGNORED_OBSTACLE_WITH_PHOTO: (90, 95, 102, 140),
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


def build_room_colors() -> dict[str, Color]:
    return {
        str(slot): ROOM_COLOR_POOL[(slot - 1) % len(ROOM_COLOR_POOL)]
        for slot in range(1, _ROOM_SLOTS + 1)
    }


def build_palette(
    *, show_background: bool = True, show_walls: bool = True, show_rooms: bool = True
) -> ColorsPalette:
    """The calm palette, honouring the same config flags the vendor factory honours."""
    colors = dict(MAP_COLORS)
    if not show_background:
        colors[SupportedColor.MAP_OUTSIDE] = _TRANSPARENT
    if not show_walls:
        colors[SupportedColor.GREY_WALL] = _TRANSPARENT
        colors[SupportedColor.MAP_WALL] = _TRANSPARENT
        colors[SupportedColor.MAP_WALL_V2] = _TRANSPARENT
    if show_rooms:
        room_colors = build_room_colors()
    else:
        room_colors = {str(slot): _TRANSPARENT for slot in range(1, _ROOM_SLOTS + 1)}
    return ColorsPalette(colors, room_colors)


def install_calm_palette() -> None:
    """Swap the palette the vendor renderer uses for ours. Idempotent.

    Patches ``_create_rendering_components`` because it is the single seam where
    the vendor builds its palette and sizes. Map geometry remains vendor-owned.
    """
    import roborock.map.map_parser as vendor_map_parser

    if getattr(vendor_map_parser._create_rendering_components, "_calm_palette", False):
        return
    vendor_create = vendor_map_parser._create_rendering_components

    def create_with_calm_palette(config: Any) -> Any:
        _, sizes, image_config = vendor_create(config)
        palette = build_palette(
            show_background=config.show_background,
            show_walls=config.show_walls,
            show_rooms=config.show_rooms,
        )
        trace_sizes = {key: sizes.get_size(key) for key in Size}
        # Each map cell is 5 cm: a 30 cm mop swath must scale with the floor plan.
        trace_sizes[Size.MOP_PATH_WIDTH] = 6 * config.map_scale
        trace_sizes[Size.PATH_WIDTH] = 0.45 * config.map_scale
        return palette, Sizes(trace_sizes), image_config

    create_with_calm_palette._calm_palette = True  # type: ignore[attr-defined]
    vendor_map_parser._create_rendering_components = create_with_calm_palette
