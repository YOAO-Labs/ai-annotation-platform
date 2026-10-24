from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import uuid

import pytest

from aap_notifications.websocket import (
    notification_message_allowed,
    run_pubsub_ws,
    serve_notification_socket,
)


class _Delivery:
    def __init__(self, rows=(), *, allowed=True, error=None):
        self.rows = list(rows)
        self.allowed = allowed
        self.error = error
        self.lookups = []
        self.deliveries = []

    async def notification_by_id(self, notification_id):
        self.lookups.append(notification_id)
        if self.error is not None:
            raise self.error
        return self.rows.pop(0) if self.rows else None

    async def notification_deliverable(self, notification_id, user_id):
        self.deliveries.append((notification_id, user_id))
        return self.allowed


class _WebSocket:
    def __init__(self):
        self.accepted = 0
        self.closed = []
        self.sent = []
        self.received = asyncio.Event()
        self.frames = asyncio.Queue()

    async def accept(self):
        self.accepted += 1

    async def close(self, *, code):
        self.closed.append(code)

    async def receive(self):
        return await self.frames.get()

    async def send_text(self, data):
        self.sent.append(data)
        self.received.set()


class _PubSub:
    def __init__(self, *, subscribe_error=None, unsubscribe_error=None):
        self.subscribe_error = subscribe_error
        self.unsubscribe_error = unsubscribe_error
        self.subscriptions = []
        self.unsubscriptions = []
        self.closed = 0
        self.started = asyncio.Event()

    async def subscribe(self, channel):
        self.subscriptions.append(channel)
        if self.subscribe_error is not None:
            raise self.subscribe_error

    async def unsubscribe(self, channel):
        self.unsubscriptions.append(channel)
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error

    async def close(self):
        self.closed += 1

    async def listen(self):
        self.started.set()
        await asyncio.Future()
        yield {}  # pragma: no cover


class _Redis:
    def __init__(self, pubsub):
        self._pubsub = pubsub
        self.created = 0

    def pubsub(self):
        self.created += 1
        return self._pubsub


def _principal(actor_id):
    return SimpleNamespace(actor_id=actor_id)


@pytest.mark.asyncio
async def test_message_gate_rejects_api_keys_before_identity_and_allows_sync():
    actor_id = uuid.uuid4()
    identity_calls = []
    open_calls = []

    async def authenticate(token):
        identity_calls.append(token)
        return _principal(actor_id)

    def open_delivery():
        open_calls.append(True)
        raise AssertionError("sync and rejected API key must not open PostgreSQL")

    assert not await notification_message_allowed(
        "ak_legacy",
        b'{"type":"notifications.sync"}',
        authenticate=authenticate,
        open_delivery=open_delivery,
    )
    assert identity_calls == []

    assert await notification_message_allowed(
        "jwt",
        b'{"type":"notifications.sync","reason":"reconnect"}',
        authenticate=authenticate,
        open_delivery=open_delivery,
    )
    assert identity_calls == ["jwt"]
    assert open_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"[]", b"{}", b'{"id":"not-a-uuid"}', b'{"id":null}'],
)
async def test_message_gate_fails_closed_on_malformed_or_unidentifiable_frames(payload):
    identity_calls = []

    async def authenticate(token):
        identity_calls.append(token)
        return _principal(uuid.uuid4())

    async def open_delivery():
        raise AssertionError("invalid frame must not open PostgreSQL")

    assert not await notification_message_allowed(
        "jwt", payload, authenticate=authenticate, open_delivery=open_delivery
    )
    assert identity_calls == ["jwt"]


@pytest.mark.asyncio
async def test_message_gate_reauthenticates_and_reuses_found_row_for_delivery_check():
    actor_id, notification_id = uuid.uuid4(), uuid.uuid4()
    delivery = _Delivery(rows=[object()])
    auth_calls = []
    open_calls = []

    async def authenticate(token):
        auth_calls.append(token)
        return _principal(actor_id)

    @asynccontextmanager
    async def open_delivery():
        open_calls.append(True)
        yield delivery

    raw = ('{"id":"' + str(notification_id) + '","payload":[null,7]}').encode()
    assert await notification_message_allowed(
        "jwt", raw, authenticate=authenticate, open_delivery=open_delivery
    )
    assert auth_calls == ["jwt"]
    assert open_calls == [True]
    assert delivery.lookups == [notification_id]
    assert delivery.deliveries == [(notification_id, actor_id)]


