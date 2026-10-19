from __future__ import annotations

import sys
from types import ModuleType

import pytest

from aap_identity.login_counter import RedisLoginCounter


class FakeRedis:
    def __init__(self, value=None, *, error=None):
        self.value = value
        self.error = error
        self.calls = []
        self.closed = False

    async def get(self, key):
        self.calls.append(("get", key))
        if self.error:
            raise self.error
        return self.value

    async def incr(self, key):
        self.calls.append(("incr", key))
        if self.error:
            raise self.error
        self.value = int(self.value or 0) + 1
        return self.value

    async def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))
        return True

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.value = None
        return 1

    async def aclose(self):
        self.closed = True


def install_fake_redis(monkeypatch, client, calls):
    package = ModuleType("redis")
    package.__path__ = []
    module = ModuleType("redis.asyncio")

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return client

    module.from_url = from_url
    monkeypatch.setitem(sys.modules, "redis", package)
    monkeypatch.setitem(sys.modules, "redis.asyncio", module)


@pytest.mark.asyncio
async def test_counter_keeps_shared_key_ttl_and_reset_behavior(monkeypatch):
    client = FakeRedis()
    from_url_calls = []
    install_fake_redis(monkeypatch, client, from_url_calls)
    counter = RedisLoginCounter("redis://:private@redis/0", window_seconds=90)

    assert await counter.get_count("192.0.2.5") == 0
    assert await counter.increment("192.0.2.5") == 1
    assert await counter.increment("192.0.2.5") == 2
    await counter.reset("192.0.2.5")

    assert client.calls == [
        ("get", "login_failed:192.0.2.5"),
        ("incr", "login_failed:192.0.2.5"),
        ("expire", "login_failed:192.0.2.5", 90),
        ("incr", "login_failed:192.0.2.5"),
        ("delete", "login_failed:192.0.2.5"),
    ]
    assert from_url_calls[0][1] == {
        "decode_responses": True,
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
    }
    assert client.closed


@pytest.mark.asyncio
async def test_counter_fails_open_and_logs_error_class_without_ip_or_exception_text(
    monkeypatch, caplog
):
    ip = "198.51.100.77"
    client = FakeRedis(error=RuntimeError("private redis URL with password"))
    install_fake_redis(monkeypatch, client, [])
    counter = RedisLoginCounter("redis://private", window_seconds=60)

    assert await counter.get_count(ip) == 0
    assert await counter.increment(ip) == 0
    await counter.reset(ip)
    assert "RuntimeError" in caplog.text
    assert ip not in caplog.text
    assert "private redis URL with password" not in caplog.text
    assert client.closed


@pytest.mark.asyncio
async def test_empty_ip_never_opens_redis_and_window_must_be_positive():
    counter = RedisLoginCounter("redis://unused")
    counter._client = lambda: (_ for _ in ()).throw(AssertionError("connected"))
    assert await counter.get_count("") == 0
    assert await counter.increment("") == 0
    await counter.reset("")
    with pytest.raises(ValueError):
        RedisLoginCounter("redis://unused", window_seconds=0)
