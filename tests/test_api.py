"""P4: API endpoints via TestClient against the fake gateway."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.adapters.web import server
from roborock_auto.adapters.web.server import app
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    CleaningRun,
    CleanSource,
    ConsumablePart,
    DeviceStatus,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")
    with TestClient(app) as c:
        yield c


def _runs(client) -> list[CleaningRun]:
    return asyncio.run(client.app.state.cleaning_log.list_runs())


def test_status_shape(client):
    body = client.get("/status").json()
    assert set(body) == {
        "state_name", "battery", "in_cleaning", "docked", "error",
        "fan_power", "mop_route", "water_mode", "clean_area_m2", "clean_time_s", "dock_error",
        "cleaning_mode", "drying_mop",
        "cleaning", "map_ok",
    }
    assert body["map_ok"] is True


def test_carpet_readback(client):
    assert client.get("/carpet").json() == {"mode": "avoid"}  # fake's default
    client.post("/carpet", json={"mode": "rise"})
    assert client.get("/carpet").json() == {"mode": "rise"}


def test_map_txt_served(client):
    r = client.get("/map.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "@ = robot" in body
    assert "Living Room" in body and "Kitchen" in body and "Hallway" in body


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
    assert client.post("/start", json={"mode": "vacuum"}).json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is True
    assert client.post("/dock").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is False


def test_stop_ends_job(client):
    assert client.post("/start", json={"mode": "vacuum"}).json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is True
    assert client.post("/stop").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is False
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("start",),
        ("stop",),
    ]


def test_start_requires_a_mode(client):
    # A whole-house clean must name its cleaning mode; without it the robot would inherit
    # whatever mode it was last in.
    assert client.post("/start").status_code == 422
    assert client.post("/start", json={"mode": "bogus"}).status_code == 422


def test_start_records_a_cleaning_run(client):
    assert client.post("/start", json={"mode": "vacuum"}).json() == {"ok": True}
    runs = _runs(client)
    assert runs == [
        CleaningRun(
            source=CleanSource.APP,
            mode=CleaningMode.VACUUM,
            segment_ids=None,  # whole house
            started_at=runs[0].started_at,
        ),
    ]
    assert runs[0].started_at.tzinfo == UTC


def test_resume_sends_a_bare_start_and_records_nothing(client):
    # Resuming a paused job is not a new Clean: no mode change, no Cleaning Run.
    assert client.post("/resume").json() == {"ok": True}
    assert client.app.state.gateway.commands == [("connect",), ("start",)]
    assert _runs(client) == []


def test_clean_segments_records_a_cleaning_run(client):
    assert client.post(
        "/clean/segments", json={"segment_ids": [16, 17], "mode": "mop"}
    ).json() == {"ok": True}
    runs = _runs(client)
    assert runs == [
        CleaningRun(
            source=CleanSource.APP,
            mode=CleaningMode.MOP,
            segment_ids=(16, 17),
            started_at=runs[0].started_at,
        ),
    ]
    assert runs[0].started_at.tzinfo == UTC


def test_find_records_command_without_state_change(client):
    assert client.post("/find").json() == {"ok": True}
    assert client.get("/status").json()["in_cleaning"] is False
    assert client.app.state.gateway.commands == [("connect",), ("find_robot",)]


def test_carpet_roundtrip(client):
    assert client.post("/carpet", json={"mode": "rise"}).status_code == 200
    assert client.post("/carpet", json={"mode": "bogus"}).status_code == 422


def test_water_roundtrip(client):
    assert client.get("/status").json()["water_mode"] == "standard"  # fake's default
    assert client.post("/water", json={"mode": "intense"}).json() == {"ok": True}
    assert client.get("/status").json()["water_mode"] == "intense"
    assert client.post("/water", json={"mode": "bogus"}).status_code == 422


def test_map_data_shape(client):
    assert client.get("/map-data").json() == {
        "rooms": [
            {"segment_id": 16, "name": "Living Room", "bbox": [0.0, 0.0, 0.5, 0.5], "color": None},
            {"segment_id": 17, "name": "Kitchen", "bbox": [0.5, 0.0, 1.0, 0.5], "color": None},
            {"segment_id": 18, "name": "Bedroom", "bbox": [0.0, 0.5, 0.5, 1.0], "color": None},
            {"segment_id": 19, "name": "Hallway", "bbox": [0.5, 0.5, 1.0, 1.0], "color": None},
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
            {"segment_id": 16, "name": "Living Room", "bbox": None, "color": None},
            {"segment_id": 17, "name": "Kitchen", "bbox": None, "color": None},
            {"segment_id": 18, "name": "Bedroom", "bbox": None, "color": None},
            {"segment_id": 19, "name": "Hallway", "bbox": None, "color": None},
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
    assert r.json() == {"detail": "another run is already in progress"}


def test_two_stage_cancel_stops_the_run(client):
    assert client.post("/clean/two-stage").json() == {"ok": True}
    assert client.post("/clean/two-stage/cancel").json() == {"ok": True}
    # Cancellation wakes the run's next wait immediately; give the runner task a beat to
    # dock, restore carpet, and publish the terminal status.
    deadline = time.monotonic() + 5.0
    status = client.get("/clean/two-stage/status").json()
    while status["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
        status = client.get("/clean/two-stage/status").json()
    assert status == {
        "running": False,
        "stage": "cancelled",
        "message": "Two-stage clean cancelled",
        "error": None,
    }
    assert ("dock",) in client.app.state.gateway.commands


def _wait_until_idle(client) -> dict:
    deadline = time.monotonic() + 5.0
    status = client.get("/clean/two-stage/status").json()
    while status["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
        status = client.get("/clean/two-stage/status").json()
    return status


def test_a_failed_two_stage_run_releases_the_slot_for_the_next_run(client):
    client.app.state.gateway.set_timeline(
        [(0.0, DeviceStatus("cleaning", 90, in_cleaning=True, docked=False, cleaning=True))]
    )
    assert client.post("/clean/two-stage").json() == {"ok": True}
    assert _wait_until_idle(client) == {
        "running": False,
        "stage": "error",
        "message": "robot not docked/idle at start (state=cleaning); aborting",
        "error": "robot not docked/idle at start (state=cleaning); aborting",
    }
    assert client.post("/clean/two-stage").json() == {"ok": True}


def test_a_cancelled_two_stage_run_releases_the_slot_for_the_next_run(client):
    assert client.post("/clean/two-stage").json() == {"ok": True}
    assert client.post("/clean/two-stage/cancel").json() == {"ok": True}
    assert _wait_until_idle(client)["stage"] == "cancelled"
    assert client.post("/clean/two-stage").json() == {"ok": True}


def test_two_stage_cancel_when_not_running_is_409(client):
    r = client.post("/clean/two-stage/cancel")
    assert r.status_code == 409
    assert r.json() == {"detail": "no two-stage clean is running"}


@pytest.fixture
def clock(client):
    """Freezes the app's clock so Priority is an exact number; mutate to move time on."""
    holder = {"now": datetime(2026, 8, 20, 9, 0, tzinfo=UTC)}
    client.app.state.now = lambda: holder["now"]
    return holder


