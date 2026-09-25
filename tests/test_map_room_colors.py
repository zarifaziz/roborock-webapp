"""Room accents must use the adjacency-adjusted palette that rendered the PNG."""

from io import BytesIO
from struct import pack, pack_into
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from roborock.devices.traits.v1.map_content import MapContentConverter
from roborock.map.map_parser import MapParser, MapParserConfig

from roborock_auto.adapters.roborock_cloud.adapter import RoborockCloudAdapter
from roborock_auto.adapters.roborock_cloud.palette import install_calm_palette


def map_bytes(segments):
    header = bytearray(20)
    pack_into('<H', header, 2, len(header))
    cells = bytes((segment << 3) | 7 for segment in segments)
    image_header = pack('<HHIIIII', 2, 24, len(cells), 0, 0, 1, len(cells))
    return bytes(header) + image_header + cells


def gateway_with_map(segments):
    install_calm_palette()
    converter = MapContentConverter(MapParser(MapParserConfig(map_scale=1, drawables=[])))
    content = SimpleNamespace(converter=converter)

    async def refresh():
        parsed = converter.parse_map_content(map_bytes(segments))
        content.image_content, content.map_data = parsed.image_content, parsed.map_data

    content.refresh = AsyncMock(side_effect=refresh)
    rooms = SimpleNamespace(refresh=AsyncMock(), rooms=[
        SimpleNamespace(segment_id=1, name='Master bedroom'),
        SimpleNamespace(segment_id=5, name='Bathroom1'),
    ])
    gw = RoborockCloudAdapter(SimpleNamespace())
    gw._device = SimpleNamespace(v1_properties=SimpleNamespace(map_content=content, rooms=rooms))
    return gw, content


async def test_adjacent_rooms_with_same_base_color_use_rendered_accents():
    gw, content = gateway_with_map([1, 5])
    png = await gw.get_map_png()
    image = Image.open(BytesIO(png))
    pixels = [image.getpixel((x, 0)) for x in range(image.width)]
    assert pixels == [(156, 202, 255, 255), (22, 213, 203, 255)]
    metadata = await gw.get_map_data()
    rooms = await gw.list_rooms()
    assert {
        'map': {room.segment_id: room.color for room in metadata.rooms},
        'rooms': {room.segment_id: room.color for room in rooms},
    } == {
        'map': {1: '#9ccaff', 5: '#16d5cb'},
        'rooms': {1: '#9ccaff', 5: '#16d5cb'},
    }
    content.refresh.side_effect = AttributeError('offline')
    assert (await gw.get_map_png(), await gw.get_map_data()) == (png, metadata)
    assert {room.segment_id: room.color for room in await gw.list_rooms()} == {
        1: '#9ccaff', 5: '#16d5cb',
    }


async def test_room_colors_are_unknown_until_a_map_has_rendered():
    gw, _ = gateway_with_map([1, 5])
    assert {room.segment_id: room.color for room in await gw.list_rooms()} == {1: None, 5: None}


async def test_next_map_updates_palette_assignment_without_rebuilding_parser():
    segments = [1, 5]
    gw, _ = gateway_with_map(segments)
    await gw.get_map_png()
    first = await gw.get_map_data()
    segments[:] = [1, 0, 5]  # Unsegmented cell separates the rooms: both may be blue.
    png = await gw.get_map_png()
    second = await gw.get_map_data()
    image = Image.open(BytesIO(png))
    assert (first.rooms[1].color, second.rooms[1].color, image.getpixel((2, 0))) == (
        '#16d5cb', '#9ccaff', (156, 202, 255, 255),
    )
    assert {room.segment_id: room.color for room in await gw.list_rooms()} == {
        1: '#9ccaff', 5: '#9ccaff',
    }


async def test_unavailable_renderer_palette_reports_unknown_instead_of_guessing():
    gw, content = gateway_with_map([1, 5])
    await content.refresh()
    content.refresh = AsyncMock()
    del content.converter
    await gw.get_map_png()
    assert {room.segment_id: room.color for room in (await gw.get_map_data()).rooms} == {
        1: None, 5: None,
    }
