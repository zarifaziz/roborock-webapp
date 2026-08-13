"""Driven auth adapter over python-roborock. Imports roborock.* — quarantined here.

Mirrors the one-time CLI login (cli/login.py) but split into the two web steps: request a
code, then submit it and persist the session via the shared session helper."""

from __future__ import annotations

from roborock.web_api import RoborockApiClient

from roborock_auto.adapters.roborock_cloud.session import save_user_data
from roborock_auto.config import Config


class RoborockCloudAuth:
    def __init__(self, config: Config) -> None:
        self._config = config

    async def request_code(self, email: str) -> None:
        client = RoborockApiClient(username=email)
        await client.request_code_v4()

    async def submit_code(self, email: str, code: str) -> None:
        client = RoborockApiClient(username=email)
        user_data = await client.code_login_v4(code)
        save_user_data(user_data, self._config.session_path)
