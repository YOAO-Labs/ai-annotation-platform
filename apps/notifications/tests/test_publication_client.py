from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import uuid

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
    seen = {}

    class Response:
        status_code = 200
        content = json.dumps(
            {"schema": IMMEDIATE_PUBLICATION_PROTOCOL, "published": True}
        ).encode()

    class Client:
        def __init__(self, **kwargs):
            seen["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            seen["client_closed"] = True
            return None

        async def post(self, url, **kwargs):
            seen["url"] = url
            seen["request"] = kwargs
            return Response()

    monkeypatch.setitem(
        __import__("sys").modules, "httpx", SimpleNamespace(AsyncClient=Client)
    )
    client = RemotePublicationClient("http://publisher.test/", "private-service-token")

    accepted = await client.publish(_request())

    assert accepted is True
    assert seen["url"] == "http://publisher.test/v1/notifications/publish"
    assert seen["client_options"] == {
        "timeout": 10.0,
        "follow_redirects": False,
        "trust_env": False,
    }
    assert seen["client_closed"]
    assert seen["request"]["headers"]["Authorization"] == "Bearer private-service-token"
    assert json.loads(seen["request"]["content"])["message"] == _request().message


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
    class Response:
        def __init__(self):
            self.status_code = status_code
            self.content = content

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setitem(
        __import__("sys").modules, "httpx", SimpleNamespace(AsyncClient=Client)
    )
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(ImmediatePublicationUnavailable) as error:
        await client.publish(_request())
    assert "wrong" not in str(error.value)
    assert "published" not in str(error.value)


@pytest.mark.asyncio
async def test_client_propagates_cancellation(monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    monkeypatch.setitem(
        __import__("sys").modules, "httpx", SimpleNamespace(AsyncClient=Client)
    )
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(asyncio.CancelledError):
        await client.publish(_request())


@pytest.mark.asyncio
async def test_client_redacts_transport_exception(monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise RuntimeError("https://service:secret@publisher.test private payload")

    monkeypatch.setitem(
        __import__("sys").modules, "httpx", SimpleNamespace(AsyncClient=Client)
    )
    client = RemotePublicationClient("http://publisher.test", "service-token")

    with pytest.raises(ImmediatePublicationUnavailable) as error:
        await client.publish(_request())
    assert "secret" not in str(error.value)
    assert "private payload" not in str(error.value)
