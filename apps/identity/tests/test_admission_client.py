from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import uuid

import pytest

import aap_identity_admission.client as admission_client
from aap_identity_admission.actor_status_contract import (
    ACTOR_STATUS_PROTOCOL,
    ActorStatus,
    actor_status_reply,
)
from aap_identity_admission.client import RemoteAdmissionClient
from aap_identity_admission.contract import (
    IdentityFailure,
    IdentityUnavailable,
    Principal,
    failure_reply,
    principal_reply,
)


def _principal() -> Principal:
    return Principal(
        actor_id=uuid.uuid4(),
        name="Admitted actor",
        email="actor@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=4,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


class _Stream:
    def __init__(self, status_code, chunks, *, wait=None):
        self.status_code = status_code
        self.chunks = chunks
        self.wait = wait
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False

    async def aiter_bytes(self, chunk_size):
        for chunk in self.chunks:
            if self.wait is not None:
                await self.wait.wait()
            yield chunk


class _HttpClient:
    def __init__(self, response, **kwargs):
        self.response = response
        self.kwargs = kwargs
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, *, json, headers):
        self.request = {
            "method": method,
            "url": url,
            "json": json,
            "headers": headers,
        }

        @asynccontextmanager
        async def opened():
            async with self.response as response:
                yield response

        return opened()


def _install_http(monkeypatch, response):
    clients = []

    def build(**kwargs):
        client = _HttpClient(response, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(admission_client.httpx, "AsyncClient", build)
    return clients


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base", ["https://identity.internal", "https://identity.internal/proxy/identity"]
)
async def test_authenticate_uses_shared_protocol_and_scoped_bearer(monkeypatch, base):
    principal = _principal()
    body = json.dumps(principal_reply(principal)).encode()
    response = _Stream(200, [body[:7], body[7:]])
    clients = _install_http(monkeypatch, response)
    remote = RemoteAdmissionClient(
        origin=base + "/",
        service_token="scoped-bearer",
        timeout_seconds=1.25,
    )

    assert await remote.authenticate("opaque-user-token") == principal
    client = clients[0]
    assert client.request == {
        "method": "POST",
        "url": base + "/v1/identity/authenticate",
        "json": {"token": "opaque-user-token"},
        "headers": {"Authorization": "Bearer scoped-bearer"},
    }
    assert client.kwargs == {"timeout": 1.25, "trust_env": False}
    assert response.closed


@pytest.mark.asyncio
async def test_actor_status_wire_accepts_active_role_changed_and_missing(monkeypatch):
    actor_id = uuid.uuid4()
    active = ActorStatus(actor_id=actor_id, is_active=True)
    active_response = _Stream(200, [json.dumps(actor_status_reply(active)).encode()])
    clients = _install_http(monkeypatch, active_response)
    remote = RemoteAdmissionClient(
        origin="http://identity.internal", service_token="scoped"
    )

    assert await remote.actor_status(actor_id) == active
    assert clients[0].request == {
        "method": "POST",
        "url": "http://identity.internal/v1/identity/actor-status",
        "json": {"schema": ACTOR_STATUS_PROTOCOL, "actor_id": str(actor_id)},
        "headers": {"Authorization": "Bearer scoped"},
    }

    missing_response = _Stream(200, [json.dumps(actor_status_reply(None)).encode()])
    clients = _install_http(monkeypatch, missing_response)
    assert await remote.actor_status(actor_id) is None
    assert clients[0].request["url"].endswith("/v1/identity/actor-status")


