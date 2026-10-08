"""One-time interactive login. Run locally, once, to create user_data.json.

    uv run python -m roborock_auto.adapters.cli.login

Sends an email code, you type it back, and the resulting session is saved. Account only:
in fake mode there is nothing to log in to. Needs only ROBOROCK_EMAIL (and TIMEZONE), so it
runs before you know the duid or carpet values.
"""

from __future__ import annotations

import asyncio

from roborock_auto.adapters import get_auth
from roborock_auto.config import Config


async def main() -> None:
    config = Config.from_env(require_device=False)
    if config.gateway != "live":
        print("GATEWAY is not 'live' — fake mode needs no login. Set GATEWAY=live to use this.")
        return

    email = config.email or input("Roborock account email: ").strip()
    auth = get_auth(config)
    await auth.request_code(email)
    code = input("Enter the code emailed to you: ").strip()
    await auth.submit_code(email, code)
    print(f"Saved session to {config.session_path}. Do NOT commit this file.")


if __name__ == "__main__":
    asyncio.run(main())
