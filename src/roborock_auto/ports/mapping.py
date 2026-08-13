"""Mapping-context port: read-only map and room data. Map-write features (no-go zones,
split/merge) will get a separate MapEditPort when they arrive — writes can destroy the
saved map, so they must not hide inside a read interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import MapData, Room


@runtime_checkable
class MapPort(Protocol):
    async def get_map_png(self) -> bytes: ...
    async def get_map_data(self) -> MapData: ...
    async def list_rooms(self) -> list[Room]: ...

    def map_ok(self) -> bool:
        """False when the most recent map fetch fell back to the placeholder, so the UI
        can say so instead of showing a stale-looking map as if it were live."""
        ...
