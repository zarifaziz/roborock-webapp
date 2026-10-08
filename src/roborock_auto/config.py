"""Configuration from environment / .env.

Fake mode needs nothing set. Live mode requires the account fields plus the discovered
carpet integers: those drive a physical robot, so there is no safe default and we refuse to
run live without them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


def _get(name: str, default: str) -> str:
    """Env var, treating set-but-empty (CARPET_RISE_VALUE=) the same as unset."""
    value = os.getenv(name, "").strip()
    return value or default


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} must be set for live mode. Run "
            f"`uv run python -m roborock_auto.adapters.cli.login`, then "
            f"`uv run python -m roborock_auto.adapters.cli.discover`, and fill in .env "
            f"before running against the real robot."
        )
    return value


def _require_timezone() -> str:
    """The household's IANA timezone, required with no default in every mode: "system
    local" resolves to UTC inside a container and shifts every schedule silently."""
    name = os.getenv("TIMEZONE", "").strip()
    if not name:
        raise ConfigError(
            "TIMEZONE must be set to the household's IANA zone name "
            "(e.g. TIMEZONE=Australia/Sydney in .env). Refusing to guess: schedules "
            "would silently run hours off."
        )
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TIMEZONE {name!r} is not a known IANA zone name")
    return name


@dataclass(frozen=True)
class Config:
    gateway: str            # "fake" | "live"
    email: str
    duid: str
    carpet_rise_value: int  # firmware int for "Rise", discovered via discover.py
    carpet_avoid_value: int
    poll_interval_sec: float
    app_port: int
    session_path: str       # where login.py writes / live mode reads user_data.json
    database_path: str      # SQLite file recording what this app did (cleaning runs)
    timezone: str           # IANA zone name Schedule Rules are interpreted in; no default

    # The vendor library's device cache (pickled), including the robot's LAN address, so a
    # local reconnect does not depend on asking the cloud for it.
    cache_path: str = "roborock_cache.pkl"
    assistant_enabled: bool = False
    assistant_model: str = "sonnet"

    @classmethod
    def from_env(cls, *, require_device: bool = True) -> Config:
        """require_device=False is for the onboarding CLIs (login, discover), which run
        before the duid and carpet values are known. They never set the carpet mode, so
        the placeholder carpet values they get are never sent to the robot."""
        gateway = _get("GATEWAY", "fake").lower()
        live = gateway == "live"
        device_live = live and require_device
        # In live mode the carpet ints control mop-on-carpet behavior, so they are required,
        # not defaulted. A wrong value would wet a carpet.
        rise = _require("CARPET_RISE_VALUE") if device_live else _get("CARPET_RISE_VALUE", "0")
        avoid = _require("CARPET_AVOID_VALUE") if device_live else _get("CARPET_AVOID_VALUE", "1")
        return cls(
            gateway=gateway,
            email=_require("ROBOROCK_EMAIL") if live else _get("ROBOROCK_EMAIL", ""),
            duid=_require("ROBOROCK_DUID") if device_live else _get("ROBOROCK_DUID", ""),
            carpet_rise_value=int(rise),
            carpet_avoid_value=int(avoid),
            poll_interval_sec=float(_get("POLL_INTERVAL_SEC", "5")),
            app_port=int(_get("APP_PORT", "8000")),
            session_path=_get("SESSION_PATH", "user_data.json"),
            database_path=_get("DATABASE_PATH", "roborock.db"),
            timezone=_require_timezone(),
            cache_path=_get("CACHE_PATH", "roborock_cache.pkl"),
            assistant_enabled=_get("ASSISTANT_ENABLED", "false").lower() == "true",
            assistant_model=_get("ASSISTANT_MODEL", "sonnet"),
        )
