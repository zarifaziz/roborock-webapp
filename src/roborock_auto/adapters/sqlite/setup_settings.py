"""SetupSettingsPort over the setup_settings table: one row per device, a null column is
a setting the user never chose."""

from __future__ import annotations

import sqlite3

from roborock_auto.domain.models import CleanSettings, FanPower, MopMode, WaterMode


class SqliteSetupSettings:
    def __init__(self, conn: sqlite3.Connection, device_id: str) -> None:
        self._conn = conn
        self._device_id = device_id

    async def get_setup_settings(self) -> CleanSettings:
        row = self._conn.execute(
            "SELECT fan_power, water_mode, mop_route FROM setup_settings WHERE device_id = ?",
            (self._device_id,),
        ).fetchone()
        self._conn.commit()
        if row is None:
            return CleanSettings()
        fan_power, water_mode, mop_route = row
        return CleanSettings(
            fan_power=None if fan_power is None else FanPower(fan_power),
            water_mode=None if water_mode is None else WaterMode(water_mode),
            mop_route=None if mop_route is None else MopMode(mop_route),
        )

    async def save_setup_settings(self, settings: CleanSettings) -> None:
        self._conn.execute(
            """
            INSERT INTO setup_settings (device_id, fan_power, water_mode, mop_route)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (device_id) DO UPDATE SET
                fan_power = COALESCE(excluded.fan_power, fan_power),
                water_mode = COALESCE(excluded.water_mode, water_mode),
                mop_route = COALESCE(excluded.mop_route, mop_route)
            """,
            (
                self._device_id,
                None if settings.fan_power is None else settings.fan_power.value,
                None if settings.water_mode is None else settings.water_mode.value,
                None if settings.mop_route is None else settings.mop_route.value,
            ),
        )
        self._conn.commit()
