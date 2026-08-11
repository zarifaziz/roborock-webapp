"""Configuration from environment / .env.

Every account-specific field has a working default so fake mode needs nothing set. Going
live is only filling these in and setting GATEWAY=live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str) -> str:
    """Env var, treating set-but-empty (VACUUM_SCENE_ID=) the same as unset."""
    value = os.getenv(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Config:
    gateway: str            # "fake" | "live"
    email: str
    duid: str
    vacuum_scene_id: int
    mop_scene_id: int
    carpet_rise_value: int  # firmware int for "Rise", discovered via discover.py
    carpet_avoid_value: int
    poll_interval_sec: float
    app_port: int
    session_path: str       # where login.py writes / live mode reads user_data.json

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            gateway=_get("GATEWAY", "fake").lower(),
            email=_get("ROBOROCK_EMAIL", ""),
            duid=_get("ROBOROCK_DUID", ""),
            vacuum_scene_id=int(_get("VACUUM_SCENE_ID", "0")),
            mop_scene_id=int(_get("MOP_SCENE_ID", "0")),
            carpet_rise_value=int(_get("CARPET_RISE_VALUE", "0")),
            carpet_avoid_value=int(_get("CARPET_AVOID_VALUE", "1")),
            poll_interval_sec=float(_get("POLL_INTERVAL_SEC", "5")),
            app_port=int(_get("APP_PORT", "8000")),
            session_path=_get("SESSION_PATH", "user_data.json"),
        )
