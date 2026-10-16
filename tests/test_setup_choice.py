"""The Setup tab's fan, water and route are the user's standing choice, kept by the app.

The robot cannot hold them: a vacuum-only clean switches water off and a mop-only clean
switches suction off, so reading Setup back from the robot loses it after one clean.
These drive the real server wiring over a fake robot whose clock moves on each poll.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from roborock_auto.adapters.cron import run as cron
from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.adapters.sqlite import SqliteSetupSettings, open_database
from roborock_auto.adapters.web import server
from roborock_auto.adapters.web.server import app
from roborock_auto.domain.models import CleanSettings, FanPower, MopMode, WaterMode

TURBO_VACUUM = ("turbo", None, "standard")
INTENSE_DEEP_MOP = ("off", "intense", "deep")
TURBO_INTENSE_DEEP = ("turbo", "intense", "deep")


class TickingRobot(FakeRobot):
    """Five fake seconds pass per status read, so a whole clean takes a few real polls.
    Notes (fan power, water flow, mop route) at the moment each clean is triggered."""

    def __init__(self) -> None:
        self.t = 0.0
        super().__init__(clock=lambda: self.t)
        self.settings_at_departure: list[tuple[str | None, str | None, str | None]] = []

    async def get_status(self):
        self.t += 5.0
        return await super().get_status()

    async def _note(self) -> None:
        status = await super().get_status()
        self.settings_at_departure.append((status.fan_power, status.water_mode, status.mop_route))

    async def start(self) -> None:
        await self._note()
        await super().start()

    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        await self._note()
        await super().clean_rooms(segment_ids, repeat)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "roborock.db"))
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")
    monkeypatch.setenv("POLL_INTERVAL_SEC", "0.01")
    monkeypatch.setattr(server, "get_gateway", lambda _config: TickingRobot())
    with TestClient(app) as c:
        yield c


def _choose_turbo_intense_deep(client) -> None:
    assert client.post("/fan", json={"power": "turbo"}).json() == {"ok": True}
    assert client.post("/water", json={"mode": "intense"}).json() == {"ok": True}
    assert client.post("/mop", json={"mode": "deep"}).json() == {"ok": True}


def _run_to_completion(client, path: str) -> dict:
    assert client.post(path).json() == {"ok": True}
    deadline = time.monotonic() + 10.0
    progress = client.get("/sequences/progress").json()
    while progress["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        progress = client.get("/sequences/progress").json()
    return {"running": progress["running"], "stage": progress["stage"]}


def _whole_house(mode: str, carpet: str) -> dict:
    return {"mode": mode, "carpet": carpet, "segment_ids": None}


def test_a_sequence_keeps_setup_on_every_run_not_just_the_first(client):
    _choose_turbo_intense_deep(client)
    client.post(
        "/sequences",
        json={
            "name": "Vacuum then mop",
            "steps": [_whole_house("vacuum", "rise"), _whole_house("mop", "avoid")],
        },
    )
    done = {"running": False, "stage": "done"}
    assert _run_to_completion(client, "/sequences/1/run") == done
    assert _run_to_completion(client, "/sequences/1/run") == done
    assert client.app.state.gateway.settings_at_departure == [
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
    ]


def test_the_daily_two_stage_clean_keeps_setup_on_day_two(client):
    _choose_turbo_intense_deep(client)
    done = {"running": False, "stage": "done"}
    assert _run_to_completion(client, "/clean/two-stage") == done
    assert _run_to_completion(client, "/clean/two-stage") == done
    assert client.app.state.gateway.settings_at_departure == [
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
    ]


def test_plain_cleans_keep_setup_through_vacuum_then_mop_then_both(client):
    _choose_turbo_intense_deep(client)
    for mode in ("vacuum", "mop", "vac_and_mop"):
        assert client.post("/start", json={"mode": mode}).json() == {"ok": True}
        assert client.post("/dock").json() == {"ok": True}
    assert client.app.state.gateway.settings_at_departure == [
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
        TURBO_INTENSE_DEEP,
    ]


def test_setup_shows_the_stored_choice_after_a_mop_clean_left_suction_off(client):
    _choose_turbo_intense_deep(client)
    client.post("/start", json={"mode": "mop"})
    client.post("/dock")
    client.app.state.status_cache["at"] = None
    body = client.get("/status").json()
    assert (body["fan_power"], body["setup"]) == (
        "off",
        {"fan_power": "turbo", "water_mode": "intense", "mop_route": "deep"},
    )


def test_setup_shows_the_robot_until_the_user_chooses(client):
    assert client.get("/status").json()["setup"] == {
        "fan_power": "balanced",
        "water_mode": "standard",
        "mop_route": "standard",
    }


async def test_the_cron_two_stage_clean_keeps_the_stored_setup_on_day_two(monkeypatch, tmp_path):
    path = str(tmp_path / "roborock.db")
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")
    monkeypatch.setenv("POLL_INTERVAL_SEC", "0.01")
    # Rows are keyed by device; an empty id is the fake's "fake", whatever a .env says.
    monkeypatch.setenv("ROBOROCK_DUID", "")
    db = open_database(path)
    await SqliteSetupSettings(db, device_id="fake").save_setup_settings(
        CleanSettings(FanPower.TURBO, WaterMode.INTENSE, MopMode.DEEP)
    )
    db.close()
    robot = TickingRobot()
    monkeypatch.setattr(cron, "get_gateway", lambda _config: robot)
    assert (await cron.main(), await cron.main()) == (0, 0)
    assert robot.settings_at_departure == [
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
        TURBO_VACUUM,
        INTENSE_DEEP_MOP,
    ]
