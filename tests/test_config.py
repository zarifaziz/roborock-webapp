"""Config parsing: empty env vars in fake mode, and required fields in live mode."""

from __future__ import annotations

import pytest

from roborock_auto.config import Config, ConfigError


def test_empty_env_vars_fall_back_to_defaults_in_fake_mode(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    for name in ("CARPET_RISE_VALUE", "CARPET_AVOID_VALUE", "APP_PORT"):
        monkeypatch.setenv(name, "")
    config = Config.from_env()
    assert config.gateway == "fake"
    assert config.app_port == 8000


def test_live_mode_requires_carpet_values(monkeypatch):
    monkeypatch.setenv("GATEWAY", "live")
    monkeypatch.setenv("ROBOROCK_EMAIL", "e@x.com")
    monkeypatch.setenv("ROBOROCK_DUID", "DUID1")
    monkeypatch.delenv("CARPET_RISE_VALUE", raising=False)
    with pytest.raises(ConfigError, match="CARPET_RISE_VALUE"):
        Config.from_env()


def test_live_mode_parses_when_all_present(monkeypatch):
    for name, value in {
        "GATEWAY": "live", "ROBOROCK_EMAIL": "e@x.com", "ROBOROCK_DUID": "DUID1",
        "CARPET_RISE_VALUE": "2", "CARPET_AVOID_VALUE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    config = Config.from_env()
    assert config.carpet_rise_value == 2
    assert config.carpet_avoid_value == 0


def test_database_path_defaults_beside_the_session_file(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert Config.from_env().database_path == "roborock.db"


def test_database_path_is_configurable(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("DATABASE_PATH", "/data/cleans.db")
    assert Config.from_env().database_path == "/data/cleans.db"
