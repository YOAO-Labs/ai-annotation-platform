"""WebSocket relay, credential-fact, and reauthorization regressions."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import ws
from app.core.security import create_access_token
from app.db.models.api_key import ApiKey
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.identity_gateway import IdentityGateway
from app.services.notification import NotificationService
from tests.factory import create_api_key, create_project, create_task, create_user


def _actor_facts(*, role: str = "employee") -> IdentityActorFacts:
    return IdentityActorFacts(
        id=uuid.uuid4(),
        name="WebSocket actor",
        email="ws-actor@test.local",
        role=role,
        is_active=True,
    )


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


class _FakeHandshakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int) -> None:
        self.close_code = code


class _FakeStatsPubSub:
    async def subscribe(self, _channel: str) -> None:
        pass

    async def unsubscribe(self, _channel: str) -> None:
        pass

    async def close(self) -> None:
        pass


class _FakeStatsRedis:
    def pubsub(self) -> _FakeStatsPubSub:
        return _FakeStatsPubSub()

    async def incr(self, _key: str) -> int:
        return 1

    async def decr(self, _key: str) -> int:
        return 0


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
    async def invalid_credential(_token, *, allow_api_key=True):
        return None

    monkeypatch.setattr(ws, "_authenticate_socket_token", invalid_credential)
    assert await ws._revalidate_project_stream("tok", None) is False


@pytest.fixture(autouse=True)
def _no_redis_pool(monkeypatch):
    # The relay test never touches Redis, but importing the module must not
    # construct a live pool.
    yield


async def test_notification_parser_fails_closed(monkeypatch):
    user = SimpleNamespace(actor_id=uuid.uuid4(), role="employee")

    async def fake_auth(_token, *, allow_api_key=True):
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
    user = SimpleNamespace(actor_id=uuid.uuid4(), role="employee")

    async def fake_auth(_token, *, allow_api_key=True):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.base.async_session", lambda: _Ctx())

    seen: dict = {}

    async def fake_notification(self, candidate_id):
        seen["notification_id"] = candidate_id
        return object()  # row is committed and visible

    async def fake_deliverable(self, notification_id, user_id):
        seen["pair"] = (notification_id, user_id)
        return False

    monkeypatch.setattr(NotificationService, "notification_by_id", fake_notification)
    monkeypatch.setattr(
        NotificationService, "notification_deliverable", fake_deliverable
    )
    notification_id = uuid.uuid4()
    allowed = await ws._notification_message_allowed(
        "t", json.dumps({"id": str(notification_id)})
    )
    assert allowed is False
    assert seen["pair"] == (notification_id, user.actor_id)


async def test_notification_gate_retries_until_row_visible(monkeypatch):
    """A message published before its transaction commits is not dropped: the
    gate waits for the row to become visible instead of failing immediately."""

    user = SimpleNamespace(actor_id=uuid.uuid4(), role="employee")
    notification_id = uuid.uuid4()

    async def fake_auth(_token, *, allow_api_key=True):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)
    monkeypatch.setattr(ws, "_NOTIFICATION_GATE_RETRY_SECONDS", 0)

    class _FakeDb:
        def __init__(self) -> None:
            self.looks = 0

    fake_db = _FakeDb()

    class _Ctx:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.base.async_session", lambda: _Ctx())

    async def fake_deliverable(self, _notification_id, _user_id):
        return True

    async def fake_notification(self, _notification_id):
        fake_db.looks += 1
        return object() if fake_db.looks >= 2 else None

    monkeypatch.setattr(NotificationService, "notification_by_id", fake_notification)
    monkeypatch.setattr(
        NotificationService, "notification_deliverable", fake_deliverable
    )
    allowed = await ws._notification_message_allowed(
        "t", json.dumps({"id": str(notification_id)})
    )
    assert allowed is True
    assert fake_db.looks == 2


async def test_notification_gate_fails_closed_after_retry_window(monkeypatch):
    user = SimpleNamespace(actor_id=uuid.uuid4(), role="employee")

    async def fake_auth(_token, *, allow_api_key=True):
        return user

    monkeypatch.setattr(ws, "_authenticate_socket_token", fake_auth)
    monkeypatch.setattr(ws, "_NOTIFICATION_GATE_RETRY_SECONDS", 0)

    class _FakeDb:
        def __init__(self) -> None:
            self.looks = 0

    fake_db = _FakeDb()

    class _Ctx:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.base.async_session", lambda: _Ctx())

    async def fake_notification(self, _notification_id):
        fake_db.looks += 1
        return None  # row never becomes visible

    monkeypatch.setattr(NotificationService, "notification_by_id", fake_notification)
    assert (
        await ws._notification_message_allowed(
            "t", json.dumps({"id": str(uuid.uuid4())})
        )
        is False
    )
    assert fake_db.looks == 4


async def test_load_active_user_returns_facts_after_session_closes(
    test_engine, monkeypatch
):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as setup:
        active = await create_user(
            setup,
            "super_admin",
            f"ws-active-{uuid.uuid4()}@test.local",
            "Active socket actor",
        )
        inactive = await create_user(
            setup,
            "employee",
            f"ws-inactive-{uuid.uuid4()}@test.local",
            "Inactive socket actor",
        )
        inactive.is_active = False
        active_id, inactive_id = active.id, inactive.id
        expected = IdentityActorStatus(
            id=active.id,
            role=active.role,
            is_active=True,
        )
        await setup.commit()

    monkeypatch.setattr("app.db.base.async_session", sessions)
    try:
        actor = await ws._load_active_user(str(active_id))
        assert actor == expected
        assert isinstance(actor, IdentityActorStatus)
        assert not isinstance(actor, User)
        assert actor.role == "super_admin"
        assert await ws._load_active_user(None) is None
        assert await ws._load_active_user("not-a-uuid") is None
        assert await ws._load_active_user(str(uuid.uuid4())) is None
        assert await ws._load_active_user(str(inactive_id)) is None
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(User).where(User.id.in_([active_id, inactive_id]))
            )
            await cleanup.commit()


async def test_task_stream_rechecks_account_after_identity_fact_snapshot(
    test_engine, monkeypatch
):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as setup:
        actor = await create_user(
            setup, "project_admin", f"ws-revoked-{uuid.uuid4()}@test.local", "Owner"
        )
        project = await create_project(setup, owner_id=actor.id)
        task = await create_task(setup, project_id=project.id)
        actor_id, project_id, task_id = actor.id, project.id, task.id
        await setup.commit()

    monkeypatch.setattr("app.db.base.async_session", sessions)
    load_active_user = ws._load_active_user
    captured = []

    async def load_then_revoke(user_id):
        facts = await load_active_user(user_id)
        assert isinstance(facts, IdentityActorStatus)
        assert facts.is_active
        captured.append(facts)
        async with sessions() as revoke:
            await revoke.execute(
                update(User).where(User.id == actor_id).values(is_active=False)
            )
            await revoke.commit()
        return facts

    monkeypatch.setattr(ws, "_load_active_user", load_then_revoke)
    try:
        assert not await ws._revalidate_task_visible(task_id, str(actor_id))
        assert len(captured) == 1 and captured[0].is_active
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(Task).where(Task.id == task_id))
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
            await cleanup.commit()


async def test_socket_jwt_auth_uses_current_actor_and_keeps_revocation_policy(
    monkeypatch,
):
    actor = SimpleNamespace(
        actor_id=uuid.uuid4(), role="super_admin", credential_type="jwt"
    )
    calls: list[str] = []
    failure: list[Exception | None] = [None]

    class FakeGateway:
        async def authenticate(self, token: str, *, db=None):
            calls.append(token)
            if failure[0] is not None:
                raise failure[0]
            return actor

    monkeypatch.setattr(ws, "get_identity_gateway", FakeGateway)

    authenticated = await ws._authenticate_socket_token("jwt")
    assert authenticated is actor
    assert authenticated.role == "super_admin"
    failure[0] = ws.IdentityCredentialRejected("credential_revoked")
    assert await ws._authenticate_socket_token("jwt") is None
    failure[0] = ws.IdentityGatewayUnavailable()
    assert await ws._authenticate_socket_token("jwt") is None
    assert calls == ["jwt", "jwt", "jwt"]


async def test_api_key_and_jwt_socket_policies_share_identity_gateway(monkeypatch):
    actor_id = uuid.uuid4()
    api_key_principal = SimpleNamespace(
        actor_id=actor_id, role="super_admin", credential_type="api_key"
    )
    jwt_principal = SimpleNamespace(
        actor_id=actor_id, role="super_admin", credential_type="jwt"
    )
    calls: list[str] = []

    class FakeGateway:
        async def authenticate(self, token: str, *, db=None):
            calls.append(token)
            return api_key_principal if token.startswith("ak_") else jwt_principal

    gateway = FakeGateway()
    monkeypatch.setattr(ws, "get_identity_gateway", lambda: gateway)
    api_key = "ak_" + "k" * 32
    assert await ws._authenticate_socket_token(api_key) is api_key_principal
    assert await ws._authenticate_socket_token(api_key, allow_api_key=False) is None
    assert calls == [api_key]

    stats_redis = _FakeStatsRedis()

    async def no_stream(_websocket, _pubsub, **_kwargs) -> None:
        return None

    monkeypatch.setattr(ws, "_get_redis_pool", lambda: "test-pool")
    monkeypatch.setattr(ws.aioredis, "Redis", lambda connection_pool: stats_redis)
    monkeypatch.setattr(ws, "_run_pubsub_ws", no_stream)

    stats_socket = _FakeHandshakeWebSocket()
    await ws.ml_backend_stats_socket(stats_socket, token=api_key)
    assert stats_socket.accepted
    assert calls[-1] == api_key

    for handler, extra in (
        (ws.prediction_jobs_socket, {}),
        (ws.notifications_socket, {}),
        (ws.video_tracker_job_socket, {"job_id": uuid.uuid4()}),
    ):
        socket = _FakeHandshakeWebSocket()
        await handler(socket, token=api_key, **extra)
        assert not socket.accepted
        assert socket.close_code == ws.status.WS_1008_POLICY_VIOLATION

    jwt_token = create_access_token(subject=str(actor_id), role="super_admin")
    jwt_socket = _FakeHandshakeWebSocket()
    await ws.prediction_jobs_socket(jwt_socket, token=jwt_token)
    assert jwt_socket.accepted
    assert calls[-1] == jwt_token


@pytest.mark.asyncio
async def test_socket_gateway_persists_api_key_usage_from_function_runtime(
    test_engine, monkeypatch
):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as setup:
        user = await create_user(
            setup,
            "super_admin",
            f"ws-api-key-{uuid.uuid4()}@test.local",
            "API socket admin",
        )
        key, plaintext = await create_api_key(setup, user.id, "WS Identity test", [])
        user_id, key_id = user.id, key.id
        await setup.commit()

    gateway = IdentityGateway(
        auth_database_url=(
            os.environ.get("AAP_MODULE_DATA_IDENTITY_AUTH_URL")
            or test_engine.url.render_as_string(hide_password=False)
        )
    )
    monkeypatch.setattr(ws, "get_identity_gateway", lambda: gateway)
    try:
        principal = await ws._authenticate_socket_token(plaintext)
        assert principal is not None
        assert principal.actor_id == user_id
        assert principal.credential_type == "api_key"
        async with sessions() as check:
            persisted = await check.get(ApiKey, key_id)
            assert persisted is not None and persisted.last_used_at is not None
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(ApiKey).where(ApiKey.id == key_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
