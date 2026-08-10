"""Shared session helpers used by login, discovery, the app, and the cron.

Only touches python-roborock for persistence types. The device/command surface lives in
LiveGateway.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborock.data.containers import UserData


def save_user_data(user_data: UserData, path: str) -> None:
    Path(path).write_text(json.dumps(user_data.as_dict(), indent=2))


def load_user_data(path: str) -> UserData:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"session file {path} not found; run `uv run python -m roborock_auto.login` first"
        )
    return UserData.from_dict(json.loads(p.read_text()))