@pytest.mark.asyncio
async def test_missing_row_gets_four_looks_and_three_fixed_waits(monkeypatch):
    actor_id, notification_id = uuid.uuid4(), uuid.uuid4()
    delivery = _Delivery()
    delays = []

    async def authenticate(_token):
        return _principal(actor_id)

    @asynccontextmanager
    async def open_delivery():
        yield delivery

    async def no_wait(delay):
        delays.append(delay)

    monkeypatch.setattr("aap_notifications.websocket.asyncio.sleep", no_wait)
    assert not await notification_message_allowed(
        "jwt",
        json.dumps({"id": str(notification_id)}).encode(),
        authenticate=authenticate,
        open_delivery=open_delivery,
    )
    assert delivery.lookups == [notification_id] * 4
    assert delays == [0.15] * 3


@pytest.mark.asyncio
async def test_first_notification_read_errors_propagate():
    actor_id, notification_id = uuid.uuid4(), uuid.uuid4()
    failure = RuntimeError("database failure")
    delivery = _Delivery(error=failure)

    async def authenticate(_token):
        return _principal(actor_id)

    @asynccontextmanager
    async def open_delivery():
        yield delivery

    with pytest.raises(RuntimeError) as raised:
        await notification_message_allowed(
            "jwt",
            json.dumps({"id": str(notification_id)}).encode(),
            authenticate=authenticate,
            open_delivery=open_delivery,
        )
    assert raised.value is failure


@pytest.mark.asyncio
async def test_handshake_rejects_api_key_or_inactive_actor_before_accept():
    actor_id = uuid.uuid4()
    socket = _WebSocket()
    redis = _Redis(_PubSub())
    calls = []

    async def authenticate(token):
        calls.append(("authenticate", token))
        return _principal(actor_id)

    async def active_actor(user_id):
        calls.append(("status", user_id))
        return SimpleNamespace(is_active=False)

    async def open_delivery():
        raise AssertionError("rejected handshake must not open PostgreSQL")

    await serve_notification_socket(
        socket,
        "ak_legacy",
        authenticate=authenticate,
        active_actor=active_actor,
        open_delivery=open_delivery,
        redis_client=lambda: redis,
    )
    assert calls == []
    assert socket.accepted == 0
    assert socket.closed == [1008]
    assert redis.created == 0

    await serve_notification_socket(
        socket,
        "jwt",
        authenticate=authenticate,
        active_actor=active_actor,
        open_delivery=open_delivery,
        redis_client=lambda: redis,
    )
    assert calls == [("authenticate", "jwt"), ("status", actor_id)]
    assert socket.accepted == 0
    assert socket.closed == [1008, 1008]


@pytest.mark.asyncio
async def test_active_actor_role_snapshot_is_not_compared_to_token_role():
    actor_id = uuid.uuid4()
    socket = _WebSocket()
    pubsub = _PubSub()
    redis = _Redis(pubsub)

    async def authenticate(_token):
        return SimpleNamespace(actor_id=actor_id, role="employee")

    async def active_actor(user_id):
        assert user_id == actor_id
        return SimpleNamespace(is_active=True, role="viewer")

    async def open_delivery():
        raise AssertionError("no frame needs a delivery read")

    async def relay(_websocket, _pubsub, *, allow_message):
        assert await allow_message(b'{"type":"notifications.sync"}')

    await serve_notification_socket(
        socket,
        "jwt",
        authenticate=authenticate,
        active_actor=active_actor,
        open_delivery=open_delivery,
        redis_client=lambda: redis,
        relay=relay,
    )
    assert socket.accepted == 1
    assert socket.closed == []
    assert pubsub.subscriptions == [f"notify:{actor_id}"]
    assert pubsub.unsubscriptions == [f"notify:{actor_id}"]
    assert pubsub.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["subscribe", "unsubscribe"])
async def test_socket_cleanup_closes_pubsub_after_errors(failure):
    actor_id = uuid.uuid4()
    error = RuntimeError(f"{failure} failed")
    pubsub = _PubSub(
        subscribe_error=error if failure == "subscribe" else None,
        unsubscribe_error=error if failure == "unsubscribe" else None,
    )
    socket = _WebSocket()

    async def authenticate(_token):
        return _principal(actor_id)

    async def active_actor(_actor_id):
        return SimpleNamespace(is_active=True)

    async def open_delivery():
        raise AssertionError("no per-message delivery check")

    async def no_stream(_websocket, _pubsub, *, allow_message):
        return None

    await serve_notification_socket(
        socket,
        "jwt",
        authenticate=authenticate,
        active_actor=active_actor,
        open_delivery=open_delivery,
        redis_client=lambda: _Redis(pubsub),
        relay=no_stream,
    )
    assert pubsub.closed == 1
    assert pubsub.subscriptions == [f"notify:{actor_id}"]
    assert pubsub.unsubscriptions == [f"notify:{actor_id}"]


