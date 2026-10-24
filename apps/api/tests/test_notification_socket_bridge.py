"""Actual loopback owner connections preserve the existing API socket binding."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from app.services.notification_socket_bridge import (
    _TRANSPORT_LOG,
    notification_socket_endpoint,
    proxy_notification_socket,
)


class Downstream:
    def __init__(self):
        self.accepted = asyncio.Event()
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closes = []
        self.disconnected = False

    async def accept(self):
        self.accepted.set()

    async def receive(self):
        event = await self.incoming.get()
        self.disconnected = event["type"] == "websocket.disconnect"
        return event

    async def send_text(self, value):
        await self.outgoing.put(value)

    async def send_bytes(self, value):
        await self.outgoing.put(value)

    async def close(self, code, reason=""):
        if self.disconnected:
            raise RuntimeError("already disconnected")
        self.closes.append((code, reason))


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://owner",
        "ws://user:secret@owner",
        "ws://owner/path",
        "ws://owner?x=1",
        "ws://owner#x",
    ],
)
def test_bridge_rejects_origins_that_change_the_fixed_owner_binding(origin):
    with pytest.raises(ValueError):
        notification_socket_endpoint(origin, "credential")


@pytest.mark.asyncio
async def test_bridge_relays_raw_large_and_binary_frames_without_proxy(monkeypatch):
    token = "query + / ? = & credential"
    raw = '{ "type": "notifications.sync", "extension": [1, null, true] }'
    large = "payload:" + "x" * (2 * 1024 * 1024)
    owner_closed = asyncio.Event()

    async def owner(connection):
        assert urlsplit(connection.request.path).path == "/ws/notifications"
        assert parse_qs(urlsplit(connection.request.path).query)["token"] == [token]
        await connection.send(raw)
        await connection.send(b"\x00\xffbinary")
        await connection.send(large)
        assert await connection.recv() == "client frame"
        await connection.send("received")
        await connection.wait_closed()
        assert (connection.close_code, connection.close_reason) == (1000, "client done")
        owner_closed.set()

    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    async with serve(owner, "127.0.0.1", 0, logger=_TRANSPORT_LOG) as server:
        port = server.sockets[0].getsockname()[1]
        downstream = Downstream()
        task = asyncio.create_task(
            proxy_notification_socket(downstream, token, f"ws://127.0.0.1:{port}")
        )
        try:
            await asyncio.wait_for(downstream.accepted.wait(), 3)
            await downstream.incoming.put(
                {"type": "websocket.receive", "text": "client frame"}
            )
            assert await asyncio.wait_for(downstream.outgoing.get(), 3) == raw
            assert (
                await asyncio.wait_for(downstream.outgoing.get(), 3)
                == b"\x00\xffbinary"
            )
            assert await asyncio.wait_for(downstream.outgoing.get(), 3) == large
            assert await asyncio.wait_for(downstream.outgoing.get(), 3) == "received"
            await downstream.incoming.put(
                {"type": "websocket.disconnect", "code": 1000, "reason": "client done"}
            )
            await asyncio.wait_for(task, 3)
            await asyncio.wait_for(owner_closed.wait(), 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_bridge_denies_before_accept_and_does_not_follow_credential_redirect(
    caplog,
):
    token = "never-log-or-forward-this-credential"
    sink_connections = []

    async def sink(connection):
        sink_connections.append(connection.request.path)

    async with serve(sink, "127.0.0.1", 0, logger=_TRANSPORT_LOG) as sink_server:
        sink_port = sink_server.sockets[0].getsockname()[1]

        async def redirect(_connection, request):
            location = f"ws://127.0.0.1:{sink_port}/?" + urlsplit(request.path).query
            return Response(302, "Found", Headers([("Location", location)]), b"")

        async with serve(
            sink, "127.0.0.1", 0, process_request=redirect, logger=_TRANSPORT_LOG
        ) as owner_server:
            port = owner_server.sockets[0].getsockname()[1]
            downstream = Downstream()
            await asyncio.wait_for(
                proxy_notification_socket(downstream, token, f"ws://127.0.0.1:{port}"),
                3,
            )
    assert not downstream.accepted.is_set()
    assert downstream.closes == [(1008, "")]
    assert sink_connections == []
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_bridge_preserves_owner_close_code_and_reason():
    async def owner(connection):
        await connection.close(4001, "owner stopped")

    async with serve(owner, "127.0.0.1", 0, logger=_TRANSPORT_LOG) as server:
        downstream = Downstream()
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(
            proxy_notification_socket(downstream, "token", f"ws://127.0.0.1:{port}"), 3
        )
    assert downstream.accepted.is_set()
    assert downstream.closes == [(4001, "owner stopped")]


@pytest.mark.asyncio
async def test_bridge_cancellation_releases_the_actual_owner_connection():
    owner_closed = asyncio.Event()

    async def owner(connection):
        await connection.wait_closed()
        owner_closed.set()

    async with serve(owner, "127.0.0.1", 0, logger=_TRANSPORT_LOG) as server:
        port = server.sockets[0].getsockname()[1]
        downstream = Downstream()
        task = asyncio.create_task(
            proxy_notification_socket(downstream, "token", f"ws://127.0.0.1:{port}")
        )
        await asyncio.wait_for(downstream.accepted.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(owner_closed.wait(), 3)
    assert downstream.closes == [(1012, "")]


@pytest.mark.asyncio
async def test_public_route_selects_owner_without_embedded_admission(monkeypatch):
    from app.api.v1 import ws

    calls = []

    async def proxy(websocket, token, origin):
        calls.append((websocket, token, origin))

    async def no_embedded_admission(*_args, **_kwargs):
        raise AssertionError("Independent binding must use owner admission")

    monkeypatch.setattr(ws.settings, "notification_socket_url", "ws://owner:8180")
    monkeypatch.setattr(
        "app.services.notification_socket_bridge.proxy_notification_socket", proxy
    )
    monkeypatch.setattr(ws, "_notification_authenticate", no_embedded_admission)
    downstream = Downstream()
    await ws.notifications_socket(downstream, token="jwt")
    assert calls == [(downstream, "jwt", "ws://owner:8180")]


@pytest.mark.asyncio
async def test_invalid_selected_owner_binding_denies_without_embedded_fallback(
    monkeypatch,
):
    from app.api.v1 import ws

    async def no_embedded_admission(*_args, **_kwargs):
        raise AssertionError("A selected owner must never fall back")

    monkeypatch.setattr(ws.settings, "notification_socket_url", "http://invalid")
    monkeypatch.setattr(ws, "_notification_authenticate", no_embedded_admission)
    downstream = Downstream()
    await ws.notifications_socket(downstream, token="jwt")
    assert not downstream.accepted.is_set()
    assert downstream.closes == [(1008, "")]
