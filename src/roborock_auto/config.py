"""Configuration from environment / .env.

Every account-specific field has a working default so fake mode needs nothing set. Going
live is only filling these in and setting GATEWAY=live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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
            gateway=os.getenv("GATEWAY", "fake").lower(),
            email=os.getenv("ROBOROCK_EMAIL", ""),
            duid=os.getenv("ROBOROCK_DUID", ""),
            vacuum_scene_id=int(os.getenv("VACUUM_SCENE_ID", "0")),
            mop_scene_id=int(os.getenv("MOP_SCENE_ID", "0")),
            carpet_rise_value=int(os.getenv("CARPET_RISE_VALUE", "0")),
            carpet_avoid_value=int(os.getenv("CARPET_AVOID_VALUE", "1")),
            poll_interval_sec=float(os.getenv("POLL_INTERVAL_SEC", "5")),
            app_port=int(os.getenv("APP_PORT", "8000")),
            session_path=os.getenv("SESSION_PATH", "user_data.json"),
        )
