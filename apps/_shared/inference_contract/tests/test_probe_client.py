"""Mocked HTTP tests for the single-attempt backend probe client."""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.client import BackendProbeHttpClient
from aap_inference_contract.probe import (
    BACKEND_PROBE_PROTOCOL,
    BackendProbeFailure,
    BackendProbeLegacyFailure,
    BackendProbeSuccess,
    BackendProbeUnavailable,
    backend_probe_reply_bytes,
    decode_backend_probe_request,
    make_backend_probe_request,
)

_REGISTRY_ID = uuid.UUID("42c20fd5-345e-49e7-850e-35756e11ed40")
_SUBJECT = "v1.YWJj"
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _request():
    return make_backend_probe_request(
        _REGISTRY_ID,
        _SUBJECT,
        timeout=2,
        limits=CapabilityLimits(180, 12),
    )


def _success(request, modalities: list[str] | None = None) -> bytes:
    return backend_probe_reply_bytes(
        BackendProbeSuccess.model_validate(
            {
                "schema": BACKEND_PROBE_PROTOCOL,
                "ok": True,
                "request_id": request.request_id,
                "registry_id": request.registry_id,
                "subject_sha256": hashlib.sha256(
                    request.probe_subject.encode("ascii")
                ).hexdigest(),
                "modalities": modalities,
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
        body = await request.aread()
        decoded = decode_backend_probe_request(body)
        return httpx.Response(
            200,
            content=_success(decoded, ["image", "lidar"]),
            headers={"content-type": "application/json"},
        )

    options = _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = BackendProbeHttpClient(
        "http://inference-owner.local/base/",
        "private-probe-token",
        timeout_seconds=4,
    )
    reply = await client.execute(_request())

    assert isinstance(reply, BackendProbeSuccess)
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/base/v1/backends/probe"
    assert requests[0].headers["authorization"] == "Bearer private-probe-token"
    assert "private-probe-token" not in (await requests[0].aread()).decode("ascii")
    assert options == [{"follow_redirects": False, "trust_env": False}]


@pytest.mark.asyncio
async def test_client_maps_transport_non_200_and_malformed_replies_without_retry(
    monkeypatch,
) -> None:
    count = 0

    async def redirect(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(307, headers={"location": "http://other.invalid/"})

    _install_mock_transport(monkeypatch, httpx.MockTransport(redirect))
    client = BackendProbeHttpClient("http://owner.local", "token")
    with pytest.raises(
        BackendProbeUnavailable, match="^Inference backend probe unavailable$"
    ):
        await client.execute(_request())
    assert count == 1

    malformed_count = 0

    async def malformed(request: httpx.Request) -> httpx.Response:
        nonlocal malformed_count
        malformed_count += 1
        return httpx.Response(
            200,
            json={"schema": BACKEND_PROBE_PROTOCOL, "ok": True},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(malformed))
    with pytest.raises(BackendProbeUnavailable):
        await client.execute(_request())
    assert malformed_count == 1


@pytest.mark.asyncio
async def test_client_preserves_valid_legacy_failure_and_null_modalities(
    monkeypatch,
) -> None:
    async def failure(request: httpx.Request) -> httpx.Response:
        typed = decode_backend_probe_request(await request.aread())
        return httpx.Response(
            200,
            content=backend_probe_reply_bytes(
                BackendProbeFailure.model_validate(
                    {
                        "schema": BACKEND_PROBE_PROTOCOL,
                        "ok": False,
                        "request_id": typed.request_id,
                        "registry_id": typed.registry_id,
                        "subject_sha256": hashlib.sha256(
                            typed.probe_subject.encode("ascii")
                        ).hexdigest(),
                        "error": "legacy_materialization_failure",
                    }
                )
            ),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(failure))
    client = BackendProbeHttpClient("http://owner.local", "token")
    with pytest.raises(BackendProbeLegacyFailure):
        await client.probe_modalities(
            _REGISTRY_ID,
            _SUBJECT,
            timeout=2,
            limits=CapabilityLimits(180, 12),
        )

    async def null_modalities(request: httpx.Request) -> httpx.Response:
        typed = decode_backend_probe_request(await request.aread())
        return httpx.Response(
            200,
            content=_success(typed, None),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(null_modalities))
    assert (
        await client.probe_modalities(
            _REGISTRY_ID,
            _SUBJECT,
            timeout=2,
            limits=CapabilityLimits(180, 12),
        )
        is None
    )


@pytest.mark.asyncio
async def test_client_maps_oversized_reply_and_network_error_to_unavailable(
    monkeypatch,
) -> None:
    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b" " * 4097,
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(oversized))
    client = BackendProbeHttpClient("http://owner.local", "token")
    with pytest.raises(BackendProbeUnavailable):
        await client.execute(_request())

    network_calls = 0

    async def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        raise httpx.ConnectError("secret or URL must not escape", request=request)

    _install_mock_transport(monkeypatch, httpx.MockTransport(unavailable))
    with pytest.raises(BackendProbeUnavailable) as raised:
        await client.execute(_request())
    assert str(raised.value) == "Inference backend probe unavailable"
    assert network_calls == 1


def test_client_rejects_invalid_provider_configuration_without_io() -> None:
    for base_url, token, timeout in (
        ("file:///tmp/probe", "token", 30),
        ("http://user:password@owner.local", "token", 30),
        ("http://owner.local", "", 30),
        ("http://owner.local", "token", True),
    ):
        with pytest.raises(BackendProbeUnavailable):
            BackendProbeHttpClient(base_url, token, timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_client_rejects_compressed_reply_before_consuming_body(monkeypatch):
    class ForbiddenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Compressed provider body must not be decompressed")
            yield b""

    async def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            stream=ForbiddenBody(),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(BackendProbeUnavailable):
        await BackendProbeHttpClient("http://owner.local", "token").execute(_request())
