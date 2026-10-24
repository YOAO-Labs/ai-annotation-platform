from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace
import sys

import pytest

from aap_notifications.delivery_config import NotificationPublisherSettings
from aap_notifications.delivery_worker import _serve, run_forever
from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
)


def _settings(*, api_token=None):
    return NotificationPublisherSettings(
        _env_file=None,
        database_url="postgresql://publisher.test/notifications",
        redis_url="redis://publisher.test/0",
        api_token=api_token,
    )


@pytest.mark.asyncio
async def test_once_mode_ignores_optional_http_api_and_keeps_existing_output(
    monkeypatch, capsys
):
    calls = []

    async def run_once(settings):
        calls.append(settings)
        return {"claimed": 0, "published": 0, "retry": 0, "stale": 0, "unavailable": 0}

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("--once must not start the HTTP API")

    monkeypatch.setattr("aap_notifications.delivery_worker.run_once", run_once)
    monkeypatch.setattr("aap_notifications.delivery_worker.run_forever", forbidden)

    await _serve(_settings(api_token="service-token"), once=True)

    assert len(calls) == 1
    assert capsys.readouterr().out == (
        '{"claimed":0,"published":0,"retry":0,"stale":0,"unavailable":0}\n'
    )


@pytest.mark.asyncio
async def test_no_token_keeps_durable_only_run_forever(monkeypatch):
    calls = []

    async def run_forever(settings):
        calls.append(settings)

    monkeypatch.setattr("aap_notifications.delivery_worker.run_forever", run_forever)

    await _serve(_settings(), once=False)

    assert len(calls) == 1
    assert calls[0].api_token is None


@pytest.mark.asyncio
async def test_optional_api_runs_beside_durable_loop_and_closes_shared_redis(
    monkeypatch,
):
    captured = {}

    class Redis:
        async def aclose(self):
            captured["redis_closed"] = True

    class RedisFactory:
        @staticmethod
        def from_url(url, **kwargs):
            captured["redis_options"] = (url, kwargs)
            return Redis()

    class Config:
        def __init__(self, app, **kwargs):
            captured["app"] = app
            captured["server_options"] = kwargs

    class Server:
        def __init__(self, _config):
            self.should_exit = False

        async def serve(self):
            captured["server_started"] = True

    uvicorn = types.ModuleType("uvicorn")
    uvicorn.Config = Config
    uvicorn.Server = Server
    redis = types.ModuleType("redis")
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.Redis = RedisFactory
    redis.asyncio = redis_asyncio
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "redis", redis)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)

    async def run_forever(
        settings,
        *,
        data,
        redis,
        stop_event,
        ready_event,
    ):
        captured["durable_started"] = True
        assert settings.api_token.get_secret_value() == "service-token"
        assert data is not None and redis is not None
        ready_event.set()
        await stop_event.wait()

    monkeypatch.setattr("aap_notifications.delivery_worker.run_forever", run_forever)

    await _serve(_settings(api_token="service-token"), once=False)

    assert captured["durable_started"]
    assert captured["server_started"]
    assert captured["server_options"] == {
        "host": "127.0.0.1",
        "port": 8181,
        "access_log": False,
        "log_level": "warning",
    }
    assert captured["redis_closed"]


@pytest.mark.asyncio
async def test_run_forever_sets_readiness_after_policy_and_ping_then_clears_on_stop():
    ready = asyncio.Event()
    stop = asyncio.Event()
    order = []

    class Data:
        async def delivery_policy(self):
            order.append("policy")
            return SimpleNamespace(policy=NOTIFICATION_DELIVERY_POLICY)

        async def claim_deliveries(self, *_args, **_kwargs):
            assert ready.is_set()
            order.append("claim")
            stop.set()
            return []

    class Redis:
        async def ping(self):
            order.append("ping")

    await run_forever(
        _settings(),
        data=Data(),
        redis=Redis(),
        stop_event=stop,
        ready_event=ready,
    )

    assert order == ["policy", "ping", "claim"]
    assert not ready.is_set()
