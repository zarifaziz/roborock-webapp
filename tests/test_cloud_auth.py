from types import SimpleNamespace
from typing import ClassVar

from roborock_auto.adapters.roborock_cloud import auth as auth_module
from roborock_auto.adapters.roborock_cloud.auth import RoborockCloudAuth


class FakeApiClient:
    """Records calls per instance; the real client ties a code to the instance that requested it."""

    instances: ClassVar[list["FakeApiClient"]] = []

    def __init__(self, username: str) -> None:
        self.username = username
        self.calls: list[tuple] = []
        FakeApiClient.instances.append(self)

    async def request_code_v4(self) -> None:
        self.calls.append(("request_code_v4",))

    async def code_login_v4(self, code: str) -> dict:
        self.calls.append(("code_login_v4", code))
        return {"user": self.username}


def _auth(monkeypatch, tmp_path):
    FakeApiClient.instances = []
    saved: list[tuple] = []
    monkeypatch.setattr(auth_module, "RoborockApiClient", FakeApiClient)
    monkeypatch.setattr(
        auth_module, "save_user_data", lambda data, path: saved.append((data, path))
    )
    return RoborockCloudAuth(SimpleNamespace(session_path=tmp_path / "user_data.json")), saved


async def test_code_is_submitted_on_the_client_that_requested_it(monkeypatch, tmp_path):
    auth, saved = _auth(monkeypatch, tmp_path)

    await auth.request_code("me@example.com")
    await auth.submit_code("me@example.com", "123456")

    assert [c.calls for c in FakeApiClient.instances] == [
        [("request_code_v4",), ("code_login_v4", "123456")]
    ]
    assert saved == [({"user": "me@example.com"}, tmp_path / "user_data.json")]


async def test_a_new_request_replaces_the_pending_client(monkeypatch, tmp_path):
    auth, _ = _auth(monkeypatch, tmp_path)

    await auth.request_code("me@example.com")
    await auth.request_code("me@example.com")
    await auth.submit_code("me@example.com", "123456")

    assert [c.calls for c in FakeApiClient.instances] == [
        [("request_code_v4",)],
        [("request_code_v4",), ("code_login_v4", "123456")],
    ]
