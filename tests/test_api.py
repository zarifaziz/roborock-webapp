"""P4: API endpoints via TestClient against the fake gateway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from roborock_auto.adapters.web.server import app
from roborock_auto.domain.models import CleaningMode, ConsumablePart


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    with TestClient(app) as c:
        yield c


def test_status_shape(client):
    body = client.get("/status").json()
    assert set(body) == {
        "state_name", "battery", "in_cleaning", "docked", "error",
        "fan_power", "mop_route", "clean_area_m2", "clean_time_s", "dock_error",
        "cleaning", "map_ok",
    }
    assert body["map_ok"] is True


def test_carpet_readback(client):
    assert client.get("/carpet").json() == {"mode": "avoid"}  # fake's default
    client.post("/carpet", json={"mode": "rise"})
    assert client.get("/carpet").json() == {"mode": "rise"}


def test_map_png_served(client):
    r = client.get("/map.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_consumables_snapshot(client):
    assert client.get("/consumables").json() == {
        "main_brush_pct": 82,
        "side_brush_pct": 64,
        "filter_pct": 71,
        "sensor_pct": 45,
        "strainer_pct": 88,
        "cleaning_brush_pct": 93,
    }


@pytest.mark.parametrize("part", ["main_brush", "side_brush", "filter", "sensor"])
def test_consumables_reset_happy(client, part):
    assert client.post("/consumables/reset", json={"part": part}).json() == {"ok": True}
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("reset_consumable", ConsumablePart(part)),
    ]


def test_consumables_reset_bad_part_is_422(client):
    assert client.post("/consumables/reset", json={"part": "bogus"}).status_code == 422
    assert client.post("/consumables/reset", json={}).status_code == 422


def test_rooms_and_routines(client):
    rooms = client.get("/rooms").json()
    assert {"segment_id", "name"} <= set(rooms[0])
    routines = client.get("/routines").json()
    assert {"id", "name"} <= set(routines[0])


def test_commands_take_effect(client):
    assert client.post("/start").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is True
    assert client.post("/dock").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is False


def test_stop_ends_job(client):
    assert client.post("/start").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is True
    assert client.post("/stop").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is False
    assert client.app.state.gateway.commands == [("connect",), ("start",), ("stop",)]


def test_find_records_command_without_state_change(client):
    assert client.post("/find").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is False
    assert client.app.state.gateway.commands == [("connect",), ("find_robot",)]


def test_carpet_roundtrip(client):
    assert client.post("/carpet", json={"mode": "rise"}).status_code == 200
    assert client.post("/carpet", json={"mode": "bogus"}).status_code == 422


def test_map_data_shape(client):
    assert client.get("/map-data").json() == {
        "rooms": [
            {"segment_id": 16, "name": "Living Room", "bbox": [0.0, 0.0, 0.5, 0.5]},
            {"segment_id": 17, "name": "Kitchen", "bbox": [0.5, 0.0, 1.0, 0.5]},
            {"segment_id": 18, "name": "Bedroom", "bbox": [0.0, 0.5, 0.5, 1.0]},
            {"segment_id": 19, "name": "Hallway", "bbox": [0.5, 0.5, 1.0, 1.0]},
        ],
        "robot": {"x": 50, "y": 50},
        "image_width": 100,
        "image_height": 100,
    }


@pytest.mark.parametrize("mode", ["vacuum", "mop", "vac_and_mop"])
def test_clean_segments_happy(client, mode):
    assert client.post(
        "/clean/segments", json={"segment_ids": [16, 17], "mode": mode}
    ).json() == {"ok": True}
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("set_cleaning_mode", CleaningMode(mode)),
        ("clean_rooms", (16, 17)),
    ]


def test_clean_segments_repeat_threads_through(client):
    assert client.post(
        "/clean/segments", json={"segment_ids": [16, 17], "mode": "vacuum", "repeat": 2}
    ).json() == {"ok": True}
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16, 17), 2),
    ]


@pytest.mark.parametrize("repeat", [0, 4])
def test_clean_segments_repeat_out_of_range_is_422(client, repeat):
    assert client.post(
        "/clean/segments", json={"segment_ids": [16], "mode": "vacuum", "repeat": repeat}
    ).status_code == 422


def test_clean_segments_empty_ids_is_422(client):
    assert client.post(
        "/clean/segments", json={"segment_ids": [], "mode": "vacuum"}
    ).status_code == 422


def test_clean_segments_requires_mode(client):
    # Room cleaning must always name a cleaning mode; without it the robot would inherit
    # whatever mode it was last in (e.g. mopping on a "vacuum" request).
    assert client.post("/clean/segments", json={"segment_ids": [16]}).status_code == 422


def test_clean_segments_bad_mode_is_422(client):
    assert client.post(
        "/clean/segments", json={"segment_ids": [16], "mode": "bogus"}
    ).status_code == 422


def test_two_stage_scoped_starts_running(client):
    assert client.post("/clean/two-stage", json={"segment_ids": [16]}).json() == {"ok": True}
    assert client.get("/clean/two-stage/status").json()["running"] is True


def test_auth_status(client):
    assert client.get("/auth/status").json() == {
        "connected": True,
        "mode": "fake",
        "email": None,
    }


def test_auth_request_code_happy(client):
    assert client.post("/auth/request-code", json={"email": "a@b.com"}).json() == {"ok": True}


def test_auth_request_code_empty_is_422(client):
    assert client.post("/auth/request-code", json={"email": "  "}).status_code == 422
    assert client.post("/auth/request-code", json={}).status_code == 422


def test_auth_submit_code_happy(client):
    body = {"email": "a@b.com", "code": "1234"}
    assert client.post("/auth/submit-code", json=body).json() == {"ok": True}


def test_auth_submit_code_empty_is_422(client):
    assert client.post("/auth/submit-code", json={"email": "a@b.com", "code": ""}).status_code == 422
    assert client.post("/auth/submit-code", json={"code": "1234"}).status_code == 422


def test_auth_discover_shape(client):
    assert client.get("/auth/discover").json() == {
        "mode": "fake",
        "carpet_mode": "avoid",
        "rooms": [
            {"segment_id": 16, "name": "Living Room", "bbox": None},
            {"segment_id": 17, "name": "Kitchen", "bbox": None},
            {"segment_id": 18, "name": "Bedroom", "bbox": None},
            {"segment_id": 19, "name": "Hallway", "bbox": None},
        ],
        "routines": [
            {"id": 1001, "name": "Vacuum whole house"},
            {"id": 1002, "name": "Mop whole house"},
        ],
        "consumables": {
            "main_brush_pct": 82,
            "side_brush_pct": 64,
            "filter_pct": 71,
            "sensor_pct": 45,
            "strainer_pct": 88,
            "cleaning_brush_pct": 93,
        },
    }


def test_two_stage_starts_and_reports_running(client):
    assert client.get("/clean/two-stage/status").json() == {
        "running": False,
        "stage": "idle",
        "message": "",
        "error": None,
    }
    assert client.post("/clean/two-stage").json() == {"ok": True}
    status = client.get("/clean/two-stage/status").json()
    assert status["running"] is True
    # The fake advances on a real clock, so the exact live stage is timing-dependent; it is
    # one of the active tokens, never idle/error, while running. Full ordering is pinned in
    # test_two_stage_reports_stage_sequence.
    assert status["stage"] in {"vacuuming", "vacuum_waiting", "mopping", "mop_waiting"}
    assert status["error"] is None


def test_two_stage_conflict_when_running(client):
    assert client.post("/clean/two-stage").json() == {"ok": True}
    r = client.post("/clean/two-stage")
    assert r.status_code == 409
    assert r.json() == {"detail": "a two-stage clean is already running"}
