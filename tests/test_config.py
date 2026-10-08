"""Config parsing: empty env vars in fake mode, and required fields in live mode."""

from __future__ import annotations

import pytest

from roborock_auto.config import Config, ConfigError


@pytest.fixture(autouse=True)
def timezone_set(monkeypatch):
    """TIMEZONE is required in every mode; tests about other settings just provide it."""
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")


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


def test_cache_path_defaults_beside_the_session_file(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.delenv("CACHE_PATH", raising=False)
    assert Config.from_env().cache_path == "roborock_cache.pkl"


def test_cache_path_is_configurable(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("CACHE_PATH", "/data/roborock_cache.pkl")
    assert Config.from_env().cache_path == "/data/roborock_cache.pkl"


def test_timezone_is_required_in_every_mode_and_the_error_names_the_setting(monkeypatch):
    # "System local" is no default: it resolves to UTC inside a container and shifts
    # every schedule silently.
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.delenv("TIMEZONE", raising=False)
    with pytest.raises(ConfigError, match="TIMEZONE"):
        Config.from_env()
    monkeypatch.setenv("TIMEZONE", "  ")
    with pytest.raises(ConfigError, match="TIMEZONE"):
        Config.from_env()


def test_timezone_must_be_a_known_iana_zone(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("TIMEZONE", "Sydney")
    with pytest.raises(ConfigError, match="'Sydney' is not a known IANA zone name"):
        Config.from_env()


def test_timezone_parses(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    assert Config.from_env().timezone == "Australia/Sydney"


@pytest.mark.parametrize("enabled, model, expected", [
    ("", "", (False, "sonnet")),
    ("true", "claude-sonnet-5", (True, "claude-sonnet-5")),
    ("false", "sonnet", (False, "sonnet")),
])
def test_assistant_configuration(monkeypatch, enabled, model, expected):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("ASSISTANT_ENABLED", enabled)
    monkeypatch.setenv("ASSISTANT_MODEL", model)
    config = Config.from_env()
    assert (config.assistant_enabled, config.assistant_model) == expected
