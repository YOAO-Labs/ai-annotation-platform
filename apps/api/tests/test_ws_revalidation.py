"""Pure tests for per-payload websocket reauthorization (B3).

No database or Redis: the relay must drop a protected message as soon as the
revalidation callback rejects, not after a grace interval.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import ws
from app.services.notification import NotificationService


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data) -> None:
        self.sent.append(data if isinstance(data, str) else data.decode())

    async def receive(self) -> dict:
        # Block forever; the relay completing ends the wait and cancels this.
        await asyncio.sleep(3600)
        return {"type": "websocket.disconnect"}


class _FakePubSub:
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages

    async def listen(self):
        for message in self._messages:
            yield {"type": "message", "data": message}


async def test_relay_revalidates_before_each_message():
    calls: list[int] = []

    async def revalidate() -> bool:
        calls.append(1)
        return len(calls) < 2  # allow first, reject second

    websocket = _FakeWebSocket()
    pubsub = _FakePubSub(["first", "second"])

    await asyncio.wait_for(
        ws._run_pubsub_ws(websocket, pubsub, heartbeat=False, revalidate=revalidate),
        timeout=5,
    )

    assert websocket.sent == ["first"]
    assert len(calls) == 2


async def test_relay_without_revalidate_forwards_all():
    websocket = _FakeWebSocket()
    pubsub = _FakePubSub(["a", "b"])

    await asyncio.wait_for(
        ws._run_pubsub_ws(websocket, pubsub, heartbeat=False), timeout=5
    )

    assert websocket.sent == ["a", "b"]


async def test_project_stream_rejects_when_credential_invalid(monkeypatch):
    async def invalid_credential(_token):
        return None

    monkeypatch.setattr(ws, "_authenticate_socket_token", invalid_credential)
    assert await ws._revalidate_project_stream("tok", None) is False


@pytest.fixture(autouse=True)
def _no_redis_pool(monkeypatch):
    # The relay test never touches Redis, but importing the module must not
    # construct a live pool.
    yield


async def test_notification_parser_fails_closed(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())

    async def fake_auth(_token):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)

    assert await ws._notification_message_allowed("t", b"not-json") is False
    assert await ws._notification_message_allowed("t", b"[1,2]") is False
    assert await ws._notification_message_allowed("t", b'{"type":"x"}') is False
    assert (
        await ws._notification_message_allowed(
            "t", b'{"type":"notifications.sync","reason":"read"}'
        )
        is True
    )


async def test_notification_parser_resolves_stored_row(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())

    async def fake_auth(_token):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)

    class _FakeDb:
        async def get(self, _model, _notification_id):
            return object()  # row is committed and visible

    class _Ctx:
        async def __aenter__(self):
            return _FakeDb()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.base.async_session", lambda: _Ctx())

    seen: dict = {}

    async def fake_deliverable(self, notification_id, user_id):
        seen["pair"] = (notification_id, user_id)
        return False

    monkeypatch.setattr(
        NotificationService, "notification_deliverable", fake_deliverable
    )
    notification_id = uuid.uuid4()
    allowed = await ws._notification_message_allowed(
        "t", json.dumps({"id": str(notification_id)})
    )
    assert allowed is False
    assert seen["pair"] == (notification_id, user.id)


async def test_notification_gate_retries_until_row_visible(monkeypatch):
    """A message published before its transaction commits is not dropped: the
    gate waits for the row to become visible instead of failing immediately."""

    user = SimpleNamespace(id=uuid.uuid4())
    notification_id = uuid.uuid4()

    async def fake_auth(_token):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)
    monkeypatch.setattr(ws, "_NOTIFICATION_GATE_RETRY_SECONDS", 0)

    class _FakeDb:
        def __init__(self) -> None:
            self.looks = 0

        async def get(self, _model, _notification_id):
            self.looks += 1
            return object() if self.looks >= 2 else None  # visible on 2nd look

    fake_db = _FakeDb()

    class _Ctx:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.base.async_session", lambda: _Ctx())

    async def fake_deliverable(self, _notification_id, _user_id):
        return True

    monkeypatch.setattr(
        NotificationService, "notification_deliverable", fake_deliverable
    )
    allowed = await ws._notification_message_allowed(
        "t", json.dumps({"id": str(notification_id)})
    )
    assert allowed is True
    assert fake_db.looks == 2


async def test_notification_gate_fails_closed_after_retry_window(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())

    async def fake_auth(_token):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)
    monkeypatch.setattr(ws, "_NOTIFICATION_GATE_RETRY_SECONDS", 0)

    class _FakeDb:
        async def get(self, _model, _notification_id):
            return None  # row never becomes visible

    class _Ctx:
        async def __aenter__(self):
            return _FakeDb()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.base.async_session", lambda: _Ctx())
    assert (
        await ws._notification_message_allowed(
            "t", json.dumps({"id": str(uuid.uuid4())})
        )
        is False
    )