@pytest.mark.asyncio
async def test_authentication_owner_failure_is_a_closed_identity_failure(monkeypatch):
    response = _Stream(200, [json.dumps(failure_reply("credential_revoked")).encode()])
    _install_http(monkeypatch, response)
    with pytest.raises(IdentityFailure) as error:
        await RemoteAdmissionClient(
            origin="https://identity.internal", service_token="scoped"
        ).authenticate("opaque-user-token")
    assert error.value.code == "credential_revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "annotator"),
        ("generation", True),
        ("is_active", 1),
        ("credential_type", "other"),
        ("extra", True),
    ],
)
async def test_client_rejects_invalid_principal_shapes(field, value, monkeypatch):
    payload = json.loads(json.dumps(principal_reply(_principal())))
    payload["data"][field] = value
    _install_http(monkeypatch, _Stream(200, [json.dumps(payload).encode()]))

    with pytest.raises(IdentityUnavailable):
        await RemoteAdmissionClient(
            origin="https://identity.internal", service_token="scoped"
        ).authenticate("opaque-user-token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _Stream(503, [b"unavailable"]),
        _Stream(200, [b'{"schema":"wrong","ok":true}']),
    ],
)
async def test_remote_failures_are_sanitized(response, monkeypatch):
    _install_http(monkeypatch, response)
    remote = RemoteAdmissionClient(
        origin="https://identity.internal", service_token="do-not-leak"
    )

    with pytest.raises(IdentityUnavailable) as error:
        await remote.actor_status(uuid.uuid4())
    assert "do-not-leak" not in str(error.value)


@pytest.mark.asyncio
async def test_mismatched_actor_status_and_oversized_reply_fail_closed(monkeypatch):
    actor_id = uuid.uuid4()
    mismatch = ActorStatus(actor_id=uuid.uuid4(), is_active=True)
    _install_http(
        monkeypatch, _Stream(200, [json.dumps(actor_status_reply(mismatch)).encode()])
    )
    remote = RemoteAdmissionClient(
        origin="https://identity.internal", service_token="scoped"
    )
    with pytest.raises(IdentityUnavailable):
        await remote.actor_status(actor_id)

    monkeypatch.setattr(admission_client, "MAX_RESPONSE_BYTES", 16)
    _install_http(monkeypatch, _Stream(200, [b"x" * 17]))
    with pytest.raises(IdentityUnavailable):
        await remote.actor_status(actor_id)


@pytest.mark.asyncio
async def test_transport_timeout_and_cancellation_are_sanitized_and_closed(monkeypatch):
    delayed = _Stream(200, [b"{}"])

    async def slow_chunks(chunk_size):
        await asyncio.sleep(0.05)
        yield b"{}"

    delayed.aiter_bytes = slow_chunks
    _install_http(monkeypatch, delayed)
    remote = RemoteAdmissionClient(
        origin="https://identity.internal",
        service_token="secret",
        timeout_seconds=0.001,
    )
    with pytest.raises(IdentityUnavailable) as error:
        await remote.actor_status(uuid.uuid4())
    assert "secret" not in str(error.value)
    assert delayed.closed

    entered = asyncio.Event()
    blocked = _Stream(200, [], wait=asyncio.Event())

    async def wait_chunk(chunk_size):
        entered.set()
        await blocked.wait.wait()
        yield b"{}"

    blocked.aiter_bytes = wait_chunk
    _install_http(monkeypatch, blocked)
    cancel_remote = RemoteAdmissionClient(
        origin="https://identity.internal", service_token="secret", timeout_seconds=2
    )
    task = asyncio.create_task(cancel_remote.actor_status(uuid.uuid4()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert blocked.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@identity.internal",
        "https://identity.internal?",
        "https://identity.internal#",
        " https://identity.internal",
    ],
)
async def test_bad_origin_or_local_token_is_rejected_without_network(
    monkeypatch, origin
):
    called = False

    def unexpected(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("should not open HTTP transport")

    monkeypatch.setattr(admission_client.httpx, "AsyncClient", unexpected)
    with pytest.raises(IdentityUnavailable):
        await RemoteAdmissionClient(origin=origin, service_token="scoped").actor_status(
            uuid.uuid4()
        )
    with pytest.raises(IdentityFailure) as error:
        await RemoteAdmissionClient(
            origin="https://identity.internal", service_token="scoped"
        ).authenticate("")
    assert error.value.code == "credential_invalid"
    assert called is False
