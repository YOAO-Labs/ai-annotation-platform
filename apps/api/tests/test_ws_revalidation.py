"""WebSocket relay, credential-fact, and reauthorization regressions."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import ws
from app.core.security import create_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts
from app.services import api_key_service
from app.services.notification import NotificationService
from tests.factory import create_project, create_task, create_user


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
    user = _actor_facts()

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
    user = _actor_facts()

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

    user = _actor_facts()
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
    user = _actor_facts()

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
        expected = IdentityActorFacts(
            id=active.id,
            name=active.name,
            email=active.email,
            role=active.role,
            is_active=True,
        )
        await setup.commit()

    monkeypatch.setattr("app.db.base.async_session", sessions)
    try:
        actor = await ws._load_active_user(str(active_id))
        assert actor == expected
        assert isinstance(actor, IdentityActorFacts)
        assert not isinstance(actor, User)
        assert actor.name == "Active socket actor"
        assert actor.email == expected.email
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
        assert isinstance(facts, IdentityActorFacts)
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
    actor = _actor_facts(role="super_admin")
    state = {"blacklisted": False, "generation": 4, "redis_down": False}
    loaded_subjects: list[str] = []

    monkeypatch.setattr(
        ws,
        "decode_access_token",
        lambda _token: {
            "sub": str(actor.id),
            "jti": "socket-jti",
            "gen": 4,
            "role": "viewer",
        },
    )

    async def is_blacklisted(_jti: str) -> bool:
        if state["redis_down"]:
            raise RuntimeError("temporary Redis failure")
        return state["blacklisted"]

    async def get_user_generation(_user_id: str) -> int:
        if state["redis_down"]:
            raise RuntimeError("temporary Redis failure")
        return state["generation"]

    async def load_active_user(user_id: str):
        loaded_subjects.append(user_id)
        return actor

    monkeypatch.setattr(ws, "is_blacklisted", is_blacklisted)
    monkeypatch.setattr(ws, "get_user_generation", get_user_generation)
    monkeypatch.setattr(ws, "_load_active_user", load_active_user)

    authenticated = await ws._authenticate_socket_token("jwt")
    assert authenticated is actor
    assert authenticated.role == "super_admin"  # DB fact overrides stale claim
    assert loaded_subjects == [str(actor.id)]

    state["blacklisted"] = True
    assert await ws._authenticate_socket_token("jwt") is None
    assert loaded_subjects == [str(actor.id)]

    state["blacklisted"] = False
    state["generation"] = 5
    assert await ws._authenticate_socket_token("jwt") is None
    assert loaded_subjects == [str(actor.id)]

    state["redis_down"] = True
    assert await ws._authenticate_socket_token("jwt") is actor
    assert loaded_subjects == [str(actor.id), str(actor.id)]


async def test_api_key_last_used_is_committed_and_handshake_policies_stay_distinct(
    test_engine, monkeypatch
):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    email = f"ws-api-key-{uuid.uuid4()}@test.local"
    async with sessions() as setup:
        user = await create_user(
            setup,
            "super_admin",
            email,
            "API socket admin",
        )
        plaintext, prefix = api_key_service.generate_token()
        api_key = ApiKey(
            user_id=user.id,
            name="WS identity test",
            key_prefix=prefix,
            key_hash=hash_password(plaintext),
            scopes=[],
        )
        setup.add(api_key)
        await setup.flush()
        user_id, api_key_id = user.id, api_key.id
        await setup.commit()

    monkeypatch.setattr("app.db.base.async_session", sessions)
    try:
        commits: list[None] = []

        def count_commit(_connection) -> None:
            commits.append(None)

        event.listen(test_engine.sync_engine, "commit", count_commit)
        try:
            actor = await ws._authenticate_socket_token(plaintext)
        finally:
            event.remove(test_engine.sync_engine, "commit", count_commit)
        assert actor == IdentityActorFacts(
            id=user_id,
            name="API socket admin",
            email=email,
            role="super_admin",
            is_active=True,
        )
        assert len(commits) == 1
        async with sessions() as check:
            used_key = await check.get(ApiKey, api_key_id)
            assert used_key is not None and used_key.last_used_at is not None

        stats_redis = _FakeStatsRedis()

        async def no_stream(_websocket, _pubsub, **_kwargs) -> None:
            return None

        monkeypatch.setattr(ws, "_get_redis_pool", lambda: "test-pool")
        monkeypatch.setattr(ws.aioredis, "Redis", lambda connection_pool: stats_redis)
        monkeypatch.setattr(ws, "_run_pubsub_ws", no_stream)

        commits.clear()
        event.listen(test_engine.sync_engine, "commit", count_commit)
        try:
            stats_socket = _FakeHandshakeWebSocket()
            await ws.ml_backend_stats_socket(stats_socket, token=plaintext)
        finally:
            event.remove(test_engine.sync_engine, "commit", count_commit)
        assert stats_socket.accepted
        assert stats_socket.close_code is None
        assert len(commits) == 1

        # These three endpoints deliberately remain direct JWT-only handshakes.
        for handler, extra in (
            (ws.prediction_jobs_socket, {}),
            (ws.notifications_socket, {}),
            (ws.video_tracker_job_socket, {"job_id": uuid.uuid4()}),
        ):
            socket = _FakeHandshakeWebSocket()
            await handler(socket, token=plaintext, **extra)
            assert not socket.accepted
            assert socket.close_code == ws.status.WS_1008_POLICY_VIOLATION

        # Direct JWT prediction-job handshake still uses the DB role without
        # adopting the shared helper's blacklist/generation admission checks.
        jwt = create_access_token(subject=str(user_id), role="viewer")

        async def unexpected_blacklist_check(_jti: str) -> bool:
            raise AssertionError("direct JWT handshake must not check blacklist")

        async def unexpected_generation_check(_user_id: str) -> int:
            raise AssertionError("direct JWT handshake must not check generation")

        monkeypatch.setattr(ws, "is_blacklisted", unexpected_blacklist_check)
        monkeypatch.setattr(ws, "get_user_generation", unexpected_generation_check)
        jwt_socket = _FakeHandshakeWebSocket()
        await ws.prediction_jobs_socket(jwt_socket, token=jwt)
        assert jwt_socket.accepted
        assert jwt_socket.close_code is None
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(ApiKey).where(ApiKey.id == api_key_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
