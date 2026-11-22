import asyncio
import multiprocessing
import os
import secrets
import signal
import time
import uuid
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace
from unittest import TestCase

import pytest
import redis
import redis.asyncio as aioredis
from celery.contrib.testing.worker import start_worker
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app.api.v1 import ws
from app.config import settings
from app.services import ml_stats_presence as presence
from app.workers import ml_health
from app.workers.celery_app import celery_app

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_ML_STATS_ACCEPTANCE") != "1"
    or os.environ.get("YOAO_WORKTREE_MODE") != "test",
    reason="Requires explicitly enabled, owned Redis and Celery acceptance",
)

_LEASE_MS = 2_000
_RENEW_SECONDS = 0.15


@pytest.fixture
def isolated_presence(monkeypatch):
    url = make_url(os.environ["REDIS_URL"])
    assert os.environ.get("YOAO_WORKTREE_RESOURCE_ROOT")
    assert url.host == "127.0.0.1"
    assert make_url(settings.redis_url) == url
    assert make_url(settings.effective_celery_broker) == url
    key = f"ml-backend-stats:leases:acceptance:{uuid.uuid4()}"
    monkeypatch.setattr(presence, "_LEASES_KEY", key)
    monkeypatch.setattr(presence, "_LEASE_MILLISECONDS", _LEASE_MS)
    monkeypatch.setattr(presence, "_RENEW_SECONDS", _RENEW_SECONDS)
    return url, key


def _stats_socket_app(revoked: Event | None = None):
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await ws.close_redis_pool()

    actor_id = uuid.uuid4()

    async def authenticate(token, *, allow_api_key):
        assert allow_api_key
        if token not in {"jwt", "ak_stats"}:
            return None
        role = (
            "annotator" if revoked is not None and revoked.is_set() else "super_admin"
        )
        return SimpleNamespace(actor_id=actor_id, role=role)

    app = FastAPI(lifespan=lifespan)
    app.include_router(ws.router)
    return app, authenticate


def _subscribe_in_child(key: str):
    presence._LEASES_KEY = key
    presence._LEASE_MILLISECONDS = _LEASE_MS
    presence._RENEW_SECONDS = _RENEW_SECONDS
    app, ws._authenticate_socket_token = _stats_socket_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/ml-backend-stats?token=jwt") as socket,
    ):
        while True:
            socket.receive_text()


def _wait_for_count(redis_client, count: int):
    deadline = time.monotonic() + 15
    while True:
        observed = presence.count_live_stats_subscribers(redis_client)
        listeners = redis_client.pubsub_numsub(presence.ML_STATS_CHANNEL)[0][1]
        if observed == count and listeners >= count:
            return
        assert time.monotonic() < deadline, (count, observed)
        time.sleep(0.03)


