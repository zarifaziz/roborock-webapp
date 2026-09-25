"""Live map freshness and shared refreshes, without a robot or wall-clock waits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request

from roborock_auto.adapters.web import server


def _request(gateway):
    if not hasattr(gateway, "map_ok"):
        gateway.map_ok = Mock(return_value=True)
    state = SimpleNamespace(
        gateway=gateway,
        map_cache={"at": 0.0, "png": b""},
        map_lock=asyncio.Lock(),
    )
    return Request({"type": "http", "app": SimpleNamespace(state=state)})


async def test_map_updates_on_the_next_three_second_poll(monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock.value))
    gateway = SimpleNamespace(get_map_png=AsyncMock(side_effect=[b"position-1", b"position-2"]))
    request = _request(gateway)

    first = await server.map_png(request)
    clock.value += 3.0
    second = await server.map_png(request)

    assert [first.body, second.body, gateway.get_map_png.await_count] == [
        b"position-1", b"position-2", 2,
    ]


async def test_simultaneous_map_requests_share_one_device_fetch():
    started, release = asyncio.Event(), asyncio.Event()

    async def read_map():
        started.set()
        await release.wait()
        return b"current-position"

    gateway = SimpleNamespace(get_map_png=AsyncMock(side_effect=read_map))
    request = _request(gateway)
    first = asyncio.create_task(server.map_png(request))
    await started.wait()
    second = asyncio.create_task(server.map_png(request))
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(first, second)

    assert [response.body for response in responses] == [b"current-position"] * 2
    assert gateway.get_map_png.await_count == 1


async def test_slow_map_fetch_is_cached_from_completion(monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock.value))

    async def read_map():
        clock.value += 5.0
        return b"current-position"

    gateway = SimpleNamespace(get_map_png=AsyncMock(side_effect=read_map))
    request = _request(gateway)
    first = await server.map_png(request)
    clock.value += 1.0
    second = await server.map_png(request)

    assert [first.body, second.body, gateway.get_map_png.await_count] == [
        b"current-position", b"current-position", 1,
    ]


async def test_live_map_cannot_be_cached_by_the_browser():
    gateway = SimpleNamespace(get_map_png=AsyncMock(return_value=b"current-position"))
    response = await server.map_png(_request(gateway))

    assert dict(response.headers) == {
        "cache-control": "no-store",
        "x-map-stale": "false",
        "content-length": "16",
        "content-type": "image/png",
    }


async def test_map_failure_before_first_success_is_unavailable():
    gateway = SimpleNamespace(get_map_png=AsyncMock(return_value=b""), map_ok=lambda: False)
    response = await server.map_png(_request(gateway))
    assert (response.status_code, response.body, dict(response.headers)) == (
        503, b"", {"cache-control": "no-store", "x-map-stale": "true", "content-length": "0"},
    )


async def test_cached_map_keeps_its_own_stale_flag():
    gateway = SimpleNamespace(
        get_map_png=AsyncMock(return_value=b"last-real-map"), map_ok=Mock(return_value=False),
    )
    request = _request(gateway)
    first = await server.map_png(request)
    gateway.map_ok.return_value = True
    second = await server.map_png(request)
    assert [(r.status_code, r.body, r.headers["x-map-stale"]) for r in (first, second)] == [
        (200, b"last-real-map", "true"), (200, b"last-real-map", "true"),
    ]
    assert gateway.get_map_png.await_count == 1


async def test_unavailable_initial_map_is_coalesced_and_retried_after_ttl(monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock.value))
    gateway = SimpleNamespace(
        get_map_png=AsyncMock(side_effect=[b"", b"recovered"]), map_ok=Mock(return_value=False),
    )
    request = _request(gateway)
    responses = await asyncio.gather(*(server.map_png(request) for _ in range(3)))
    assert ([(r.status_code, r.body) for r in responses], gateway.get_map_png.await_count) == (
        [(503, b"")] * 3, 1,
    )
    clock.value += 3
    gateway.map_ok.return_value = True
    recovered = await server.map_png(request)
    assert (recovered.status_code, recovered.body, recovered.headers["x-map-stale"],
            gateway.get_map_png.await_count) == (200, b"recovered", "false", 2)



@pytest.mark.parametrize('first_route', ['png', 'metadata'])
async def test_first_image_and_metadata_share_the_same_initial_map_fetch(first_route):
    from roborock_auto.domain.models import MapData

    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = None

    async def read_png():
        nonlocal snapshot
        entered.set()
        await release.wait()
        snapshot = MapData(image_width=100, image_height=80)
        return b'initial-map'

    async def read_data():
        if snapshot is None:
            await gateway.get_map_png()
        return snapshot

    gateway = SimpleNamespace(get_map_png=AsyncMock(side_effect=read_png),
                              get_map_data=AsyncMock(side_effect=read_data), map_ok=lambda: True)
    request = _request(gateway)
    handlers = [server.map_png, server.map_data]
    if first_route == 'metadata':
        handlers.reverse()
    first = asyncio.create_task(handlers[0](request))
    await entered.wait()
    second = asyncio.create_task(handlers[1](request))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    if first_route == 'metadata':
        results.reverse()
    assert (results[0].body, results[1], gateway.get_map_png.await_count) == (
        b'initial-map', {'rooms': [], 'robot': None, 'image_width': 100, 'image_height': 80}, 1,
    )
