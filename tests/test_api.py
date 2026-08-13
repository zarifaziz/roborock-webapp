"""P4: API endpoints via TestClient against the fake gateway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from roborock_auto.adapters.web.server import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    with TestClient(app) as c:
        yield c


def test_status_shape(client):
    body = client.get("/status").json()
    assert set(body) == {"state_name", "battery", "in_cleaning", "docked", "error"}


def test_map_png_served(client):
    r = client.get("/map.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


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


def test_carpet_roundtrip(client):
    assert client.post("/carpet", json={"mode": "rise"}).status_code == 200
    assert client.post("/carpet", json={"mode": "bogus"}).status_code == 422


def test_clean_rooms(client):
    assert client.post("/clean/rooms", json={"segment_ids": [16, 17]}).json() == {"ok": True}
