from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from aap_notifications.publication_client import RemotePublicationClient
from aap_notifications.publication_contract import (
    IMMEDIATE_PUBLICATION_PROTOCOL,
    ImmediatePublicationRequest,
    ImmediatePublicationUnavailable,
    PublicationAuthorization,
)


def _request():
    return ImmediatePublicationRequest(
        schema=IMMEDIATE_PUBLICATION_PROTOCOL,
        user_id=uuid.UUID("50000000-0000-4000-8000-000000000001"),
        message={
            "id": "50000000-0000-4000-8000-000000000002",
            "type": "open.event.type",
            "target_type": "task",
            "target_id": "50000000-0000-4000-8000-000000000003",
            "payload": {"nested": [2**100, "你好"]},
            "created_at": "2026-10-24T12:00:00+08:00",
        },
        authorization=PublicationAuthorization(
            restricted=True,
            project_id=uuid.UUID("50000000-0000-4000-8000-000000000004"),
            export_required=False,
            member=True,
            reviewer=False,
            owner=False,
            super_admin=False,
        ),
    )


@pytest.mark.parametrize(
    "origin",
    [
        "relative/path",
        "ftp://publisher.test",
        "http://user:secret@publisher.test",
        "http://publisher.test/publish",
        "http://publisher.test?token=secret",
        "http://publisher.test?",
        "http://publisher.test#fragment",
        "http://publisher.test#",
        "http://publisher.test:bad",
        "http://publisher.test:",
    ],
)
def test_client_rejects_non_root_or_unsafe_origins(origin):
    with pytest.raises(ValueError):
        RemotePublicationClient(origin, "service-token")


def test_client_rejects_unsafe_service_tokens_and_timeouts():
    for token in ("", " leading", "trailing ", "line\nbreak", "t" * 4097):
        with pytest.raises(ValueError):
            RemotePublicationClient("http://publisher.test", token)
    for timeout in (0, -1, 61, float("nan"), True):
        with pytest.raises(ValueError):
            RemotePublicationClient("http://publisher.test", "service-token", timeout)


@pytest.mark.asyncio
async def test_client_posts_once_without_redirects_or_environment_proxy(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"schema": IMMEDIATE_PUBLICATION_PROTOCOL, "published": True},
            request=request,
        )

    clients, options = _mock_httpx_transport(monkeypatch, handler)
    client = RemotePublicationClient("http://publisher.test/", "private-service-token")

    accepted = await client.publish(_request())

    assert accepted is True
    assert len(requests) == 1
    assert str(requests[0].url) == "http://publisher.test/v1/notifications/publish"
    assert options[0] == {
        "timeout": 10.0,
        "follow_redirects": False,
        "trust_env": False,
    }
    assert clients[0].is_closed
    assert requests[0].headers["Authorization"] == "Bearer private-service-token"
    assert json.loads(requests[0].content)["message"] == _request().message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (302, b'{"schema":"wrong","published":true}'),
        (200, b'{"schema":"aap-notifications-immediate-publication/v1","published":1}'),
    ],
)
async def test_client_sanitizes_http_and_reply_failures(
    monkeypatch, status_code, content
):
    requests = []

    def handler(request):
        requests.append(request)
        headers = {"Location": "http://elsewhere.test"} if status_code == 302 else None
        return httpx.Response(
            status_code, content=content, headers=headers, request=request
        )

    clients, _options = _mock_httpx_transport(monkeypatch, handler)
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(ImmediatePublicationUnavailable) as error:
        await client.publish(_request())
    assert "wrong" not in str(error.value)
    assert "published" not in str(error.value)
    assert len(requests) == 1
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_client_redacts_transport_exception(monkeypatch):
    def handler(_request):
        raise RuntimeError("https://service:secret@publisher.test private payload")

    clients, _options = _mock_httpx_transport(monkeypatch, handler)
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(ImmediatePublicationUnavailable) as error:
        await client.publish(_request())
    assert "secret" not in str(error.value)
    assert "private payload" not in str(error.value)
    assert clients[0].is_closed


def _mock_httpx_transport(monkeypatch, handler):
    clients = []
    options = []
    real_async_client = httpx.AsyncClient

    def create_client(**kwargs):
        options.append(kwargs.copy())
        client = real_async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    return clients, options


@pytest.mark.asyncio
async def test_httpx_streaming_caps_reply_and_closes_response_and_client(monkeypatch):
    streams = []
    requests = []

    class OversizedBody(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            streams.append(self)

        async def __aiter__(self):
            yield b"x" * 4097

        async def aclose(self):
            self.closed = True

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=OversizedBody(), request=request)

    clients, _options = _mock_httpx_transport(monkeypatch, handler)
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(ImmediatePublicationUnavailable):
        await client.publish(_request())

    assert len(requests) == 1
    assert streams[0].closed
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_httpx_streaming_propagates_cancellation_and_closes_resources(
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()
    streams = []
    requests = []

    class BlockingBody(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            streams.append(self)

        async def __aiter__(self):
            started.set()
            await release.wait()
            yield b'{"schema":'

        async def aclose(self):
            self.closed = True

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=BlockingBody(), request=request)

    clients, _options = _mock_httpx_transport(monkeypatch, handler)
    client = RemotePublicationClient("http://publisher.test", "service-token")
    task = asyncio.create_task(client.publish(_request()))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(requests) == 1
    assert streams[0].closed
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_httpx_read_timeout_is_sanitized_and_closes_response_and_client(
    monkeypatch,
):
    streams = []

    class TimedOutBody(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            streams.append(self)

        async def __aiter__(self):
            raise httpx.ReadTimeout("private transport details")
            yield b""

        async def aclose(self):
            self.closed = True

    def handler(request):
        return httpx.Response(200, stream=TimedOutBody(), request=request)

    clients, _options = _mock_httpx_transport(monkeypatch, handler)
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(ImmediatePublicationUnavailable) as error:
        await client.publish(_request())

    assert "private transport details" not in str(error.value)
    assert streams[0].closed
    assert clients[0].is_closed
