"""One-time interactive login. Run locally, once, to create user_data.json.

    uv run python -m roborock_auto.adapters.cli.login

Sends an email code, you type it back, and the resulting session is saved. Account only:
in fake mode there is nothing to log in to.
"""

from __future__ import annotations

import asyncio

from roborock.web_api import RoborockApiClient

from roborock_auto.adapters.roborock_cloud.session import save_user_data
from roborock_auto.config import Config


async def main() -> None:
    config = Config.from_env()
    if config.gateway != "live":
        print("GATEWAY is not 'live' — fake mode needs no login. Set GATEWAY=live to use this.")
        return

    email = config.email or input("Roborock account email: ").strip()
    client = RoborockApiClient(username=email)
    await client.request_code_v4()
    code = input("Enter the code emailed to you: ").strip()
    user_data = await client.code_login_v4(code)
    save_user_data(user_data, config.session_path)
    print(f"Saved session to {config.session_path}. Do NOT commit this file.")


if __name__ == "__main__":
    asyncio.run(main())