@pytest.mark.asyncio
async def test_socket_cleanup_closes_pubsub_when_subscribe_is_cancelled():
    actor_id = uuid.uuid4()
    pubsub = _PubSub(subscribe_error=asyncio.CancelledError())
    socket = _WebSocket()

    async def authenticate(_token):
        return _principal(actor_id)

    async def active_actor(_actor_id):
        return SimpleNamespace(is_active=True)

    async def open_delivery():
        raise AssertionError("no per-message delivery check")

    with pytest.raises(asyncio.CancelledError):
        await serve_notification_socket(
            socket,
            "jwt",
            authenticate=authenticate,
            active_actor=active_actor,
            open_delivery=open_delivery,
            redis_client=lambda: _Redis(pubsub),
        )
    assert pubsub.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["relay", "unsubscribe"])
async def test_socket_cleanup_preserves_cancellation_and_closes_pubsub(cancel_at):
    actor_id = uuid.uuid4()
    pubsub = _PubSub(
        unsubscribe_error=asyncio.CancelledError()
        if cancel_at == "unsubscribe"
        else None
    )
    socket = _WebSocket()

    async def authenticate(_token):
        return _principal(actor_id)

    async def active_actor(_actor_id):
        return SimpleNamespace(is_active=True)

    async def open_delivery():
        raise AssertionError("no per-message delivery check")

    async def relay(_websocket, _pubsub, *, allow_message):
        if cancel_at == "relay":
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await serve_notification_socket(
            socket,
            "jwt",
            authenticate=authenticate,
            active_actor=active_actor,
            open_delivery=open_delivery,
            redis_client=lambda: _Redis(pubsub),
            relay=relay,
        )
    assert pubsub.closed == 1


@pytest.mark.asyncio
async def test_pubsub_relay_preserves_raw_message_text_and_runs_message_gate():
    socket = _WebSocket()
    pubsub = _PubSub()
    raw = b'{"payload":[null,9007199254740993],"type":"future.event"}'
    checks = []

    async def listen():
        yield {"type": "subscribe", "data": 1}
        yield {"type": "message", "data": raw}
        await asyncio.Future()

    pubsub.listen = listen

    async def receive_after_send():
        await socket.received.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    socket.receive = receive_after_send

    async def allow_message(data):
        checks.append(data)
        return True

    await run_pubsub_ws(socket, pubsub, heartbeat=False, allow_message=allow_message)

    assert checks == [raw]
    assert socket.sent == [raw.decode()]


@pytest.mark.asyncio
async def test_pubsub_relay_cancels_and_gathers_pending_tasks():
    socket = _WebSocket()
    pubsub = _PubSub()
    cancelled = False

    async def listen():
        nonlocal cancelled
        pubsub.started.set()
        try:
            await asyncio.Future()
            yield {}  # pragma: no cover
        finally:
            cancelled = True

    pubsub.listen = listen
    relay = asyncio.create_task(run_pubsub_ws(socket, pubsub, heartbeat=False))
    await asyncio.wait_for(pubsub.started.wait(), timeout=1)
    relay.cancel()
    with pytest.raises(asyncio.CancelledError):
        await relay
    assert cancelled


@pytest.mark.asyncio
async def test_heartbeat_uses_thirty_second_interval(monkeypatch):
    socket = _WebSocket()
    pubsub = _PubSub()
    delays = []
    original_sleep = asyncio.sleep

    async def cooperative_sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("aap_notifications.websocket.asyncio.sleep", cooperative_sleep)

    async def listen():
        await asyncio.Future()
        yield {}  # pragma: no cover

    pubsub.listen = listen
    monitor = asyncio.create_task(run_pubsub_ws(socket, pubsub))
    await asyncio.wait_for(socket.received.wait(), timeout=1)
    await socket.frames.put({"type": "websocket.disconnect", "code": 1000})
    await monitor

    assert socket.sent[0] == '{"type": "ping"}'
    assert delays[0] == 30
