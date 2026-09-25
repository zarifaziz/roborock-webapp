"""Live map freshness and shared refreshes, without a robot or wall-clock waits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Request

from roborock_auto.adapters.web import server


def _request(gateway):
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
        "content-length": "16",
        "content-type": "image/png",
    }