def _overdue(segment_id, *, days, cleaned_at=None, priority=None):
    return {
        "segment_id": segment_id,
        "interval_days": days,
        "last_cleaned_at": None if cleaned_at is None else cleaned_at.isoformat(),
        "priority": priority,
        "never_cleaned": cleaned_at is None,
        "overdue": priority is None or priority >= 1.0,
    }


def test_intervals_set_list_and_remove(client):
    assert client.post("/intervals", json={"segment_id": 17, "interval_days": 2}).json() == {
        "ok": True
    }
    assert client.post("/intervals", json={"segment_id": 16, "interval_days": 0.5}).json() == {
        "ok": True
    }
    assert client.get("/intervals").json() == [
        {"segment_id": 16, "interval_days": 0.5},
        {"segment_id": 17, "interval_days": 2.0},
    ]
    assert client.delete("/intervals/16").json() == {"ok": True}
    assert client.get("/intervals").json() == [{"segment_id": 17, "interval_days": 2.0}]


def test_setting_an_interval_again_replaces_it(client):
    client.post("/intervals", json={"segment_id": 16, "interval_days": 7})
    client.post("/intervals", json={"segment_id": 16, "interval_days": 3})
    assert client.get("/intervals").json() == [{"segment_id": 16, "interval_days": 3.0}]


@pytest.mark.parametrize("interval_days", [0, -1, -0.5])
def test_non_positive_interval_is_422(client, interval_days):
    r = client.post("/intervals", json={"segment_id": 16, "interval_days": interval_days})
    assert r.status_code == 422
    assert r.json() == {"detail": "Room Interval must be a positive duration"}
    assert client.get("/intervals").json() == []


def test_interval_requires_both_fields(client):
    assert client.post("/intervals", json={"segment_id": 16}).status_code == 422
    assert client.post("/intervals", json={"interval_days": 2}).status_code == 422


def test_removing_an_interval_that_was_never_set_is_404(client):
    r = client.delete("/intervals/16")
    assert r.status_code == 404
    assert r.json() == {"detail": "segment 16 has no Room Interval"}


def test_overdue_scores_every_segment_with_an_interval_worst_first(client, clock):
    now = clock["now"]
    client.post("/clean/segments", json={"segment_ids": [16, 17], "mode": "vacuum"})
    clock["now"] = now + timedelta(days=3)
    for segment_id, days in ((16, 7), (17, 2), (18, 1)):
        client.post("/intervals", json={"segment_id": segment_id, "interval_days": days})
    assert client.get("/overdue").json() == [
        _overdue(18, days=1.0),
        _overdue(17, days=2.0, cleaned_at=now, priority=1.5),
        _overdue(16, days=7.0, cleaned_at=now, priority=3 / 7),
    ]


def test_overdue_clean_cleans_every_overdue_segment_and_records_the_run(client, clock):
    now = clock["now"]
    client.post("/clean/segments", json={"segment_ids": [16], "mode": "vacuum"})
    client.post("/dock")  # the catch-up clean only starts from a docked, idle robot
    clock["now"] = now + timedelta(days=1)
    for segment_id, days in ((16, 7), (17, 2), (18, 1)):
        client.post("/intervals", json={"segment_id": segment_id, "interval_days": days})
    assert client.post("/clean/overdue", json={"mode": "mop"}).json() == {
        "ok": True,
        "segment_ids": [17, 18],
    }
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16,)),
        ("dock",),
        ("set_cleaning_mode", CleaningMode.MOP),
        ("clean_rooms", (17, 18)),
    ]
    assert _runs(client) == [
        CleaningRun(CleanSource.APP, CleaningMode.VACUUM, (16,), now),
        CleaningRun(CleanSource.APP, CleaningMode.MOP, (17, 18), now + timedelta(days=1)),
    ]


def test_overdue_clean_with_nothing_overdue_leaves_the_robot_alone(client, clock):
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    client.post("/clean/overdue", json={"mode": "vacuum"})
    client.post("/dock")
    client.app.state.gateway.commands.clear()
    assert client.post("/clean/overdue", json={"mode": "vacuum"}).json() == {
        "ok": True,
        "segment_ids": [],
    }
    assert client.app.state.gateway.commands == []


