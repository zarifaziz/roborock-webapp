"""P5: README.md and ARCHITECTURE.md stay consistent with the shipped app.

Regression coverage for two doc bugs: the fake-mode quickstart omitting the now-required
TIMEZONE setting (Config.from_env() raises ConfigError without it), and ARCHITECTURE.md
describing the shipped SCHEDULING context as still-future work.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _quickstart_code_block() -> str:
    readme = (REPO_ROOT / "README.md").read_text()
    match = re.search(
        r"## Quickstart \(fake mode, no account\)\n\n.*?```\n(.*?)```", readme, re.DOTALL
    )
    assert match, "README.md quickstart section not found"
    return match.group(1)


def test_readme_quickstart_sets_up_env_before_launching_the_app():
    expected = (
        "git clone https://github.com/zarifaziz/roborock-webapp.git\n"
        "cd roborock-webapp\n"
        "cp .env.example .env    # fake mode needs only TIMEZONE; set it to your IANA zone\n"
        "uv sync\n"
        "uv run uvicorn roborock_auto.adapters.web.server:app --host 127.0.0.1 --port 8000\n"
    )
    assert _quickstart_code_block() == expected


def test_architecture_does_not_defer_the_shipped_scheduling_context():
    architecture = (REPO_ROOT / "ARCHITECTURE.md").read_text()
    assert "SchedulePort" not in architecture
    assert "dock actions, schedules," not in architecture
