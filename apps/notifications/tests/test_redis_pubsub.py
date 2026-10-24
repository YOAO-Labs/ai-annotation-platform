from __future__ import annotations

import asyncio
import uuid

import pytest

from aap_notifications import redis_pubsub


class FakeRedis:
    def __init__(self, *, publish_error=None, close_error=None):
        self.publish_error = publish_error
        self.close_error = close_error
        self.published = []
        self.closed = 0

    async def publish(self, channel, message):
        self.published.append((channel, message))
        if self.publish_error is not None:
            raise self.publish_error

    async def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def _install_fake_redis(monkeypatch, client):
    urls = []

    def from_url(redis_url):
        urls.append(redis_url)
        return client

    monkeypatch.setattr(redis_pubsub, "_redis_from_url", from_url)
    return urls


def test_channel_for_keeps_the_websocket_channel_shape():
    user_id = uuid.UUID("60000000-0000-4000-8000-000000000003")

    assert redis_pubsub.channel_for(user_id) == f"notify:{user_id}"
    assert redis_pubsub.channel_for(str(user_id)) == f"notify:{user_id}"


@pytest.mark.asyncio
async def test_publish_json_preserves_default_json_encoding_and_closes(
    monkeypatch,
):
    client = FakeRedis()
    urls = _install_fake_redis(monkeypatch, client)
    user_id = uuid.UUID("60000000-0000-4000-8000-000000000003")

    await redis_pubsub.publish_json(
        "redis://cache/0",
        channel=redis_pubsub.channel_for(user_id),
        message={"text": "中文", "count": 2},
    )

    assert urls == ["redis://cache/0"]
    assert client.published == [
        (f"notify:{user_id}", '{"text": "\\u4e2d\\u6587", "count": 2}')
    ]
    assert client.closed == 1


@pytest.mark.asyncio
async def test_serialization_failure_still_closes_without_publishing(monkeypatch):
    client = FakeRedis()
    _install_fake_redis(monkeypatch, client)

    with pytest.raises(TypeError):
        await redis_pubsub.publish_json(
            "redis://cache/0",
            channel="notify:user",
            message={"not_json": object()},
        )

    assert client.published == []
    assert client.closed == 1


@pytest.mark.asyncio
async def test_transport_failure_still_closes_and_propagates(monkeypatch):
    error = RuntimeError("publish failed")
    client = FakeRedis(publish_error=error)
    _install_fake_redis(monkeypatch, client)

    with pytest.raises(RuntimeError) as raised:
        await redis_pubsub.publish_json(
            "redis://cache/0", channel="notify:user", message={"type": "event"}
        )

    assert raised.value is error
    assert client.closed == 1


@pytest.mark.asyncio
async def test_close_failure_keeps_existing_finally_error_precedence(monkeypatch):
    publish_error = RuntimeError("publish failed")
    close_error = ValueError("close failed")
    client = FakeRedis(publish_error=publish_error, close_error=close_error)
    _install_fake_redis(monkeypatch, client)

    with pytest.raises(ValueError) as raised:
        await redis_pubsub.publish_json(
            "redis://cache/0", channel="notify:user", message={"type": "event"}
        )

    assert raised.value is close_error
    assert client.closed == 1


@pytest.mark.asyncio
async def test_cancellation_still_closes_and_propagates(monkeypatch):
    client = FakeRedis(publish_error=asyncio.CancelledError())
    _install_fake_redis(monkeypatch, client)

    with pytest.raises(asyncio.CancelledError):
        await redis_pubsub.publish_json(
            "redis://cache/0", channel="notify:user", message={"type": "event"}
        )

    assert client.closed == 1