def test_overdue_clean_requires_a_mode(client):
    assert client.post("/clean/overdue").status_code == 422
    assert client.post("/clean/overdue", json={"mode": "bogus"}).status_code == 422


def test_overdue_clean_conflicts_with_a_running_two_stage_clean(client):
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    assert client.post("/clean/two-stage").json() == {"ok": True}
    r = client.post("/clean/overdue", json={"mode": "vacuum"})
    assert r.status_code == 409
    assert r.json() == {"detail": "another run is already in progress"}


def test_overdue_clean_conflicts_when_the_robot_is_not_ready(client):
    # A live status read, not the in-process two-stage flag: a different mechanism, so a
    # different message.
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    client.app.state.gateway.set_timeline(
        [(0.0, DeviceStatus("error", 90, in_cleaning=False, docked=True, error="brush stuck"))]
    )
    r = client.post("/clean/overdue", json={"mode": "vacuum"})
    assert r.status_code == 409
    assert r.json() == {"detail": "the robot is not ready: it must be docked, idle and error-free"}
    assert _runs(client) == []


def _rule_request(**over) -> dict:
    base = {
        "name": "Weekday mornings",
        "recurrence": {"days": ["friday", "monday", "wednesday"], "at": "09:30"},
        "action": {"type": "clean", "mode": "vacuum", "segment_ids": [16, 17]},
        "enabled": True,
    }
    base.update(over)
    return base


def _rule_doc(
    id, *, name="Weekday mornings", days=("monday", "wednesday", "friday"), at="09:30",
    mode="vacuum", segment_ids=(16, 17), enabled=True, next_fires_at=None,
) -> dict:
    return {
        "id": id,
        "name": name,
        "enabled": enabled,
        "recurrence": {"days": list(days), "at": at},
        "action": {
            "type": "clean",
            "mode": mode,
            "segment_ids": None if segment_ids is None else list(segment_ids),
        },
        "next_fires_at": next_fires_at,
    }


def test_schedule_crud_snapshot(client, clock):
    """The full Rule lifecycle at Thu 2026-08-20 19:00 Sydney: create two Rules, list
    them, replace one, delete it. Days echo in canonical Monday-first order and
    next_fires_at is the next UTC instant the Rule's Recurrence implies."""
    weekday = _rule_doc(1, next_fires_at="2026-08-20T23:30:00+00:00")  # Fri 09:30 AEST
    assert client.post("/schedules", json=_rule_request()).json() == weekday

    weekend = _rule_doc(
        2, name="Weekend mop", days=("sunday",), at="11:00", mode="mop",
        segment_ids=None,  # whole house
        next_fires_at="2026-08-23T01:00:00+00:00",  # Sun 11:00 AEST
    )
    assert client.post(
        "/schedules",
        json=_rule_request(
            name="Weekend mop",
            recurrence={"days": ["sunday"], "at": "11:00"},
            action={"type": "clean", "mode": "mop", "segment_ids": None},
        ),
    ).json() == weekend
    assert client.get("/schedules").json() == [weekday, weekend]

    paused = _rule_doc(1, name="Paused mornings", enabled=False)  # disabled: no next firing
    assert client.put(
        "/schedules/1", json=_rule_request(name="Paused mornings", enabled=False)
    ).json() == paused
    assert client.get("/schedules").json() == [paused, weekend]

    assert client.delete("/schedules/1").json() == {"ok": True}
    assert client.get("/schedules").json() == [weekend]


def test_schedule_names_are_labels_so_duplicates_are_allowed(client, clock):
    client.post("/schedules", json=_rule_request())
    client.post("/schedules", json=_rule_request())
    docs = client.get("/schedules").json()
    assert [(doc["id"], doc["name"]) for doc in docs] == [
        (1, "Weekday mornings"),
        (2, "Weekday mornings"),
    ]


def test_schedule_with_no_days_is_422(client):
    r = client.post("/schedules", json=_rule_request(recurrence={"days": [], "at": "09:30"}))
    assert r.status_code == 422
    assert r.json() == {"detail": "a Schedule Rule needs at least one day; disable it to pause it"}
    assert client.get("/schedules").json() == []


def test_schedule_with_an_empty_segment_list_is_422(client):
    r = client.post(
        "/schedules",
        json=_rule_request(action={"type": "clean", "mode": "vacuum", "segment_ids": []}),
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "an empty Segment list is ambiguous; use null for the whole house"}


def test_schedule_with_an_invalid_weekday_is_422(client):
    r = client.post(
        "/schedules", json=_rule_request(recurrence={"days": ["funday"], "at": "09:30"})
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "invalid value 'funday' for Weekday"}


def test_schedule_with_an_invalid_time_is_422(client):
    r = client.post(
        "/schedules", json=_rule_request(recurrence={"days": ["monday"], "at": "25:99"})
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "invalid time '25:99' for recurrence.at: expected HH:MM"}


@pytest.mark.parametrize("at", ["09:30:45+05:00", "09:30+00:00", "09:30:00", "0930"])
def test_schedule_with_a_time_outside_the_hh_mm_contract_is_422(client, at):
    # A UTC offset would be discarded during generation while the response echoed it
    # back — the Rule would silently run hours off. Seconds and compact forms fail the
    # same canonical HH:MM check.
    r = client.post("/schedules", json=_rule_request(recurrence={"days": ["monday"], "at": at}))
    assert r.status_code == 422
    assert r.json() == {"detail": f"invalid time {at!r} for recurrence.at: expected HH:MM"}
    assert client.get("/schedules").json() == []


def test_schedule_with_an_invalid_mode_is_422(client):
    r = client.post(
        "/schedules",
        json=_rule_request(action={"type": "clean", "mode": "polish", "segment_ids": None}),
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "invalid value 'polish' for CleaningMode"}