def test_beat_and_stats_socket_converge_after_socket_process_is_killed(
    isolated_presence, monkeypatch
):
    url, key = isolated_presence
    revoked = Event()
    app, authenticate = _stats_socket_app(revoked)
    monkeypatch.setattr(ws, "_authenticate_socket_token", authenticate)
    monkeypatch.setattr(ws, "_REDIS_POOL", None)
    owner_calls = []

    @asynccontextmanager
    async def stats_session():
        yield object()

    async def snapshots():
        owner_calls.append("stats_snapshots")
        return ()

    monkeypatch.setattr(ml_health, "task_session", stats_session)
    monkeypatch.setattr(
        ml_health,
        "backend_control_service",
        lambda _db: SimpleNamespace(stats_snapshots=snapshots),
    )
    assert not celery_app.conf.task_always_eager
    timestamps = set()

    def take_snapshot(socket):
        message = socket.receive_json()
        assert message["backends"] == []
        timestamp = message["timestamp"]
        assert timestamp and timestamp not in timestamps
        timestamps.add(timestamp)

    def tick():
        return ml_health.publish_ml_backend_stats.delay().get(timeout=15)

    child = multiprocessing.get_context("spawn").Process(
        target=_subscribe_in_child, args=(key,)
    )
    with redis.from_url(url.render_as_string(hide_password=False)) as broker:
        child.start()
        try:
            _wait_for_count(broker, 1)
            with (
                start_worker(
                    celery_app,
                    pool="solo",
                    queues=("default",),
                    perform_ping_check=False,
                ),
                TestClient(app) as client,
                client.websocket_connect(
                    "/ws/ml-backend-stats?token=ak_stats"
                ) as socket,
            ):
                _wait_for_count(broker, 2)
                assert tick() == {"published": 0, "subscribers": 2}
                take_snapshot(socket)
                child.kill()
                child.join(timeout=5)
                assert child.exitcode == -signal.SIGKILL
                _wait_for_count(broker, 1)
                assert tick() == {"published": 0, "subscribers": 1}
                take_snapshot(socket)

                broker.set(key, "wrong-type", ex=30)
                with TestCase().assertLogs(ml_health.log, level="WARNING"):
                    assert tick() == {"skipped": True, "subscribers": 0}
                broker.delete(key)
                _wait_for_count(broker, 1)
                assert tick() == {"published": 0, "subscribers": 1}
                take_snapshot(socket)

                revoked.set()
                assert tick() == {"published": 0, "subscribers": 1}
                _wait_for_count(broker, 0)
                assert tick() == {"skipped": True, "subscribers": 0}
                assert owner_calls == ["stats_snapshots"] * 4
        finally:
            if child.is_alive():
                child.kill()
            child.join(timeout=5)
            child.close()
            broker.delete(key)


async def test_acl_errors_failed_attach_and_repeated_detach_cannot_remove_a_peer(
    isolated_presence,
):
    url, key = isolated_presence
    user = "stats-lease-" + uuid.uuid4().hex
    password = secrets.token_urlsafe(24)
    owner_url = url.render_as_string(hide_password=False)
    restricted_url = url.set(username=user, password=password).render_as_string(
        hide_password=False
    )
    async with (
        aioredis.from_url(owner_url) as owner,
        aioredis.from_url(restricted_url) as restricted,
    ):
        await owner.execute_command(
            "ACL",
            "SETUSER",
            user,
            "on",
            ">" + password,
            "~" + key,
            "+@all",
            "-eval",
        )
        live = presence.MLStatsPresence(owner)
        rejected = presence.MLStatsPresence(restricted)
        cleanup_failure = presence.MLStatsPresence(restricted)
        maintainer = None
        try:
            with pytest.raises(redis.exceptions.NoPermissionError):
                await restricted.eval("return 1", 0)
            with TestCase().assertLogs(presence.log, level="WARNING"):
                await asyncio.gather(live.refresh(), rejected.refresh())
            maintainer = asyncio.create_task(live.maintain())
            original = await owner.zrange(key, 0, -1)
            assert len(original) == 1
            await asyncio.gather(rejected.detach(), rejected.detach())
            assert await owner.zrange(key, 0, -1) == original

            await owner.execute_command("ACL", "SETUSER", user, "+eval", "-zrem")
            await cleanup_failure.refresh()
            assert await owner.zcard(key) == 2
            with TestCase().assertLogs(presence.log, level="WARNING"):
                await asyncio.gather(cleanup_failure.detach(), cleanup_failure.detach())
            assert await owner.zcard(key) == 2
            with redis.from_url(owner_url) as observer:
                async with asyncio.timeout(10):
                    while presence.count_live_stats_subscribers(observer) != 1:
                        await asyncio.sleep(0.03)
                assert await owner.zrange(key, 0, -1) == original
                maintainer.cancel()
                await asyncio.gather(maintainer, return_exceptions=True)
                await asyncio.gather(live.detach(), live.detach())
                assert presence.count_live_stats_subscribers(observer) == 0
        finally:
            if maintainer is not None:
                maintainer.cancel()
                await asyncio.gather(maintainer, return_exceptions=True)
            await owner.delete(key)
            await owner.execute_command("ACL", "DELUSER", user)
