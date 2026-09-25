"""Mapping-context port: read-only map and room data. Map-write features (no-go zones,
split/merge) will get a separate MapEditPort when they arrive — writes can destroy the
saved map, so they must not hide inside a read interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import MapData, Room


@runtime_checkable
class MapPort(Protocol):
    async def get_map_png(self) -> bytes:
        """Latest real PNG, retained on failure; empty when no map is available yet."""
        ...
    async def get_map_data(self) -> MapData: ...
    async def get_map_text(self) -> str:
        """A compact text/ASCII rendering of the map — rooms, robot position, and driven-path
        coverage — cheap to read (for an agent or a terminal) instead of the PNG."""
        ...

    async def list_rooms(self) -> list[Room]: ...

    def map_ok(self) -> bool:
        """False when the latest fetch failed; any retained map must be marked stale."""
        ...
