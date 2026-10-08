"""The server's logging (`uvicorn --log-config scripts/uvicorn-logging.json`): every line
timestamped, and the vendor library's connection messages shown, so a lost robot connection
can be traced in the log."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
LOG_CONFIG = "scripts/uvicorn-logging.json"

# Applies the config the way `uvicorn --log-config` does, then logs one line per case.
_EMIT = f"""
import logging
import uvicorn

uvicorn.Config("roborock_auto.adapters.web.server:app", log_config={LOG_CONFIG!r})
logging.getLogger("uvicorn.error").info("Application startup complete.")
logging.getLogger("uvicorn.access").info(
    '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/status", "1.1", 200,
)
logging.getLogger("roborock.mqtt.roborock_session").info("MQTT session disconnected")
logging.getLogger("roborock.mqtt.roborock_session").debug("keepalive sent")
logging.getLogger("roborock_auto.adapters.roborock_cloud.adapter").warning("map fetch failed")
logging.getLogger("roborock_auto.two_stage").info("robot left the dock")
logging.getLogger("httpx").info("HTTP Request: GET https://example.invalid")
"""


def _without_timestamps(text: str) -> list[str]:
    return [
        re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ", "<time> ", line)
        for line in text.splitlines()
    ]


def test_server_log_lines_carry_timestamps_and_roborock_connection_messages():
    run = subprocess.run(
        [sys.executable, "-c", _EMIT], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert (_without_timestamps(run.stderr), _without_timestamps(run.stdout)) == (
        [
            "<time> INFO uvicorn.error: Application startup complete.",
            "<time> INFO roborock.mqtt.roborock_session: MQTT session disconnected",
            "<time> WARNING roborock_auto.adapters.roborock_cloud.adapter: map fetch failed",
            "<time> INFO roborock_auto.two_stage: robot left the dock",
        ],
        ['<time> INFO 127.0.0.1:50000 - "GET /status HTTP/1.1" 200 OK'],
    )