def test_schedule_with_an_unknown_action_type_is_422(client):
    r = client.post(
        "/schedules",
        json=_rule_request(action={"type": "mop_wash", "mode": "vacuum", "segment_ids": None}),
    )
    assert r.status_code == 422
    assert r.json() == {
        "detail": "invalid action type 'mop_wash': expected 'clean' or 'sequence'"
    }


def test_replacing_or_deleting_a_missing_schedule_is_404(client):
    assert client.put("/schedules/99", json=_rule_request()).status_code == 404
    assert client.delete("/schedules/99").status_code == 404


def test_a_deleted_schedule_stays_deleted(client):
    client.post("/schedules", json=_rule_request())
    assert client.delete("/schedules/1").json() == {"ok": True}
    assert client.delete("/schedules/1").status_code == 404
    assert client.put("/schedules/1", json=_rule_request()).status_code == 404


def test_overdue_loop_end_to_end(client, clock):
    """Set an Interval, watch the room go Overdue, clean it, watch the Priority drop."""
    started = clock["now"]
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    assert client.get("/overdue").json() == [_overdue(16, days=2.0)]

    assert client.post("/clean/overdue", json={"mode": "vacuum"}).json() == {
        "ok": True,
        "segment_ids": [16],
    }
    assert client.get("/overdue").json() == [
        _overdue(16, days=2.0, cleaned_at=started, priority=0.0)
    ]

    clock["now"] = started + timedelta(days=3)
    assert client.get("/overdue").json() == [
        _overdue(16, days=2.0, cleaned_at=started, priority=1.5)
    ]


def _occ_doc(id, rule_id, scheduled_for, *, rule_name="Weekday mornings", state="pending"):
    scheduled = datetime.fromisoformat(scheduled_for)
    return {
        "id": id,
        "rule_id": rule_id,
        "rule_name": rule_name,
        "scheduled_for": scheduled.isoformat(),
        "deadline": (scheduled + timedelta(hours=6)).isoformat(),  # the six-hour default
        "state": state,
        "attempts": 0,
        "blocked_reason": None,
        "fired_at": None,
    }


# The weekday-mornings Rule (Mon/Wed/Fri 09:30 Sydney) materialised from Thu 2026-08-20
# 19:00 Sydney over the 14-day horizon: 09:30 AEST is 23:30 UTC the previous day.
_WEEKDAY_INSTANTS = (
    "2026-08-20T23:30:00+00:00",  # Fri 21st
    "2026-08-23T23:30:00+00:00",  # Mon 24th
    "2026-08-25T23:30:00+00:00",  # Wed 26th
    "2026-08-27T23:30:00+00:00",  # Fri 28th
    "2026-08-30T23:30:00+00:00",  # Mon 31st
    "2026-09-01T23:30:00+00:00",  # Wed 2nd
)


def test_creating_a_rule_materialises_a_fortnight_of_pending_occurrences(client, clock):
    client.post("/schedules", json=_rule_request())
    assert client.get("/occurrences").json() == [
        _occ_doc(id, 1, at) for id, at in enumerate(_WEEKDAY_INSTANTS, start=1)
    ]


def test_materialising_again_leaves_existing_occurrences_untouched(client, clock):
    """Creating a second Rule re-runs Materialisation over every Rule; the first Rule's
    Occurrences keep their identities and gain no duplicates."""
    client.post("/schedules", json=_rule_request())
    client.post(
        "/schedules",
        json=_rule_request(
            name="Weekend mop",
            recurrence={"days": ["sunday"], "at": "11:00"},
            action={"type": "clean", "mode": "mop", "segment_ids": None},
        ),
    )
    sunday = [
        _occ_doc(7, 2, "2026-08-23T01:00:00+00:00", rule_name="Weekend mop"),
        _occ_doc(8, 2, "2026-08-30T01:00:00+00:00", rule_name="Weekend mop"),
    ]
    weekday = [_occ_doc(id, 1, at) for id, at in enumerate(_WEEKDAY_INSTANTS, start=1)]
    assert client.get("/occurrences").json() == [
        weekday[0], sunday[0], weekday[1], weekday[2], weekday[3],
        sunday[1], weekday[4], weekday[5],
    ]


def test_a_disabled_rule_materialises_nothing(client, clock):
    client.post("/schedules", json=_rule_request(enabled=False))
    assert client.get("/occurrences").json() == []


def test_disabling_a_rule_cancels_every_pending_occurrence_even_past_ones(client, clock):
    client.post("/schedules", json=_rule_request())
    clock["now"] += timedelta(days=2)  # the first instant is now in the past
    client.put("/schedules/1", json=_rule_request(enabled=False))
    assert client.get("/occurrences").json() == [
        _occ_doc(id, 1, at, state="cancelled")
        for id, at in enumerate(_WEEKDAY_INSTANTS, start=1)
    ]


def test_editing_a_rule_cancels_its_occurrences_and_materialises_the_new_ones(client, clock):
    """Restating the intent supersedes the old one: the 09:30 firings become CANCELLED
    rows — history, not a gap — and the 10:00 firings (00:00 UTC) appear beside them."""
    client.post("/schedules", json=_rule_request())
    client.put("/schedules/1", json=_rule_request(recurrence={
        "days": ["friday", "monday", "wednesday"], "at": "10:00",
    }))
    replacements = (
        "2026-08-21T00:00:00+00:00",
        "2026-08-24T00:00:00+00:00",
        "2026-08-26T00:00:00+00:00",
        "2026-08-28T00:00:00+00:00",
        "2026-08-31T00:00:00+00:00",
        "2026-09-02T00:00:00+00:00",
    )
    expected = []
    for id, (old, new) in enumerate(zip(_WEEKDAY_INSTANTS, replacements), start=1):
        expected.append(_occ_doc(id, 1, old, state="cancelled"))
        expected.append(_occ_doc(id + 6, 1, new))
    assert client.get("/occurrences").json() == expected


