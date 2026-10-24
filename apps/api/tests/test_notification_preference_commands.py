"""Consumer-facing notification-preference command behavior over live adapters."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.db.models.notification import Notification
from app.db.models.notification_preference import NotificationPreference
from app.db.models.user import User
from app.deps import get_db
from app.services.notification import channel_for
from tests.factory import make_user_dict


_API_ROOT = Path(__file__).resolve().parents[1]
_request_session: ContextVar[AsyncSession | None] = ContextVar(
    "notification_preference_request_session", default=None
)
_NO_OVERRIDE = object()


@pytest.fixture
async def preference_owner(test_engine):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    email = f"notification-preference-{uuid.uuid4().hex}@test.local"
    user = User(**make_user_dict("annotator", email, "Preference owner"))
    async with session_factory() as session:
        session.add(user)
        await session.commit()
    user_id = user.id
    token = create_access_token(subject=str(user_id), role="annotator")
    try:
        yield user_id, token, session_factory
    finally:
        async with session_factory() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()


@asynccontextmanager
async def _api_client(app_module, *, raise_app_exceptions: bool = True):
    previous = app_module.dependency_overrides.get(get_db, _NO_OVERRIDE)

    async def request_session():
        session = _request_session.get()
        if session is None:
            raise RuntimeError("request session was not bound")
        yield session

    app_module.dependency_overrides[get_db] = request_session
    transport = httpx.ASGITransport(
        app=app_module, raise_app_exceptions=raise_app_exceptions
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        if previous is _NO_OVERRIDE:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


async def _request(
    client: httpx.AsyncClient,
    session: AsyncSession,
    token: str,
    method: str,
    path: str,
    *,
    body: dict | None = None,
) -> httpx.Response:
    context_token = _request_session.set(session)
    try:
        kwargs = {"headers": {"Authorization": f"Bearer {token}"}}
        if body is not None:
            kwargs["json"] = body
        return await client.request(method, path, **kwargs)
    finally:
        _request_session.reset(context_token)


@pytest.fixture
async def preference_subscriber(preference_owner) -> AsyncIterator:
    user_id, _token, _session_factory = preference_owner
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    try:
        assert await redis.ping() is True
        await pubsub.subscribe(channel_for(user_id))
        subscription = await pubsub.get_message(timeout=1)
        assert subscription is not None and subscription["type"] == "subscribe"
        yield pubsub
    finally:
        await pubsub.aclose()
        await redis.aclose()


async def _receive_sync(pubsub, *, timeout: float = 2) -> dict | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (remaining := deadline - loop.time()) > 0:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True, timeout=min(remaining, 0.1)
        )
        if message is not None:
            return json.loads(message["data"])
    return None


@pytest.mark.asyncio
async def test_asgi_preference_commit_is_visible_before_real_redis_sync(
    app_module, preference_owner, preference_subscriber
):
    user_id, token, session_factory = preference_owner
    async with _api_client(app_module) as client:
        async with session_factory() as session:
            created = await _request(
                client,
                session,
                token,
                "PUT",
                "/api/v1/notification-preferences",
                body={"type": "task.rejected", "in_app": False},
            )
        assert created.status_code == 200 and created.json() == {"ok": True}
        assert await _receive_sync(preference_subscriber) == {
            "type": "notifications.sync",
            "reason": "preferences",
        }

        async with session_factory() as session:
            stored = await session.scalar(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.type == "task.rejected",
                )
            )
            notification_count = await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id
                )
            )
        assert stored is not None
        assert stored.channels == {"in_app": False, "email": False, "toast": True}
        assert notification_count == 0

        async with session_factory() as session:
            read = await _request(
                client,
                session,
                token,
                "GET",
                "/api/v1/notification-preferences",
            )
        assert read.status_code == 200
        by_type = {item["type"]: item for item in read.json()["items"]}
        assert by_type["task.rejected"] == {
            "type": "task.rejected",
            "in_app": False,
            "email": False,
            "toast": True,
        }
        assert by_type["task.approved"]["toast"] is False

        async with session_factory() as session:
            updated = await _request(
                client,
                session,
                token,
                "PUT",
                "/api/v1/notification-preferences",
                body={"type": "task.rejected", "toast": False},
            )
        assert updated.status_code == 200
        assert await _receive_sync(preference_subscriber) == {
            "type": "notifications.sync",
            "reason": "preferences",
        }

    async with session_factory() as session:
        stored = await session.scalar(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == "task.rejected",
            )
        )
    assert stored is not None
    assert stored.channels == {"in_app": False, "email": False, "toast": False}


@pytest.mark.asyncio
async def test_independent_concurrent_asgi_updates_merge_jsonb_keys(
    app_module, preference_owner
):
    user_id, token, session_factory = preference_owner
    async with session_factory() as session:
        session.add(
            NotificationPreference(
                user_id=user_id,
                type="batch.rejected",
                channels={
                    "in_app": True,
                    "email": True,
                    "toast": False,
                    "future_channel": {"enabled": True},
                },
            )
        )
        await session.commit()

    barrier = asyncio.Barrier(2)
    async with _api_client(app_module) as client:

        async def update(channel: str, value: bool) -> tuple[httpx.Response, int]:
            async with session_factory() as session:
                backend_pid = await session.scalar(select(func.pg_backend_pid()))
                assert backend_pid is not None
                await barrier.wait()
                response = await _request(
                    client,
                    session,
                    token,
                    "PUT",
                    "/api/v1/notification-preferences",
                    body={"type": "batch.rejected", channel: value},
                )
                return response, backend_pid

        (receipt_update, receipt_pid), (toast_update, toast_pid) = await asyncio.gather(
            update("in_app", False), update("toast", True)
        )
    assert receipt_update.status_code == toast_update.status_code == 200
    assert receipt_pid != toast_pid

    async with session_factory() as session:
        stored = await session.scalar(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == "batch.rejected",
            )
        )
        row_count = await session.scalar(
            select(func.count(NotificationPreference.type)).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == "batch.rejected",
            )
        )
    assert stored is not None
    assert stored.channels == {
        "in_app": False,
        "email": True,
        "toast": True,
        "future_channel": {"enabled": True},
    }
    assert row_count == 1


@pytest.mark.asyncio
async def test_database_commit_failure_does_not_publish_or_persist(
    app_module, test_engine, preference_owner, preference_subscriber
):
    user_id, token, session_factory = preference_owner
    suffix = uuid.uuid4().hex
    function_name = f"reject_preference_commit_{suffix}"
    trigger_name = f"reject_preference_commit_trigger_{suffix}"
    try:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"""CREATE FUNCTION {function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.user_id = '{user_id}'::uuid
                        AND NEW.type = 'task.rejected' THEN
                        RAISE EXCEPTION 'intentional deferred preference test failure';
                    END IF;
                    RETURN NEW;
                END
                $$"""
            )
            await connection.exec_driver_sql(
                f"""CREATE CONSTRAINT TRIGGER {trigger_name}
                AFTER INSERT OR UPDATE ON notification_preferences
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION {function_name}()"""
            )

        async with _api_client(app_module, raise_app_exceptions=False) as client:
            async with session_factory() as session:
                failed = await _request(
                    client,
                    session,
                    token,
                    "PUT",
                    "/api/v1/notification-preferences",
                    body={"type": "task.rejected", "toast": True},
                )
        assert failed.status_code == 500
        assert await _receive_sync(preference_subscriber, timeout=0.25) is None
        async with session_factory() as session:
            stored = await session.scalar(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.type == "task.rejected",
                )
            )
        assert stored is None
    finally:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS {trigger_name} ON notification_preferences"
            )
            await connection.exec_driver_sql(
                f"DROP FUNCTION IF EXISTS {function_name}()"
            )


@pytest.mark.asyncio
async def test_redis_publish_failure_keeps_committed_preference(
    app_module, preference_owner, preference_subscriber, monkeypatch
):
    user_id, token, session_factory = preference_owner
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    async with _api_client(app_module) as client:
        async with session_factory() as session:
            response = await _request(
                client,
                session,
                token,
                "PUT",
                "/api/v1/notification-preferences",
                body={"type": "export.failed", "toast": False},
            )
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert await _receive_sync(preference_subscriber, timeout=0.25) is None
    async with session_factory() as session:
        stored = await session.scalar(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == "export.failed",
            )
        )
    assert stored is not None
    assert stored.channels == {"in_app": True, "email": False, "toast": False}


def test_notification_preference_catalog_cold_load_uses_only_stdlib():
    script = """
import importlib
import sys
sys.path.insert(0, sys.argv[1])
importlib.import_module("aap_notifications.preferences_policy")
blocked = (
    "sqlalchemy",
    "fastapi",
    "redis",
    "celery",
    "app.db",
    "app.config",
    "app.api",
    "app.workers",
)
assert not [
    name
    for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in blocked)
]
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(_API_ROOT.parent / "notifications" / "src"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
