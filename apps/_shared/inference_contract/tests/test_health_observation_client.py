"""Mocked HTTP checks for the single-attempt health observation client."""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest

from aap_inference_contract.client import BackendHealthObservationHttpClient
from aap_inference_contract.health_observation import (
    HEALTH_OBSERVATION_PROTOCOL,
    HEALTH_OBSERVATION_REPLY_MAX_BYTES,
    BackendHealthObservationReply,
    BackendHealthObservationUnavailable,
    decode_health_observation_request,
    health_observation_reply_bytes,
    make_health_observation_request,
)

_REGISTRY_ID = uuid.UUID("40c9aa1f-c69e-4094-b420-f9461dc89f7f")
_SUBJECT = "v1.Y2lwaGVydGV4dA=="
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _request():
    return make_health_observation_request(_REGISTRY_ID, _SUBJECT)


def _reply(request) -> bytes:
    return health_observation_reply_bytes(
        BackendHealthObservationReply.model_validate(
            {
                "schema": HEALTH_OBSERVATION_PROTOCOL,
                "request_id": request.request_id,
                "registry_id": request.registry_id,
                "subject_sha256": hashlib.sha256(
                    request.subject.encode("ascii")
                ).hexdigest(),
                "evidence": "v1.c2VhbGVk",
            }
        )
    )


def _install_mock_transport(
    monkeypatch, transport: httpx.AsyncBaseTransport
) -> list[dict]:
    options = []

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        options.append(
            {
                "follow_redirects": kwargs.get("follow_redirects"),
                "trust_env": kwargs.get("trust_env"),
            }
        )
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return options


@pytest.mark.asyncio
async def test_client_posts_once_with_bound_request_and_bearer_auth(
    monkeypatch,
) -> None:
    requests = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        typed = decode_health_observation_request(await request.aread())
        return httpx.Response(
            200,
            content=_reply(typed),
            headers={"content-type": "application/json"},
        )

    options = _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = BackendHealthObservationHttpClient(
        "http://inference-owner.local/base/",
        "private-observation-token",
        timeout_seconds=5,
    )
    result = await client.execute(_request())

    assert result.registry_id == _REGISTRY_ID
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/base/v1/backends/health-observation"
    assert requests[0].headers["authorization"] == "Bearer private-observation-token"
    assert "private-observation-token" not in (await requests[0].aread()).decode(
        "ascii"
    )
    assert options == [{"follow_redirects": False, "trust_env": False}]


@pytest.mark.asyncio
async def test_client_does_not_follow_redirect_or_retry_malformed_reply(
    monkeypatch,
) -> None:
    calls = 0

    async def redirect(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "http://other.invalid/"})

    _install_mock_transport(monkeypatch, httpx.MockTransport(redirect))
    client = BackendHealthObservationHttpClient("http://owner.local", "token")
    with pytest.raises(BackendHealthObservationUnavailable):
        await client.execute(_request())
    assert calls == 1

    malformed_calls = 0

    async def malformed(request: httpx.Request) -> httpx.Response:
        nonlocal malformed_calls
        malformed_calls += 1
        return httpx.Response(
            200,
            json={"schema": HEALTH_OBSERVATION_PROTOCOL, "evidence": "plaintext"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(malformed))
    with pytest.raises(BackendHealthObservationUnavailable):
        await client.execute(_request())
    assert malformed_calls == 1


@pytest.mark.asyncio
async def test_client_bounds_reply_and_sanitizes_transport_failures(
    monkeypatch,
) -> None:
    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"",
            headers={
                "content-type": "application/json",
                "content-length": str(HEALTH_OBSERVATION_REPLY_MAX_BYTES + 1),
            },
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(oversized))
    client = BackendHealthObservationHttpClient("http://owner.local", "token")
    with pytest.raises(BackendHealthObservationUnavailable):
        await client.execute(_request())

    calls = 0

    async def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("private URL and token", request=request)

    _install_mock_transport(monkeypatch, httpx.MockTransport(unavailable))
    with pytest.raises(BackendHealthObservationUnavailable) as raised:
        await client.execute(_request())
    assert str(raised.value) == "Inference health observation unavailable"
    assert calls == 1


def test_client_rejects_invalid_provider_configuration() -> None:
    for base_url, token, timeout in (
        ("file:///tmp/owner", "token", 30),
        ("http://user:password@owner.local", "token", 30),
        ("http://owner.local", "", 30),
        ("http://owner.local", "token", True),
    ):
        with pytest.raises(BackendHealthObservationUnavailable):
            BackendHealthObservationHttpClient(base_url, token, timeout_seconds=timeout)