def test_a_rename_only_edit_still_supersedes_its_occurrences(client, clock):
    """A cancelled row frees its (Rule, instant) key, so an edit that keeps the same
    Recurrence re-materialises fresh PENDING rows at the very same instants."""
    client.post("/schedules", json=_rule_request())
    client.put("/schedules/1", json=_rule_request(name="Renamed"))
    expected = []
    for id, at in enumerate(_WEEKDAY_INSTANTS, start=1):
        expected.append(_occ_doc(id, 1, at, rule_name="Renamed", state="cancelled"))
        expected.append(_occ_doc(id + 6, 1, at, rule_name="Renamed"))
    assert client.get("/occurrences").json() == expected


def test_an_already_claimed_occurrence_rides_out_a_rule_change(client, clock):
    client.post("/schedules", json=_rule_request())
    assert asyncio.run(client.app.state.occurrences.claim(1)) is True
    client.put("/schedules/1", json=_rule_request(enabled=False))
    assert client.get("/occurrences").json() == [
        _occ_doc(1, 1, _WEEKDAY_INSTANTS[0], state="claimed"),
        *(
            _occ_doc(id, 1, at, state="cancelled")
            for id, at in enumerate(_WEEKDAY_INSTANTS[1:], start=2)
        ),
    ]


def test_deleting_a_rule_cancels_pending_occurrences_and_keeps_the_history(client, clock):
    client.post("/schedules", json=_rule_request())
    assert client.delete("/schedules/1").json() == {"ok": True}
    assert client.get("/occurrences").json() == [
        _occ_doc(id, 1, at, state="cancelled")
        for id, at in enumerate(_WEEKDAY_INSTANTS, start=1)
    ]


def test_scheduler_status_before_the_first_tick(client):
    # The loop sleeps one interval before its first tick, so at startup nothing has run.
    assert client.get("/scheduler/status").json() == {
        "last_tick_at": None,
        "consecutive_errors": 0,
        "current_backoff_seconds": 30.0,
        "pending_count": 0,
        "next_scheduled_at": None,
        "timezone": "Australia/Sydney",
    }


def test_scheduler_status_reports_the_tick_and_the_next_firing(client, clock):
    client.post("/schedules", json=_rule_request())
    asyncio.run(client.app.state.scheduler.tick_safely())
    assert client.get("/scheduler/status").json() == {
        "last_tick_at": "2026-08-20T09:00:00+00:00",
        "consecutive_errors": 0,
        "current_backoff_seconds": 30.0,
        "pending_count": 6,
        "next_scheduled_at": "2026-08-20T23:30:00+00:00",
        "timezone": "Australia/Sydney",
    }


def test_a_due_rule_fires_through_the_scheduler_with_no_user_action(client, clock):
    client.post("/schedules", json=_rule_request())
    clock["now"] = datetime.fromisoformat(_WEEKDAY_INSTANTS[0])
    asyncio.run(client.app.state.scheduler.tick_safely())
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16, 17)),
    ]
    fired = _occ_doc(1, 1, _WEEKDAY_INSTANTS[0])
    fired.update(state="fired", fired_at=_WEEKDAY_INSTANTS[0])
    assert client.get("/occurrences").json() == [
        fired,
        *(_occ_doc(id, 1, at) for id, at in enumerate(_WEEKDAY_INSTANTS[1:], start=2)),
    ]
    assert _runs(client) == [
        CleaningRun(
            source=CleanSource.SCHEDULER,
            mode=CleaningMode.VACUUM,
            segment_ids=(16, 17),
            started_at=datetime.fromisoformat(_WEEKDAY_INSTANTS[0]),
        )
    ]


def test_scheduling_end_to_end_interval_to_scheduler_fire_in_fake_mode(client, clock):
    """P17: the whole feature working together — a Room Interval goes Overdue, a
    Schedule Rule due shortly Materialises an Occurrence, the scheduler fires it with no
    user action, and the Overdue Priority drops — visible in the timeline and the
    scheduler status too."""
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    assert client.get("/overdue").json() == [_overdue(16, days=2.0)]

    client.post("/schedules", json=_rule_request())
    due_at = _WEEKDAY_INSTANTS[0]
    assert client.get("/occurrences").json() == [
        _occ_doc(id, 1, at) for id, at in enumerate(_WEEKDAY_INSTANTS, start=1)
    ]

    clock["now"] = datetime.fromisoformat(due_at)
    asyncio.run(client.app.state.scheduler.tick_safely())

    fired = _occ_doc(1, 1, due_at)
    fired.update(state="fired", fired_at=due_at)
    assert client.get("/occurrences").json() == [
        fired,
        *(_occ_doc(id, 1, at) for id, at in enumerate(_WEEKDAY_INSTANTS[1:], start=2)),
    ]
    assert _runs(client) == [
        CleaningRun(
            source=CleanSource.SCHEDULER,
            mode=CleaningMode.VACUUM,
            segment_ids=(16, 17),
            started_at=datetime.fromisoformat(due_at),
        )
    ]
    assert client.get("/overdue").json() == [
        _overdue(16, days=2.0, cleaned_at=datetime.fromisoformat(due_at), priority=0.0)
    ]
    assert client.get("/scheduler/status").json() == {
        "last_tick_at": due_at,
        "consecutive_errors": 0,
        "current_backoff_seconds": 30.0,
        "pending_count": 5,
        "next_scheduled_at": _WEEKDAY_INSTANTS[1],
        "timezone": "Australia/Sydney",
    }


