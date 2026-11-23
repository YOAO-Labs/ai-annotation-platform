from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1 import ws


@pytest.mark.parametrize(
    "failure", ["subscribe_cancel", "unsubscribe_error", "unsubscribe_cancel"]
)
async def test_notification_socket_releases_pubsub_after_partial_failure(
    monkeypatch, failure
):
    closed = False

    class PubSub:
        async def subscribe(self, _channel):
            if failure == "subscribe_cancel":
                raise asyncio.CancelledError

        async def unsubscribe(self, _channel):
            if failure == "unsubscribe_error":
                raise RuntimeError("unsubscribe failed")
            if failure == "unsubscribe_cancel":
                raise asyncio.CancelledError

        async def close(self):
            nonlocal closed
            closed = True

    actor = SimpleNamespace(actor_id=uuid4(), is_active=True)

    async def authenticate(*_args, **_kwargs):
        return actor

    async def no_stream(*_args, **_kwargs):
        return None

    class Socket:
        async def accept(self):
            return None

    monkeypatch.setattr(ws, "_authenticate_socket_token", authenticate)
    monkeypatch.setattr(ws, "_load_active_user", authenticate)
    monkeypatch.setattr(ws, "_get_redis_pool", lambda: object())
    monkeypatch.setattr(
        ws.aioredis, "Redis", lambda **_kwargs: SimpleNamespace(pubsub=PubSub)
    )
    monkeypatch.setattr(ws, "_run_pubsub_ws", no_stream)
    monkeypatch.setattr(ws.settings, "notification_publication_url", "")
    monkeypatch.setattr(ws.settings, "notification_subscribe_redis_url", "")
    monkeypatch.setattr(ws.settings, "notification_subscribe_redis_url_file", "")
    if failure in {"subscribe_cancel", "unsubscribe_cancel"}:
        with pytest.raises(asyncio.CancelledError):
            await ws.notifications_socket(Socket(), token="test")
    else:
        await ws.notifications_socket(Socket(), token="test")
    assert closed


@pytest.mark.parametrize("source", ["absent", "unreadable", "conflicting", "file"])
def test_isolated_socket_does_not_borrow_general_redis_credentials(
    monkeypatch, tmp_path, source
):
    subscriber = "redis://subscribe@notifications.test:6379/0"
    secret = tmp_path / "subscriber"
    if source == "file":
        secret.write_text(subscriber + "\n")
    monkeypatch.setattr(ws, "_NOTIFICATION_REDIS_POOL", None)
    monkeypatch.setattr(
        ws.settings, "notification_publication_url", "http://publisher.test"
    )
    monkeypatch.setattr(
        ws.settings,
        "notification_subscribe_redis_url",
        subscriber if source == "conflicting" else "",
    )
    monkeypatch.setattr(
        ws.settings,
        "notification_subscribe_redis_url_file",
        "" if source == "absent" else str(secret),
    )

    def fail_general_pool():
        pytest.fail("An isolated socket must not borrow the general Redis role")

    def create_pool(url, *, max_connections, decode_responses):
        assert (url, max_connections, decode_responses) == (subscriber, 200, False)
        return object()

    monkeypatch.setattr(ws, "_get_redis_pool", fail_general_pool)
    monkeypatch.setattr(ws.ConnectionPool, "from_url", create_pool)
    if source == "file":
        assert ws._get_notification_redis_pool() is ws._get_notification_redis_pool()
    else:
        with pytest.raises(ValueError):
            ws._get_notification_redis_pool()
