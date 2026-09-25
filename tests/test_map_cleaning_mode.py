"""The map must not present a vacuum-only pass as mopped coverage."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from roborock.map.map_parser import MapParserConfig
from vacuum_map_parser_base.config.drawable import Drawable

from roborock_auto.adapters.roborock_cloud.adapter import RoborockCloudAdapter


async def test_map_drawables_follow_current_cleaning_mode_without_extra_status_reads():
    gateway = RoborockCloudAdapter(SimpleNamespace())
    gateway._map_config = MapParserConfig(
        drawables=[Drawable.MOP_PATH, Drawable.PATH, Drawable.CHARGER, Drawable.VACUUM_POSITION]
    )
    status = SimpleNamespace(current_cleaning_mode_name="vacuum", refresh=AsyncMock())
    content = SimpleNamespace(map_data=None, image_content=b"")
    observed = []

    async def render():
        observed.append(list(gateway._map_config.drawables))
        content.image_content = b"with-coverage" if Drawable.MOP_PATH in observed[-1] else b"line-only"

    content.refresh = AsyncMock(side_effect=render)
    gateway._device = SimpleNamespace(v1_properties=SimpleNamespace(status=status, map_content=content))
    frames = []
    for mode in ("vacuum", "mop", "vac_and_mop", "vacuum", None):
        status.current_cleaning_mode_name = mode
        frames.append(await gateway.get_map_png())

    line = [Drawable.PATH, Drawable.CHARGER, Drawable.VACUUM_POSITION]
    mop = [Drawable.MOP_PATH, *line]
    assert {"frames": frames, "drawables": observed, "status_reads": status.refresh.await_count} == {
        "frames": [b"line-only", b"with-coverage", b"with-coverage", b"line-only", b"line-only"],
        "drawables": [line, mop, mop, line, line],
        "status_reads": 0,
    }


async def test_existing_vendor_renderer_tracks_mode_changes_in_actual_pngs():
    from io import BytesIO

    from PIL import Image
    from roborock.map.map_parser import _create_image_generator
    from vacuum_map_parser_base.config.image_config import ImageConfig
    from vacuum_map_parser_base.map_data import ImageData, MapData, Path, Point

    from roborock_auto.adapters.roborock_cloud.palette import install_calm_palette

    install_calm_palette()

    def render(generator):
        data = MapData()
        data.image = ImageData(
            0, 0, 0, 40, 60, ImageConfig(scale=8),
            Image.new("RGBA", (480, 320), (156, 202, 255, 255)), lambda point: point,
        )
        data.path = Path(2, 2, 0, [[Point(10, 20), Point(50, 20)]])
        data.mop_path = Path(None, None, None, [[Point(10, 20), Point(50, 20)]])
        generator.draw_map(data)
        output = BytesIO()
        data.image.data.save(output, format="PNG")
        return output.getvalue()

    line_png = render(_create_image_generator(MapParserConfig(map_scale=8, drawables=[Drawable.PATH])))
    mop_png = render(_create_image_generator(MapParserConfig(
        map_scale=8, drawables=[Drawable.MOP_PATH, Drawable.PATH],
    )))
    assert line_png != mop_png
    gateway = RoborockCloudAdapter(SimpleNamespace())
    gateway._map_config = MapParserConfig(map_scale=8, drawables=[Drawable.MOP_PATH, Drawable.PATH])
    generator = _create_image_generator(gateway._map_config)
    status = SimpleNamespace(current_cleaning_mode_name="vacuum")
    content = SimpleNamespace(map_data=None, image_content=b"")

    async def refresh():
        content.image_content = render(generator)

    content.refresh = AsyncMock(side_effect=refresh)
    gateway._device = SimpleNamespace(v1_properties=SimpleNamespace(status=status, map_content=content))
    frames = []
    for mode in ("vacuum", "mop", "vac_and_mop", "vacuum", None):
        status.current_cleaning_mode_name = mode
        frames.append(await gateway.get_map_png())
    assert frames == [line_png, mop_png, mop_png, line_png, line_png]