# --- PWA serving: root-scoped service worker, manifest, icons ---


def test_service_worker_served_from_root_with_root_scope(client):
    """The worker MUST come from /sw.js (not the static mount): a service worker only
    controls pages at or below its own path, so root serving is what makes its scope /.
    The Service-Worker-Allowed header pins the widened scope explicitly."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["service-worker-allowed"] == "/"
    assert 'self.addEventListener("fetch"' in r.text


def test_manifest_served_with_manifest_content_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = r.json()
    assert body["start_url"] == "/"
    assert body["scope"] == "/"
    assert body["display"] == "standalone"
    assert [icon["src"] for icon in body["icons"]] == [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-maskable-512.png",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-maskable-512.png",
        "/static/icons/apple-touch-icon.png",
    ],
)
def test_icons_served_as_png(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


# --- Sequences: CRUD, run, cancel, progress, and the Sequence Action ---

_BATHROOMS_THEN_HOUSE = {
    "name": "Bathrooms then house",
    "steps": [
        {"mode": "vacuum", "carpet": "rise", "segment_ids": [16, 17]},
        {"mode": "vac_and_mop", "carpet": "avoid", "segment_ids": None},
    ],
}
_TWO_STAGE = {
    "name": "Two-stage",
    "steps": [
        {"mode": "vacuum", "carpet": "rise", "segment_ids": None},
        {"mode": "mop", "carpet": "avoid", "segment_ids": None},
    ],
}


def _sequence_doc(id, body) -> dict:
    return {"id": id, **body}


def test_sequence_crud_snapshot(client):
    assert client.get("/sequences").json() == []
    assert client.post("/sequences", json=_BATHROOMS_THEN_HOUSE).json() == _sequence_doc(
        1, _BATHROOMS_THEN_HOUSE
    )
    assert client.post("/sequences", json=_TWO_STAGE).json() == _sequence_doc(2, _TWO_STAGE)
    assert client.get("/sequences").json() == [
        _sequence_doc(1, _BATHROOMS_THEN_HOUSE),
        _sequence_doc(2, _TWO_STAGE),
    ]
    assert client.get("/sequences/2").json() == _sequence_doc(2, _TWO_STAGE)

    reordered = {"name": "Reordered", "steps": _BATHROOMS_THEN_HOUSE["steps"][::-1]}
    assert client.put("/sequences/1", json=reordered).json() == _sequence_doc(1, reordered)
    assert client.get("/sequences/1").json() == _sequence_doc(1, reordered)

    assert client.delete("/sequences/1").json() == {"ok": True}
    assert client.get("/sequences").json() == [_sequence_doc(2, _TWO_STAGE)]


def test_a_missing_sequence_is_404_everywhere(client):
    for response in (
        client.get("/sequences/9"),
        client.put("/sequences/9", json=_TWO_STAGE),
        client.delete("/sequences/9"),
        client.post("/sequences/9/run"),
    ):
        assert (response.status_code, response.json()) == (404, {"detail": "no Sequence 9"})


def _step(**over) -> dict:
    base = {"mode": "vacuum", "carpet": "rise", "segment_ids": None}
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"name": "Empty", "steps": []}, "a Sequence needs at least one Step"),
        ({"name": "  ", "steps": [_step()]}, "a Sequence needs a name"),
        ({"name": 7, "steps": [_step()]}, "a Sequence needs a name"),
        ({"name": "x", "steps": "vacuum"}, "steps must be a list of Steps"),
        ({"name": "x", "steps": 5}, "steps must be a list of Steps"),
        ({"name": "x", "steps": ["vacuum"]}, "steps must be a list of Steps"),
        (
            {"name": "x", "steps": [_step(segment_ids=[])]},
            "an empty Segment list is ambiguous; use null for the whole house",
        ),
        (
            {"name": "x", "steps": [_step(segment_ids=["kitchen"])]},
            "a Step's segment_ids must be a list of Segment numbers or null",
        ),
        ({"name": "x", "steps": [_step(mode="polish")]}, "invalid value 'polish' for CleaningMode"),
        ({"name": "x", "steps": [_step(carpet="shag")]}, "invalid value 'shag' for CarpetMode"),
        (
            {"name": "x", "steps": [_step(carpet="unknown")]},
            "a Step's carpet mode must be rise or avoid",
        ),
    ],
)
def test_an_invalid_sequence_is_422_on_create_and_replace(client, body, detail):
    created = client.post("/sequences", json=body)
    assert (created.status_code, created.json()) == (422, {"detail": detail})
    client.post("/sequences", json=_TWO_STAGE)
    replaced = client.put("/sequences/1", json=body)
    assert (replaced.status_code, replaced.json()) == (422, {"detail": detail})
    assert client.get("/sequences").json() == [_sequence_doc(1, _TWO_STAGE)]


_IDLE_PROGRESS = {"running": False, "stage": "idle", "message": "", "error": None}


def _wait_for_progress_idle(client) -> dict:
    deadline = time.monotonic() + 5.0
    progress = client.get("/sequences/progress").json()
    while progress["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
        progress = client.get("/sequences/progress").json()
    return progress


def test_run_then_cancel_recovers_the_robot_and_reports_the_step_it_stopped_at(client):
    assert client.get("/sequences/progress").json() == _IDLE_PROGRESS
    client.post("/sequences", json=_BATHROOMS_THEN_HOUSE)
    assert client.post("/sequences/1/run").json() == {"ok": True}
    running = client.get("/sequences/progress").json()
    assert (running["running"], running["stage"], running["step"]) == (True, "running", 0)
    assert client.post("/sequences/cancel").json() == {"ok": True}
    assert _wait_for_progress_idle(client) == {
        "running": False,
        "stage": "cancelled",
        "message": "Sequence cancelled",
        "error": None,
        "sequence_id": 1,
        "sequence_name": "Bathrooms then house",
        "source": "app",
        "step": 0,
        "step_count": 2,
        "phase": "departing",
    }
    assert client.app.state.gateway.commands == [
        ("connect",),
        ("set_carpet_mode", CarpetMode.RISE),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16, 17)),
        ("dock",),
        ("set_carpet_mode", CarpetMode.AVOID),
    ]
    assert _runs(client) == []


def test_a_sequence_run_is_refused_while_any_run_holds_the_slot(client):
    client.post("/sequences", json=_TWO_STAGE)
    assert client.post("/clean/two-stage").json() == {"ok": True}
    r = client.post("/sequences/1/run")
    assert (r.status_code, r.json()) == (
        409, {"detail": "another run is already in progress"}
    )
    client.post("/sequences/cancel")
    _wait_for_progress_idle(client)
    assert client.post("/sequences/1/run").json() == {"ok": True}
    # Both entry points share one slot, so the Two-Stage Clean is refused in turn.
    r = client.post("/clean/two-stage")
    assert (r.status_code, r.json()) == (409, {"detail": "another run is already in progress"})


def test_a_sequence_run_is_refused_when_the_robot_is_not_ready_and_frees_the_slot(client):
    client.post("/sequences", json=_TWO_STAGE)
    client.app.state.gateway.set_timeline(
        [(0.0, DeviceStatus("cleaning", 90, in_cleaning=True, docked=False, cleaning=True))]
    )
    r = client.post("/sequences/1/run")
    assert (r.status_code, r.json()) == (409, {"detail": "the robot is mid-job"})
    assert client.get("/sequences/progress").json() == _IDLE_PROGRESS
    assert client.app.state.gateway.commands == [("connect",)]


def test_cancel_with_nothing_running_is_409(client):
    r = client.post("/sequences/cancel")
    assert (r.status_code, r.json()) == (409, {"detail": "nothing is running"})


def test_the_two_stage_status_keeps_its_shape_after_a_sequence_run(client):
    client.post("/sequences", json=_BATHROOMS_THEN_HOUSE)
    client.post("/sequences/1/run")
    client.post("/sequences/cancel")
    _wait_for_progress_idle(client)
    client.post("/clean/two-stage")
    assert set(client.get("/clean/two-stage/status").json()) == {
        "running", "stage", "message", "error"
    }


def _sequence_rule(sequence_id=1, **over) -> dict:
    return _rule_request(
        name="Saturday deep clean",
        action={"type": "sequence", "sequence_id": sequence_id},
        **over,
    )


def test_schedule_rules_accept_and_return_a_sequence_action(client, clock):
    client.post("/sequences", json=_BATHROOMS_THEN_HOUSE)
    client.post("/sequences", json=_TWO_STAGE)
    expected = {
        "id": 1,
        "name": "Saturday deep clean",
        "enabled": True,
        "recurrence": {"days": ["monday", "wednesday", "friday"], "at": "09:30"},
        "action": {"type": "sequence", "sequence_id": 1},
        "next_fires_at": "2026-08-20T23:30:00+00:00",
    }
    assert client.post("/schedules", json=_sequence_rule()).json() == expected
    assert client.get("/schedules").json() == [expected]
    moved = {**expected, "action": {"type": "sequence", "sequence_id": 2}}
    assert client.put("/schedules/1", json=_sequence_rule(2)).json() == moved
    assert client.get("/schedules").json() == [moved]


@pytest.mark.parametrize(
    ("action", "detail"),
    [
        ({"type": "sequence", "sequence_id": 9}, "no Sequence 9"),
        ({"type": "sequence", "sequence_id": "1"}, "action.sequence_id must be a Sequence id"),
        ({"type": "sequence"}, "action.sequence_id must be a Sequence id"),
    ],
)
def test_a_sequence_action_must_name_an_existing_sequence(client, action, detail):
    client.post("/sequences", json=_TWO_STAGE)
    r = client.post("/schedules", json=_rule_request(action=action))
    assert (r.status_code, r.json()) == (422, {"detail": detail})
    assert client.get("/schedules").json() == []


def test_deleting_a_sequence_a_rule_fires_is_409_naming_the_rule(client):
    client.post("/sequences", json=_TWO_STAGE)
    client.post("/schedules", json=_sequence_rule())
    r = client.delete("/sequences/1")
    assert (r.status_code, r.json()) == (
        409, {"detail": "Schedule Rule 'Saturday deep clean' runs this Sequence. Change or delete that rule first."}
    )
    assert client.get("/sequences").json() == [_sequence_doc(1, _TWO_STAGE)]
    client.delete("/schedules/1")  # a deleted Rule no longer holds the Sequence
    assert client.delete("/sequences/1").json() == {"ok": True}


def test_shutdown_cancels_a_run_the_scheduler_started(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")

    class WitnessRobot(FakeRobot):
        """Notes, as the gateway closes, whether shutdown has asked the run to stop:
        event-loop teardown would cancel it too, but only after the lifespan is over."""

        async def close(self) -> None:
            self.run_cancelled_before_close = app.state.run_slot.task.cancelling() > 0
            await super().close()

    monkeypatch.setattr(server, "get_gateway", lambda _config: WitnessRobot())
    with TestClient(app) as client:
        clock = {"now": datetime(2026, 8, 20, 9, 0, tzinfo=UTC)}
        client.app.state.now = lambda: clock["now"]
        client.post("/sequences", json=_TWO_STAGE)
        client.post("/schedules", json=_sequence_rule())
        clock["now"] = datetime.fromisoformat("2026-08-20T23:30:00+00:00")
        client.portal.call(client.app.state.scheduler.tick)
        task = client.app.state.run_slot.task
        assert client.get("/sequences/progress").json()["source"] == "scheduler"
        assert not task.done()
        robot = client.app.state.gateway
    assert robot.run_cancelled_before_close is True
    assert task.cancelled()


def test_a_readiness_check_that_raises_still_frees_the_slot(client):
    client.post("/sequences", json=_TWO_STAGE)

    async def lost_contact():
        raise RuntimeError("lost contact")

    client.app.state.gateway.get_status = lost_contact
    with pytest.raises(RuntimeError, match="lost contact"):
        client.post("/sequences/1/run")
    assert client.app.state.run_slot.running is False


def test_overdue_clean_holds_the_run_slot_across_check_and_start_then_frees_it(client, clock):
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    gw, slot = client.app.state.gateway, client.app.state.run_slot
    held: list[tuple[str, bool]] = []
    get_status, clean_rooms = gw.get_status, gw.clean_rooms

    async def witness_status():
        held.append(("get_status", slot.running))
        return await get_status()

    async def witness_clean(segment_ids, repeat=1):
        held.append(("clean_rooms", slot.running))
        await clean_rooms(segment_ids, repeat)

    gw.get_status, gw.clean_rooms = witness_status, witness_clean
    assert client.post("/clean/overdue", json={"mode": "vacuum"}).json() == {
        "ok": True, "segment_ids": [16]
    }
    assert held == [("get_status", True), ("get_status", True), ("clean_rooms", True)]
    assert slot.running is False


def test_overdue_clean_frees_the_run_slot_when_the_robot_is_not_ready(client):
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    client.app.state.gateway.set_timeline(
        [(0.0, DeviceStatus("cleaning", 90, in_cleaning=True, docked=False, cleaning=True))]
    )
    assert client.post("/clean/overdue", json={"mode": "vacuum"}).status_code == 409
    assert client.app.state.run_slot.running is False


def test_overdue_clean_is_409_while_any_run_holds_the_slot_and_leaves_it_held(client):
    client.post("/intervals", json={"segment_id": 16, "interval_days": 2})
    slot = client.app.state.run_slot
    assert slot.acquire() is True
    r = client.post("/clean/overdue", json={"mode": "vacuum"})
    assert (r.status_code, r.json()) == (409, {"detail": "another run is already in progress"})
    assert slot.running is True
    assert client.app.state.gateway.commands == [("connect",)]


@pytest.mark.parametrize("endpoint,payload", [
    ("/start", {"mode": "vacuum", "carpet_mode": "rise"}),
    ("/clean/segments", {"mode": "vacuum", "carpet_mode": "rise", "segment_ids": [16]}),
])
def test_start_applies_selected_carpet_and_rejects_second_clean(client, endpoint, payload):
    assert client.post(endpoint, json=payload).json() == {"ok": True}
    assert client.get("/carpet").json() == {"mode": "rise"}
    before = list(client.app.state.gateway.commands)
    response = client.post(endpoint, json=payload)
    assert response.status_code == 409
    assert client.app.state.gateway.commands == before


@pytest.mark.parametrize("endpoint,payload", [
    ("/start", {"mode": "vacuum"}),
    ("/clean/segments", {"mode": "vacuum", "segment_ids": [16]}),
    ("/carpet", {"mode": "rise"}),
    ("/water", {"mode": "standard"}),
    ("/fan", {"power": "max"}),
    ("/mop", {"mode": "deep"}),
])
def test_setting_or_start_cannot_interleave_with_held_start_slot(client, endpoint, payload):
    assert client.app.state.run_slot.acquire()
    try:
        response = client.post(endpoint, json=payload)
        assert (response.status_code, client.app.state.gateway.commands) == (409, [("connect",)])
    finally:
        client.app.state.run_slot.release()


def test_unconfirmed_mode_returns_actionable_conflict_and_does_not_start(client):
    from dataclasses import replace

    gateway = client.app.state.gateway
    original = gateway.get_status

    async def mismatched_status():
        return replace(await original(), cleaning_mode="vac_and_mop")

    gateway.get_status = mismatched_status
    response = client.post("/start", json={"mode": "vacuum"})
    assert (response.status_code, response.json(), gateway.commands, _runs(client)) == (
        409,
        {"detail": "Cleaning mode not confirmed: requested vacuum, robot reported vac_and_mop. Clean not started."},
        [("connect",), ("set_cleaning_mode", CleaningMode.VACUUM)], [],
    )


def test_start_verification_does_not_use_cached_display_status(client):
    from unittest.mock import AsyncMock

    gateway = client.app.state.gateway
    assert client.get('/status').json()['cleaning_mode'] == 'vac_and_mop'
    gateway.get_status = AsyncMock(return_value=DeviceStatus(
        'charging', 100, False, True, cleaning_mode='vac_and_mop',
    ))
    response = client.post('/start', json={'mode': 'vacuum'})
    assert (response.status_code, response.json(), gateway.get_status.await_count, gateway.commands) == (
        409,
        {'detail': 'Cleaning mode not confirmed: requested vacuum, robot reported vac_and_mop. Clean not started.'},
        2, [('connect',), ('set_cleaning_mode', CleaningMode.VACUUM)],
    )


def test_control_invalidates_status_even_within_display_cache_ttl(client):
    states = [client.get('/status').json()['in_cleaning']]
    assert client.post('/start', json={'mode': 'vacuum'}).status_code == 200
    states.append(client.get('/status').json()['in_cleaning'])
    assert client.post('/stop').status_code == 200
    states.append(client.get('/status').json()['in_cleaning'])
    assert states == [False, True, False]
