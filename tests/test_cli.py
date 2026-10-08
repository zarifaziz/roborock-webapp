"""Onboarding CLIs on a fresh live setup: only the account email is known yet.

The live cloud classes are swapped for the fakes at the wiring point, so the tests take
the real GATEWAY=live code path without touching the network.
"""

from __future__ import annotations

import pytest

from roborock_auto.adapters import roborock_cloud
from roborock_auto.adapters.cli import discover, login
from roborock_auto.adapters.fake import FakeAuth, FakeRobot


@pytest.fixture(autouse=True)
def fresh_live_setup(monkeypatch):
    """What a new user has before discover: email and timezone, no device values."""
    monkeypatch.setenv("GATEWAY", "live")
    monkeypatch.setenv("ROBOROCK_EMAIL", "new@user.com")
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")
    for name in ("ROBOROCK_DUID", "CARPET_RISE_VALUE", "CARPET_AVOID_VALUE", "SESSION_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def robot(monkeypatch) -> FakeRobot:
    robot = FakeRobot()
    monkeypatch.setattr(roborock_cloud, "RoborockCloudAdapter", lambda config: robot)
    return robot


async def test_login_on_a_fresh_setup_needs_only_the_email(monkeypatch, capsys):
    auth = FakeAuth()
    monkeypatch.setattr(roborock_cloud, "RoborockCloudAuth", lambda config: auth)
    monkeypatch.setattr("builtins.input", lambda prompt: "123456")

    await login.main()

    assert auth.calls == [
        ("request_code", "new@user.com"),
        ("submit_code", "new@user.com", "123456"),
    ]
    assert capsys.readouterr().out == (
        "Saved session to user_data.json. Do NOT commit this file.\n"
    )


async def test_discover_without_a_duid_lists_the_devices_on_the_account(robot, capsys):
    await discover.main()

    assert capsys.readouterr().out == (
        "gateway mode : live\n"
        "devices on this account:\n"
        "  fake-duid  Fake Roborock  roborock.vacuum.fake\n"
        "Set ROBOROCK_DUID in .env to your robot's duid and run discover again.\n"
    )
    assert robot.commands == [("close",)]


async def test_discover_with_a_duid_but_no_carpet_values_prints_the_snapshot(
    robot, monkeypatch, capsys
):
    monkeypatch.setenv("ROBOROCK_DUID", "fake-duid")

    await discover.main()

    assert capsys.readouterr().out == (
        "gateway mode : live\n"
        "status       : state=charging battery=100 docked=True in_cleaning=False\n"
        "carpet value : 1  (set carpet Rise, then Avoid, in the Roborock app and run "
        "discover after each to capture both values)\n"
        "rooms:\n"
        "    16  Living Room\n"
        "    17  Kitchen\n"
        "    18  Bedroom\n"
        "    19  Hallway\n"
        "    20  Study\n"
        "    21  Bathroom\n"
        "    22  Laundry\n"
        "routines:\n"
        "      1001  Vacuum whole house\n"
        "      1002  Mop whole house\n"
        "consumables  : brush=82% side=64% filter=71%\n"
    )
