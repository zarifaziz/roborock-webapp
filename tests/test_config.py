"""Config parsing, including set-but-empty env vars (.env with blanks to fill later)."""

from __future__ import annotations

from roborock_auto.config import Config


def test_empty_env_vars_fall_back_to_defaults(monkeypatch):
    # A live .env with ids not yet filled in: GATEWAY set, numeric fields blank.
    monkeypatch.setenv("GATEWAY", "live")
    for name in ("VACUUM_SCENE_ID", "MOP_SCENE_ID", "CARPET_RISE_VALUE", "APP_PORT"):
        monkeypatch.setenv(name, "")
    config = Config.from_env()
    assert config.gateway == "live"
    assert config.vacuum_scene_id == 0
    assert config.app_port == 8000


def test_values_parse_when_set(monkeypatch):
    monkeypatch.setenv("VACUUM_SCENE_ID", "1001")
    monkeypatch.setenv("APP_PORT", "9000")
    config = Config.from_env()
    assert config.vacuum_scene_id == 1001
    assert config.app_port == 9000
