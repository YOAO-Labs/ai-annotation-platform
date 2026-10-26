"""Health challenge binding and forged proof rejection at the owner transport."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from aap_inference.backend_transport import (
    GPU_HEALTH_CHALLENGE_ECHO_MARKER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
)
from tests.test_ml_client_metrics import _backend, _transport


def _patched(transport):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    )


@pytest.mark.asyncio
async def test_health_meta_binds_exact_echo_to_challenge_request() -> None:
    challenge = "ab" * 32
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers[GPU_HEALTH_CHALLENGE_HEADER]
        captured["cache_control"] = request.headers["cache-control"]
        captured["query"] = request.url.params[GPU_HEALTH_CHALLENGE_QUERY_PARAM]
        return httpx.Response(
            200,
            headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
            json={
                "residency": {"state": "unloaded"},
                GPU_HEALTH_CHALLENGE_ECHO_MARKER: "body-cannot-forge-marker",
                "gpu_arbiter_probe": {"challenge": "body-cannot-forge-proof"},
            },
        )

    client = _transport(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert captured == {
        "header": challenge,
        "cache_control": "no-cache",
        "query": challenge,
    }
    assert meta is not None
    assert meta[GPU_HEALTH_CHALLENGE_ECHO_MARKER] == challenge
    assert "gpu_arbiter_probe" not in meta


@pytest.mark.asyncio
async def test_health_meta_without_exact_echo_is_connected_but_unproven() -> None:
    challenge = "ab" * 32

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={GPU_HEALTH_CHALLENGE_HEADER: "cd" * 32},
            json={"residency": {"state": "unloaded"}},
        )

    client = _transport(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert meta == {"residency": {"state": "unloaded"}}


@pytest.mark.asyncio
async def test_health_meta_rejects_duplicate_exact_echoes() -> None:
    challenge = "ab" * 32

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[
                (GPU_HEALTH_CHALLENGE_HEADER, challenge),
                (GPU_HEALTH_CHALLENGE_HEADER, challenge),
            ],
            json={"residency": {"state": "unloaded"}},
        )

    client = _transport(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert meta == {"residency": {"state": "unloaded"}}


@pytest.mark.asyncio
async def test_health_meta_falls_back_for_strict_legacy_backend_without_proof() -> None:
    challenge = "ab" * 32
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(422, json={"detail": "unknown query"})
        return httpx.Response(
            200,
            headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
            json={"residency": {"state": "unloaded"}},
        )

    backend = _backend()
    backend.auth_method = "token"
    backend.auth_token = "strict-legacy-token"
    client = _transport(backend)
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert meta == {"residency": {"state": "unloaded"}}
    assert len(requests) == 2
    assert requests[0].headers[GPU_HEALTH_CHALLENGE_HEADER] == challenge
    assert requests[0].url.params[GPU_HEALTH_CHALLENGE_QUERY_PARAM] == challenge
    assert GPU_HEALTH_CHALLENGE_HEADER not in requests[1].headers
    assert GPU_HEALTH_CHALLENGE_QUERY_PARAM not in requests[1].url.params
    assert "cache-control" not in requests[1].headers
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer strict-legacy-token",
        "Bearer strict-legacy-token",
    ]


@pytest.mark.asyncio
async def test_health_meta_rejects_noncanonical_challenge_before_request() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    client = _transport(_backend())
    with _patched(httpx.MockTransport(handler)):
        with pytest.raises(ValueError):
            await client.health_meta(gpu_health_challenge="A" * 64)

    assert called is False
