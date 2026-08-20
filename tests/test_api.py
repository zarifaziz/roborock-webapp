"""P4: API endpoints via TestClient against the fake gateway."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from roborock_auto.adapters.web.server import app
from roborock_auto.domain.models import (
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
    assert r.json() == {"detail": "a two-stage clean is already running"}


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
    assert r.json() == {"detail": "invalid action type 'mop_wash': expected 'clean'"}


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
