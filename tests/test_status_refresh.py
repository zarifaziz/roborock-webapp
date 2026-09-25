"""Status viewers share reads without caching command verification."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Request

from roborock_auto.adapters.web import server
from roborock_auto.domain.models import DeviceStatus

STATUS = DeviceStatus('charging', 100, False, True, cleaning_mode='vacuum')


def request_for(gateway):
    state = SimpleNamespace(gateway=gateway, status_lock=asyncio.Lock(),
                            status_cache={'at': None, 'status': None, 'error': None})
    return Request({'type': 'http', 'app': SimpleNamespace(state=state)})


async def test_concurrent_slow_status_reads_share_one_device_request():
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        entered.set()
        await release.wait()
        return STATUS

    gateway = SimpleNamespace(get_status=AsyncMock(side_effect=read), map_ok=Mock(return_value=True))
    request = request_for(gateway)
    first = asyncio.create_task(server.status(request))
    await entered.wait()
    others = [asyncio.create_task(server.status(request)) for _ in range(4)]
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(first, *others)
    assert (responses, gateway.get_status.await_count) == ([{**asdict(STATUS), 'map_ok': True}] * 5, 1)


async def test_status_ttl_starts_after_completion_and_dynamic_map_flag_stays_fresh(monkeypatch):
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: clock.now))

    async def read():
        clock.now += 20
        return STATUS

    gateway = SimpleNamespace(get_status=AsyncMock(side_effect=read), map_ok=Mock(return_value=True))
    request = request_for(gateway)
    first = await server.status(request)
    gateway.map_ok.return_value = False
    clock.now += .5
    second = await server.status(request)
    assert (first, second, gateway.get_status.await_count) == (
        {**asdict(STATUS), 'map_ok': True}, {**asdict(STATUS), 'map_ok': False}, 1,
    )
    clock.now += 1
    await server.status(request)
    assert gateway.get_status.await_count == 2


async def test_failed_status_reads_are_coalesced_and_recover_after_ttl(monkeypatch):
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    gateway = SimpleNamespace(get_status=AsyncMock(side_effect=[TimeoutError('offline'), STATUS]),
                              map_ok=lambda: True)
    request = request_for(gateway)
    for _ in range(3):
        with pytest.raises(HTTPException) as error:
            await server.status(request)
        assert (error.value.status_code, error.value.detail) == (503, 'Robot status temporarily unavailable')
    assert gateway.get_status.await_count == 1
    clock.now += 1.1
    assert await server.status(request) == {**asdict(STATUS), 'map_ok': True}
    assert gateway.get_status.await_count == 2


async def test_map_timeout_releases_device_lock_before_next_poll_retry():
    from roborock_auto.adapters.roborock_cloud.adapter import RoborockCloudAdapter

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def map_read():
        calls.append('map')
        entered.set()
        await release.wait()
        raise TimeoutError('cloud map timeout')

    async def status_read():
        calls.append('status')
        return STATUS

    gateway = RoborockCloudAdapter(SimpleNamespace())
    gateway._device = SimpleNamespace(v1_properties=SimpleNamespace(
        map_content=SimpleNamespace(refresh=map_read),
    ))
    map_request = asyncio.create_task(gateway.get_map_png())
    await entered.wait()
    status_request = asyncio.create_task(gateway._locked_retry(status_read))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(map_request, status_request) == [b'', STATUS]
    assert calls == ['map', 'status']
    assert await gateway.get_map_png() == b''
    assert calls == ['map', 'status', 'map']
